#!/usr/bin/env python3
"""Pre-execution RLM boundary for Codex Bash commands.

The important boundary is PreToolUse. The hook writes the original Bash
command to a private temporary script and changes the tool input to invoke
this file with ``--exec``. Codex therefore receives the runner's stdout as
the tool result. When the command is large, the runner stores the complete
stdout/stderr in /tmp and emits only a bounded card while preserving the
original return code.

There is deliberately no PostToolUse output summarizer here. A PostToolUse
hook runs after the tool result has already been produced and cannot remove
raw output from the billed context.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RLM_VERSION = "4.7-adapted-pretool"
DEFAULT_OUTPUT_THRESHOLD = 4_000
MAX_CARD_CHARS = 2_400
MAX_PREVIEW_CHARS = 850
MAX_TAIL_CHARS = 550
DEFAULT_ARTIFACT_ROOT = Path("/tmp/codex-continuous-rlm")
DEFAULT_RETENTION_SECONDS = 24 * 60 * 60
DEFAULT_MAX_ARTIFACT_BYTES = 256 * 1024 * 1024


def _safe_name(value: Any, default: str = "unknown", limit: int = 80) -> str:
    text = str(value or "").strip()
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
    text = "".join(char if char in allowed else "_" for char in text)
    return (text[:limit] or default).strip("._-") or default


def _threshold() -> int:
    try:
        value = int(os.environ.get("CODEX_CONTINUOUS_RLM_OUTPUT_THRESHOLD", str(DEFAULT_OUTPUT_THRESHOLD)))
    except ValueError:
        value = DEFAULT_OUTPUT_THRESHOLD
    return max(256, value)


def _artifact_root() -> Path:
    configured = os.environ.get("CODEX_CONTINUOUS_RLM_BASH_ARTIFACT_DIR")
    return Path(configured).expanduser() if configured else DEFAULT_ARTIFACT_ROOT


def _retention_seconds() -> int:
    try:
        value = int(os.environ.get("CODEX_CONTINUOUS_RLM_RETENTION_SECONDS", str(DEFAULT_RETENTION_SECONDS)))
    except ValueError:
        value = DEFAULT_RETENTION_SECONDS
    return max(1, value)


def _max_artifact_bytes() -> int:
    try:
        value = int(os.environ.get("CODEX_CONTINUOUS_RLM_MAX_ARTIFACT_BYTES", str(DEFAULT_MAX_ARTIFACT_BYTES)))
    except ValueError:
        value = DEFAULT_MAX_ARTIFACT_BYTES
    return max(1, value)


def _cleanup_stale() -> None:
    """Remove only this runner's expired files and enforce its size cap."""

    root = _artifact_root()
    now = time.time()
    retention = _retention_seconds()

    # Remove orphaned scripts created by the previous implementation, which
    # placed them directly in /tmp. Match only our exact private prefix.
    try:
        for path in Path("/tmp").glob("codex-rlm-cmd-*.sh"):
            try:
                if now - path.stat().st_mtime > retention:
                    path.unlink()
            except OSError:
                continue
    except OSError:
        pass

    if not root.exists():
        return
    candidates: list[tuple[Path, float, int]] = []
    try:
        paths = [path for path in root.rglob("*") if path.is_file() and not path.is_symlink()]
    except OSError:
        return
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        if now - stat.st_mtime > retention:
            try:
                path.unlink()
            except OSError:
                pass
            continue
        candidates.append((path, stat.st_mtime, stat.st_size))

    total = sum(size for _, _, size in candidates)
    if total > _max_artifact_bytes():
        for path, _, size in sorted(candidates, key=lambda item: item[1]):
            if total <= _max_artifact_bytes():
                break
            try:
                path.unlink()
                total -= size
            except OSError:
                continue

    # Avoid leaving one directory per old session behind. Never remove the
    # runner root itself; it is cheap and keeps the next invocation simple.
    try:
        directories = sorted(
            (path for path in root.rglob("*") if path.is_dir() and not path.is_symlink()),
            key=lambda path: len(path.parts),
            reverse=True,
        )
        for directory in directories:
            try:
                directory.rmdir()
            except OSError:
                continue
    except OSError:
        pass


def _command_root() -> Path:
    root = _artifact_root() / "commands"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def _write_command_script(command: str) -> Path:
    """Create a script whose one child shell preserves the original command."""

    _cleanup_stale()
    fd, raw_path = tempfile.mkstemp(
        prefix="codex-rlm-cmd-", suffix=".sh", dir=_command_root(), text=True
    )
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n")
            handle.write("exec /bin/sh -c ")
            handle.write(shlex.quote(command))
            handle.write("\n")
        os.chmod(path, 0o700)
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _wrapper_command(command: str, cwd: str, session_id: str) -> str:
    script = _write_command_script(command)
    installed_runner = Path(__file__).resolve().parent / "rlm" / "rlm_runner.py"
    runner = installed_runner if installed_runner.is_file() else Path(__file__).resolve()
    argv = [
        sys.executable,
        str(runner),
        "--exec",
        str(script),
        "--cwd",
        cwd,
        "--session",
        session_id,
    ]
    return shlex.join(argv)


