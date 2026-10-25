#!/usr/bin/env python3
"""Track edits made through Codex commands in any Git worktree.

Codex can edit a repository that is outside the current working directory by
using an absolute path in a shell command.  Claude's original post-edit hook
only knows ``CLAUDE_PROJECT_DIR``, so it misses that repository.  This hook
records candidate worktrees before a tool runs, compares their Git status and
explicit file hashes afterwards, and sends notifications to each affected
repository's TLDR daemon.

The hook is deliberately observational and fail-open.  It never blocks a
tool, starts embedding work itself, or scans arbitrary directories.  The
upstream daemon applies its normal dirty-file threshold before rebuilding the
repository's FAISS index.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex_continuous_bridge import canonical_tool_name  # noqa: E402
from codex_continuous_semantic_index import index_path, metadata_path  # noqa: E402


SOURCE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".ex",
    ".exs",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".lua",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".scala",
    ".sh",
    ".swift",
    ".ts",
    ".tsx",
}
PATH_RE = re.compile(r"(?<![A-Za-z0-9_])/(?:[^\s'\";&|()<>]+)")
WRITE_COMMAND_RE = re.compile(
    r"(?:^|\s)(?:sed|perl|python(?:3)?|python3?\.\d+|node|ruby|go|cargo|make|"
    r"patch|apply_patch|tee|cp|mv|rm|mkdir|touch|install|chmod|git)(?:\s|$)|"
    r"(?:\s-i(?:\s|$)|\s(?:>>?|2>|2>>|--write|--fix)(?:\s|$)|apply_patch)",
    re.IGNORECASE,
)
MAX_SNAPSHOT_FILES = 512
MAX_NOTIFY_FILES = 64
STATE_TTL_SECONDS = 15 * 60


def _deployment(payload: dict[str, Any]) -> Path:
    for key in ("CODEX_CONTINUOUS_DEPLOYMENT", "CODEX_CONTINUOUS_ROOT"):
        value = os.environ.get(key)
        if value and Path(value).is_dir():
            return Path(value).expanduser().resolve()
    value = payload.get("deployment")
    if isinstance(value, str) and Path(value).is_dir():
        return Path(value).expanduser().resolve()
    return ROOT


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:120] or "unknown"


def _state_dir(deployment: Path) -> Path:
    path = deployment / ".runtime" / "change-tracker"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _tool_input(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("tool_input")
    if isinstance(value, dict):
        return value
    value = payload.get("input")
    return value if isinstance(value, dict) else {}


def _command(payload: dict[str, Any]) -> str:
    tool_input = _tool_input(payload)
    for key in ("command", "cmd", "shell_command"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
    return ""


def _explicit_paths(payload: dict[str, Any]) -> list[Path]:
    tool_input = _tool_input(payload)
    values: list[str] = []
    for key in ("file_path", "path", "file", "target", "destination"):
        value = tool_input.get(key)
        if isinstance(value, str):
            values.append(value)
    for key in ("file_paths", "paths"):
        value = tool_input.get(key)
        if isinstance(value, list):
            values.extend(item for item in value if isinstance(item, str))
    response = payload.get("tool_response")
    if isinstance(response, dict):
        for key in ("filePath", "file_path", "path"):
            value = response.get(key)
            if isinstance(value, str):
                values.append(value)
    result: list[Path] = []
    for value in values:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path(str(payload.get("cwd") or os.getcwd())) / path
        result.append(path.resolve(strict=False))
    return list(dict.fromkeys(result))


def _command_paths(payload: dict[str, Any]) -> list[Path]:
    command = _command(payload)
    if not command:
        return _explicit_paths(payload)
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = []
    cwd = Path(str(payload.get("cwd") or os.getcwd())).expanduser().resolve()
    values: list[str] = []
    paths = _explicit_paths(payload)

    # Absolute paths embedded in redirections, substitutions, or shell words.
    values.extend(match.rstrip(".,:") for match in PATH_RE.findall(command))
    for index, token in enumerate(tokens):
        clean = token.strip(",;:")
        if not clean:
            continue
        if clean in {"cd", "-C", "--directory", "--work-tree", "--git-dir", "-f"}:
            if index + 1 < len(tokens):
                values.append(tokens[index + 1])
            continue
        for prefix in ("--git-dir=", "--work-tree=", "--directory=", "--project="):
            if clean.startswith(prefix):
                values.append(clean.split("=", 1)[1])
                break
        if clean.startswith(("/", "~", "$HOME/")):
            values.append(clean)
        elif "/" in clean and (clean.endswith(tuple(SOURCE_SUFFIXES)) or clean.startswith("./")):
            values.append(clean)

    # A relative path after `cd /repo-b` belongs to that directory.  Include
    # the cd target itself even when the target does not exist yet.
    for index, token in enumerate(tokens[:-1]):
        if token == "cd":
            target = Path(tokens[index + 1]).expanduser()
            if not target.is_absolute():
                target = cwd / target
            values.append(str(target))
            for relative in tokens[index + 2 :]:
                if relative.startswith(("-", ";", "&&", "||", ">", "<")):
                    continue
                if relative.endswith(tuple(SOURCE_SUFFIXES)) and "/" not in relative:
                    values.append(str(target / relative))
                    break

    for value in values:
        path = Path(value).expanduser()
        if str(path).startswith("$HOME/"):
            path = Path(os.environ.get("HOME", str(Path.home()))) / str(path)[6:]
        if not path.is_absolute():
            path = cwd / path
        paths.append(path.resolve(strict=False))
    return list(dict.fromkeys(paths))


def _git_root(path: Path) -> Path | None:
    candidate = path if path.is_dir() else path.parent
    try:
        result = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            text=True,
            capture_output=True,
            check=False,
            timeout=1.5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return Path(value).resolve() if value else None


def candidate_roots(payload: dict[str, Any]) -> list[Path]:
    roots: list[Path] = []
    for path in _command_paths(payload):
        root = _git_root(path)
        if root is not None and root not in roots:
            roots.append(root)
    cwd = Path(str(payload.get("cwd") or os.getcwd())).expanduser().resolve()
    root = _git_root(cwd)
    if root is not None and root not in roots:
        roots.insert(0, root)
    return roots


def _status_paths(root: Path) -> set[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            capture_output=True,
            check=False,
            timeout=3.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if result.returncode != 0:
        return set()
    paths: set[str] = set()
    for raw in result.stdout.split(b"\0"):
        if len(raw) < 4:
            continue
        value = os.fsdecode(raw[3:])
        if " -> " in value:
            old, new = value.split(" -> ", 1)
            paths.update((old, new))
        else:
            paths.add(value)
    return {
        value
        for value in paths
        if Path(value).suffix.lower() in SOURCE_SUFFIXES
    }


def _digest(path: Path) -> str | None:
    try:
        stat = path.stat()
        if stat.st_size > 1024 * 1024:
            return f"large:{stat.st_size}:{stat.st_mtime_ns}"
        digest = hashlib.sha1()  # noqa: S324 - change detection, not security
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(131072), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _relative_source(root: Path, path: Path) -> str | None:
    try:
        relative = path.resolve(strict=False).relative_to(root)
    except ValueError:
        return None
    if relative.suffix.lower() not in SOURCE_SUFFIXES:
        return None
    return relative.as_posix()


def _snapshot(root: Path, paths: list[Path]) -> dict[str, Any]:
    status = _status_paths(root)
    explicit: set[str] = set()
    for path in paths:
        relative = _relative_source(root, path)
        if relative:
            explicit.add(relative)
    all_paths = list(status | explicit)[:MAX_SNAPSHOT_FILES]
    hashes = {
        relative: digest
        for relative in all_paths
        if (digest := _digest(root / relative)) is not None
    }
    return {"status": sorted(status), "hashes": hashes, "explicit": sorted(explicit)}


def _state_key(payload: dict[str, Any]) -> str:
    value = str(
        payload.get("tool_use_id")
        or payload.get("call_id")
        or f"{payload.get('session_id', 'unknown')}:{_command(payload)}"
    )
    return hashlib.sha1(value.encode("utf-8", "replace")).hexdigest()[:32]  # noqa: S324


def _state_path(deployment: Path, payload: dict[str, Any]) -> Path:
    return _state_dir(deployment) / f"pending-{_safe(_state_key(payload))}.json"


def _successful(payload: dict[str, Any]) -> bool:
    response = payload.get("tool_response")
    if not isinstance(response, dict):
        return True
    if response.get("success") is False:
        return False
    for key in ("exitCode", "exit_code"):
        value = response.get(key)
        if isinstance(value, int) and value != 0:
            return False
    return True


def _may_modify(payload: dict[str, Any]) -> bool:
    tool = canonical_tool_name(payload.get("tool_name") or payload.get("name"))
    if tool in {"Edit", "Write", "MultiEdit"}:
        return True
    if tool != "Bash":
        return False
    command = _command(payload)
    return bool(WRITE_COMMAND_RE.search(command))


def _write_pending(deployment: Path, payload: dict[str, Any]) -> None:
    roots = [
        root
        for root in candidate_roots(payload)
        if index_path(root).is_file() and metadata_path(root).is_file()
    ]
    paths = _command_paths(payload)
    state = {
        "created_at": time.time(),
        "roots": {
            str(root): _snapshot(root, paths)
            for root in roots
        },
    }
    try:
        _state_path(deployment, payload).write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def _load_pending(deployment: Path, payload: dict[str, Any]) -> dict[str, Any] | None:
    path = _state_path(deployment, payload)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        path.unlink(missing_ok=True)
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - float(state.get("created_at", 0)) > STATE_TTL_SECONDS:
        return None
    return state if isinstance(state, dict) else None


def _changed_files(root: Path, before: dict[str, Any] | None, paths: list[Path]) -> list[Path]:
    after = _snapshot(root, paths)
    if before is None:
        changed = set(after.get("status", [])) | set(after.get("explicit", []))
    else:
        changed = set(before.get("status", [])) ^ set(after.get("status", []))
        old_hashes = before.get("hashes", {})
        new_hashes = after.get("hashes", {})
        changed.update(
            name
            for name in set(old_hashes) | set(new_hashes)
            if old_hashes.get(name) != new_hashes.get(name)
        )
    return [root / name for name in sorted(changed) if Path(name).suffix.lower() in SOURCE_SUFFIXES]


def _notify(deployment: Path, root: Path, files: list[Path]) -> int:
    if not (index_path(root).is_file() and metadata_path(root).is_file()):
        return 0
    helper = deployment / "codex_continuous_tldr_daemon.py"
    if not helper.is_file():
        return 0
    notified = 0
    for path in files[:MAX_NOTIFY_FILES]:
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(helper),
                    "--project",
                    str(root),
                    "--deployment",
                    str(deployment),
                    "--request-json",
                    json.dumps({"cmd": "notify", "file": str(path)}, ensure_ascii=False),
                ],
                cwd=str(root),
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            notified += 1
    return notified


def process(payload: dict[str, Any], event: str, deployment: Path | None = None) -> dict[str, Any]:
    """Record before-state or notify changed external repositories."""

    if not _may_modify(payload):
        return {}
    deployment = deployment or _deployment(payload)
    if event == "PreToolUse":
        _write_pending(deployment, payload)
        return {}
    if event != "PostToolUse" or not _successful(payload):
        if event == "PostToolUse":
            _load_pending(deployment, payload)
        return {}
    pending = _load_pending(deployment, payload)
    paths = _command_paths(payload)
    roots = candidate_roots(payload)
    before_roots = pending.get("roots", {}) if pending else {}
    for name in before_roots:
        root = Path(name)
        if root not in roots and root.is_dir():
            roots.append(root)
    total = 0
    for root in roots:
        before = before_roots.get(str(root)) if isinstance(before_roots, dict) else None
        total += _notify(deployment, root, _changed_files(root, before, paths))
    return {"notified": total} if total else {}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            payload = {}
        event = str(payload.get("hook_event_name") or os.environ.get("CLAUDE_HOOK_EVENT_NAME") or "")
        process(payload, event)
    except (OSError, TypeError, ValueError):
        pass
    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