def pretool_bash(data: dict[str, Any]) -> dict[str, Any]:
    """Return a PreToolUse updatedInput that executes Bash through the runner."""

    tool_name = str(data.get("tool_name") or data.get("name") or "")
    if tool_name.lower() not in {"bash", "shell", "exec", "exec_command", "command", "terminal"}:
        return {}
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return {}
    command = tool_input.get("command") or tool_input.get("cmd") or tool_input.get("shell_command")
    if not isinstance(command, str) or not command.strip():
        return {}
    # Never wrap the runner itself. This also makes the hook idempotent when
    # another PreToolUse hook replays an updated command.
    if "--exec" in command and (
        "codex_continuous_rlm.py" in command or "rlm_runner.py" in command
    ):
        return {}
    cwd = str(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    cwd_path = Path(cwd).expanduser()
    if not cwd_path.is_dir():
        cwd = os.getcwd()
    session = _safe_name(data.get("session_id") or os.environ.get("CLAUDE_SESSION_ID"))
    try:
        wrapped = _wrapper_command(command, cwd, session)
    except OSError:
        return {}
    updated = dict(tool_input)
    updated["command"] = wrapped
    if "cmd" in updated:
        updated["cmd"] = wrapped
    if "shell_command" in updated:
        updated["shell_command"] = wrapped
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": updated,
        },
        "continue": True,
    }


def _clip_bytes(value: bytes, limit: int) -> str:
    text = value.decode("utf-8", errors="replace")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[… clipped …]"


def _artifact_path(session_id: str, payload: bytes) -> Path:
    root = _artifact_root() / _safe_name(session_id, "session")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    digest = hashlib.sha256(payload).hexdigest()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return root / f"{stamp}-{digest[:16]}.log"


def _write_artifact(session_id: str, payload: bytes) -> tuple[Path, str]:
    _cleanup_stale()
    digest = hashlib.sha256(payload).hexdigest()
    path = _artifact_path(session_id, payload)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(payload)
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)
    _cleanup_stale()
    return path, digest


def _summary_card(
    *,
    path: Path,
    digest: str,
    stdout: bytes,
    stderr: bytes,
    returncode: int,
    total_bytes: int,
) -> str:
    preview = _clip_bytes(stdout, MAX_PREVIEW_CHARS)
    if not preview:
        preview = _clip_bytes(stderr, MAX_PREVIEW_CHARS)
    tail_source = stdout if stdout else stderr
    tail = _clip_bytes(tail_source[-MAX_TAIL_CHARS:], MAX_TAIL_CHARS)
    lines = [
        f"[RLM {RLM_VERSION}] Bash output stored off-context.",
        f"returncode: {returncode} | captured: {total_bytes:,} bytes | stdout: {len(stdout):,} | stderr: {len(stderr):,}",
        f"artifact: {path}",
        f"sha256: {digest}",
        "Only this card was written to the command stdout. Read the artifact with a focused range if needed.",
    ]
    if preview:
        lines.extend(["", "preview:", preview])
    if stderr:
        lines.extend(["", "stderr preview:", _clip_bytes(stderr, 500)])
    if tail and tail != preview:
        lines.extend(["", "tail:", tail])
    return "\n".join(lines)[:MAX_CARD_CHARS]


def execute_script(script: Path, cwd: str | None, session_id: str) -> int:
    """Execute the wrapped command and make stdout the sole bounded boundary."""

    _cleanup_stale()
    try:
        completed = subprocess.run(
            ["/bin/sh", str(script)],
            cwd=cwd or None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        stdout = completed.stdout or b""
        stderr = completed.stderr or b""
        combined = stdout + stderr
        if len(combined) > _threshold():
            try:
                path, digest = _write_artifact(session_id, b"[stdout]\n" + stdout + b"\n[stderr]\n" + stderr)
                sys.stdout.write(
                    _summary_card(
                        path=path,
                        digest=digest,
                        stdout=stdout,
                        stderr=stderr,
                        returncode=completed.returncode,
                        total_bytes=len(combined),
                    )
                    + "\n"
                )
            except OSError:
                # Preserve the bounded boundary even if /tmp is unavailable.
                sys.stdout.write(
                    f"[RLM] command output exceeded {_threshold():,} bytes; artifact storage failed; "
                    f"returncode: {completed.returncode}\n"
                )
        else:
            sys.stdout.buffer.write(stdout)
            sys.stdout.flush()
            sys.stderr.buffer.write(stderr)
            sys.stderr.flush()
        return completed.returncode
    finally:
        script.unlink(missing_ok=True)
        _cleanup_stale()


def _read_input() -> dict[str, Any]:
    value = json.loads(sys.stdin.read() or "{}")
    return value if isinstance(value, dict) else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pretool-bash", action="store_true")
    parser.add_argument("--exec", dest="script", type=Path)
    parser.add_argument("--cwd")
    parser.add_argument("--session", default="unknown")
    args = parser.parse_args(argv)
    if args.pretool_bash:
        try:
            result = pretool_bash(_read_input())
        except (OSError, TypeError, ValueError):
            result = {}
        sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
        return 0
    if args.script:
        return execute_script(args.script.expanduser().resolve(), args.cwd, _safe_name(args.session))
    parser.error("use --pretool-bash or --exec SCRIPT")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
