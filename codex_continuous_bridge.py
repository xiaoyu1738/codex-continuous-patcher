#!/usr/bin/env python3
"""Adapt Continuous Claude command hooks to the Codex hook wire format.

Codex and Claude Code intentionally expose very similar lifecycle hooks, but
their tool names and a few output conventions differ.  The installer creates
one Codex command hook per Continuous Claude command and points each command
at this bridge.  The bridge then:

* normalizes Codex input to the fields expected by Claude hooks;
* performs the original Claude matcher locally (Codex receives a catch-all
  matcher so aliases such as ``shell`` and ``apply_patch`` still work);
* runs the original command with a deployment-local Claude environment; and
* converts legacy ``{"result": ...}``/plain-text output to Codex JSON.

The bridge is deliberately fail-open.  A broken optional continuity hook must
not prevent Codex from editing a project.  Set ``CODEX_CONTINUOUS_STRICT=1``
when diagnosing a hook to return a non-zero status instead.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONTEXT_EVENTS = {
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
}
TOOL_EVENTS = {"PreToolUse", "PostToolUse", "PermissionRequest"}

# Codex has used several names for the same native tools across CLI builds.
# Keep aliases here instead of baking a specific build's names into the
# generated hooks.json.
TOOL_ALIASES = {
    "bash": "Bash",
    "shell": "Bash",
    "exec": "Bash",
    "exec_command": "Bash",
    "command": "Bash",
    "run_command": "Bash",
    "terminal": "Bash",
    "apply_patch": "Edit",
    "patch": "Edit",
    "edit": "Edit",
    "file_edit": "Edit",
    "write": "Write",
    "file_write": "Write",
    "write_file": "Write",
    "read": "Read",
    "read_file": "Read",
    "cat": "Read",
    "grep": "Grep",
    "search": "Grep",
    "rg": "Grep",
    "task": "Task",
    "agent": "Task",
    "spawn_agent": "Task",
    "multi_edit": "MultiEdit",
}


def _write_output(value: dict[str, Any]) -> None:
    """Write exactly one JSON object to stdout."""

    sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


def _log(message: str) -> None:
    sys.stderr.write(f"[codex-continuous] {message}\n")


def _strict() -> bool:
    return os.environ.get("CODEX_CONTINUOUS_STRICT", "").lower() in {"1", "true", "yes"}


def canonical_tool_name(value: Any) -> str:
    """Map a Codex/native tool name to the Claude Code spelling."""

    if not isinstance(value, str):
        return ""
    raw = value.strip()
    if not raw:
        return ""
    return TOOL_ALIASES.get(raw.lower(), raw)


def _patch_paths(patch: str) -> list[str]:
    """Extract file names from Codex's freeform apply_patch payload."""

    paths: list[str] = []
    for line in patch.splitlines():
        match = re.match(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$", line)
        if match:
            paths.append(match.group(1))
            continue
        match = re.match(r"^\*\*\* Move to: (.+?)\s*$", line)
        if match:
            paths.append(match.group(1))
    return list(dict.fromkeys(paths))


def adapt_input(raw: Any, event: str) -> dict[str, Any]:
    """Build a Claude-compatible hook payload from Codex's payload."""

    data = copy.deepcopy(raw) if isinstance(raw, dict) else {}
    data["hook_event_name"] = event
    data["cwd"] = str(data.get("cwd") or os.getcwd())
    data["session_id"] = str(
        data.get("session_id")
        or data.get("thread_id")
        or os.environ.get("CODEX_SESSION_ID")
        or "unknown"
    )
    data.setdefault("transcript_path", None)
    data.setdefault("model", os.environ.get("CODEX_MODEL", "codex"))
    data.setdefault("permission_mode", "default")
    data.setdefault("turn_id", os.environ.get("CODEX_TURN_ID", ""))

    if event in TOOL_EVENTS:
        original_name = data.get("tool_name") or data.get("name") or data.get("tool")
        data["tool_name"] = canonical_tool_name(original_name)
        tool_input = data.get("tool_input")
        if not isinstance(tool_input, dict):
            alternate_input = data.get("input")
            if isinstance(alternate_input, dict):
                tool_input = alternate_input
            elif isinstance(alternate_input, str):
                tool_input = {"command": alternate_input}
            elif isinstance(tool_input, str):
                tool_input = {"command": tool_input}
            else:
                tool_input = {}
        tool_input = copy.deepcopy(tool_input)
        # Claude hooks consistently use these names.  Keep the native values
        # too, so a hook that understands Codex can still inspect them.
        aliases = {
            "path": "file_path",
            "file": "file_path",
            "cmd": "command",
            "shell_command": "command",
            "new_content": "content",
            "replacement": "new_string",
            "old": "old_string",
            "patch": "command",
        }
        for source, target in aliases.items():
            if target not in tool_input and source in tool_input:
                tool_input[target] = tool_input[source]
        if data["tool_name"] == "Edit" and isinstance(tool_input.get("command"), str):
            paths = _patch_paths(tool_input["command"])
            if paths:
                tool_input.setdefault("file_path", paths[0])
                tool_input.setdefault("file_paths", paths)
                tool_input.setdefault("patch", tool_input["command"])
        data["tool_input"] = tool_input
        if "tool_response" not in data and "tool_output" in data:
            data["tool_response"] = data["tool_output"]
        data.setdefault("tool_use_id", data.get("call_id", ""))

    if event == "UserPromptSubmit" and not isinstance(data.get("prompt"), str):
        data["prompt"] = str(data.get("text") or data.get("message") or "")

    if event == "Stop":
        data.setdefault("stop_hook_active", False)

    return data


def _matcher_input(data: dict[str, Any], event: str) -> str | None:
    if event in TOOL_EVENTS:
        return str(data.get("tool_name") or "")
    if event == "SessionStart":
        return str(data.get("source") or "startup")
    if event == "SessionEnd":
        return str(data.get("reason") or "other")
    if event in {"PreCompact", "PostCompact"}:
        return str(data.get("trigger") or "auto")
    if event in {"SubagentStart", "SubagentStop"}:
        return str(data.get("agent_type") or "")
    return None


def matches_source_matcher(matcher: Any, data: dict[str, Any], event: str) -> bool:
    """Apply the original Claude matcher after alias normalization."""

    if matcher is None or matcher == "":
        return True
    if not isinstance(matcher, str):
        return False
    value = _matcher_input(data, event)
    if value is None:
        return True
    try:
        return re.search(matcher, value) is not None
    except re.error:
        _log(f"ignoring invalid matcher {matcher!r} for {event}")
        return False


def _forbidden_session_access(data: dict[str, Any], event: str) -> bool:
    """Reject tool requests that read raw Codex session history."""

    if event != "PreToolUse":
        return False
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    values: list[str] = []
    for key in ("file_path", "path", "command", "cmd", "shell_command", "query"):
        value = tool_input.get(key)
        if isinstance(value, str):
            values.append(value)
    combined = "\n".join(values)
    return "/.codex/sessions/" in combined or "~/.codex/sessions/" in combined or "$HOME/.codex/sessions/" in combined


def _forbidden_access_output() -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "Blocked: reading raw ~/.codex/sessions history is disabled by Continuous Codex. "
                "Use the current session context, a handoff YAML, or the memory recall interface."
            ),
        }
    }
def _manifest_path() -> Path:
    configured = os.environ.get("CODEX_CONTINUOUS_MANIFEST")
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().with_name("hooks-manifest.json")


def _load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            values[key] = value
    return values


def _yaml_quote(value: str) -> str:
    """Quote a scalar safely for the small YAML handoff we generate."""

    return json.dumps(value.replace("\n", " ")[:500], ensure_ascii=False)


def _codex_compact_handoff(data: dict[str, Any]) -> Path | None:
    """Persist a Codex-native handoff before automatic context compaction.

    Claude's original hook expects a continuity ledger and a Claude transcript
    shape. Codex supplies a rollout transcript and may have no ledger at all,
    so the adapter always writes a minimal, readable YAML handoff. The next
    ``SessionStart(source=compact)`` hook can discover this file and inject it
    into the compacted context.
    """

    if str(data.get("trigger") or "auto").lower() != "auto":
        return None
    project_dir = Path(str(data.get("cwd") or os.getcwd())).expanduser()
    if not project_dir.is_dir():
        project_dir = Path.cwd()
    handoff_dir = project_dir / "thoughts" / "shared" / "handoffs" / "codex"
    try:
        handoff_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    tool_calls: list[str] = []
    files: list[str] = []
    last_assistant = ""
    transcript = data.get("transcript_path")
    transcript_path = Path(str(transcript)).expanduser() if transcript else None
    if transcript_path and transcript_path.is_file():
        try:
            for line in transcript_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("role") == "assistant" and isinstance(entry.get("content"), str):
                    last_assistant = entry["content"]
                tool_name = entry.get("tool_name") or entry.get("name")
                if not tool_name and entry.get("type") == "function_call":
                    tool_name = entry.get("name")
                if isinstance(tool_name, str) and tool_name:
                    tool_calls.append(tool_name)
                tool_input = entry.get("tool_input")
                if not isinstance(tool_input, dict):
                    tool_input = entry.get("arguments") if isinstance(entry.get("arguments"), dict) else {}
                if isinstance(tool_input, dict):
                    candidate = tool_input.get("file_path") or tool_input.get("path")
                    if isinstance(candidate, str) and candidate:
                        files.append(candidate)
        except OSError:
            pass

    now = datetime.now(timezone.utc)  # noqa: UP017
    stamp = now.strftime("%Y-%m-%dT%H-%M-%S")
    session = str(data.get("session_id") or "unknown")
    path = handoff_dir / f"auto-handoff-{stamp}-{session[:8]}.yaml"
    unique_tools = list(dict.fromkeys(tool_calls[-10:]))
    unique_files = list(dict.fromkeys(files[-20:]))
    goal = "Continue the current Codex task after automatic context compaction"
    current = last_assistant.strip() or "Resume from the most recent tool activity"
    lines = [
        "---",
        "type: auto-handoff",
        "trigger: codex-pre-compact-auto",
        f"date: {_yaml_quote(now.isoformat())}",
        f"session: {_yaml_quote(session)}",
        "---",
        f"goal: {_yaml_quote(goal)}",
        f"now: {_yaml_quote(current)}",
        "recent_tool_calls:",
    ]
    if unique_tools:
        lines.extend(f"  - {_yaml_quote(name)}" for name in unique_tools)
    else:
        lines.append("  []")
    lines.append("files_modified:")
    if unique_files:
        lines.extend(f"  - {_yaml_quote(name)}" for name in unique_files)
    else:
        lines.append("  []")
    lines.extend(
        [
            "next:",
            "  - \"Review this handoff and continue the task\"",
            f"transcript_path: {_yaml_quote(str(transcript_path) if transcript_path else '')}",
        ]
    )
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        return None
    return path


def load_hook(manifest_path: Path, hook_id: str) -> tuple[dict[str, Any], Path]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    hook = manifest.get("hooks", {}).get(hook_id)
    if not isinstance(hook, dict):
        raise KeyError(f"unknown hook id: {hook_id}")
    deployment_root = Path(manifest.get("deployment_root") or manifest_path.parent).expanduser()
    return hook, deployment_root


def rewrite_command(command: str, deployment_root: Path) -> str:
    """Point Claude-specific absolute paths at the deployed copy."""

    deployed_claude = str(deployment_root / ".claude")
    replacements = (
        ("${HOME}/.claude", deployed_claude),
        ("$HOME/.claude", deployed_claude),
        ("~/.claude", deployed_claude),
    )
    rewritten = command
    for source, target in replacements:
        rewritten = rewritten.replace(source, target)

    # The launcher itself searches ~/.claude.  Resolve its requested hook
    # directly so the deployed copy is used without changing the user's HOME.
    try:
        tokens = shlex.split(rewritten)
    except ValueError:
        return rewritten
    # The smart-search hook is a direct Node command rather than the generic
    # ``hook_launcher.py`` wrapper used by several older hooks.
    if any(token.endswith("smart-search-router.mjs") for token in tokens):
        adapter = deployment_root / "codex_continuous_search_router.py"
        if adapter.is_file():
            return f"python3 {shlex.quote(str(adapter))}"
    # The upstream SessionStart hook warms the structural cache, but its
    # current command (`tldr daemon warm`) is not a supported llm-tldr CLI
    # command and it never builds the FAISS semantic index. Route it through
    # the Codex adapter, which schedules both safe background warmers.
    if any(token.endswith("session-start-tldr-cache.mjs") for token in tokens):
        adapter = deployment_root / "codex_continuous_semantic_warm.py"
        if adapter.is_file():
            return f"python3 {shlex.quote(str(adapter))}"
    launcher_index = next(
        (i for i, token in enumerate(tokens) if token.endswith("hook_launcher.py")), None
    )
    if launcher_index is None or launcher_index + 1 >= len(tokens):
        return rewritten
    hook_name = tokens[launcher_index + 1]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", hook_name):
        return rewritten
    hooks_dir = deployment_root / ".claude" / "hooks"
    if hook_name == "pre-compact-continuity":
        python_hook = hooks_dir / "pre_compact_continuity.py"
        if python_hook.exists():
            return f"python3 {shlex.quote(str(python_hook))}"
    candidates = (
        (hooks_dir / "dist" / f"{hook_name}.mjs", "node"),
        (hooks_dir / f"{hook_name}.py", "python3"),
        (hooks_dir / "src" / f"{hook_name}.ts", "npx tsx"),
    )
    for script, interpreter in candidates:
        if script.exists():
            return f"{interpreter} {shlex.quote(str(script))}"
    return rewritten


def _extract_json(stdout: str) -> Any:
    text = stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Most legacy hooks print one JSON object, but a few also print a status
    # line.  Prefer the last decodable object-shaped line.
    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if not candidate or candidate[0] not in "[{":
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def normalize_output(stdout: str, event: str) -> dict[str, Any]:
    """Convert Claude/legacy hook output to Codex's output schema."""

    parsed = _extract_json(stdout)
    if parsed is None:
        text = stdout.strip()
        if not text:
            return {}
        if event in CONTEXT_EVENTS:
            return {
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": text[-12000:],
                }
            }
        if event == "Stop":
            return {"systemMessage": text[-12000:]}
        return {}

    if isinstance(parsed, str):
        return normalize_output(parsed, event)
    if not isinstance(parsed, dict):
        return {}

    result = parsed.get("result")
    output: dict[str, Any] = {}
    for key in ("continue", "stopReason", "suppressOutput", "systemMessage", "decision", "reason"):
        if key in parsed:
            output[key] = parsed[key]

    specific = parsed.get("hookSpecificOutput")
    if isinstance(specific, dict):
        specific = copy.deepcopy(specific)
        specific["hookEventName"] = event
        output["hookSpecificOutput"] = specific
    elif any(key in parsed for key in ("additionalContext", "permissionDecision", "updatedInput")):
        specific = {"hookEventName": event}
        for key in (
            "additionalContext",
            "permissionDecision",
            "permissionDecisionReason",
            "updatedInput",
        ):
            if key in parsed:
                specific[key] = parsed[key]
        output["hookSpecificOutput"] = specific

    if result == "block":
        output["decision"] = "block"
        if not str(output.get("reason") or "").strip():
            output["reason"] = "Continuous Claude hook requested a block"
    elif result == "continue":
        output.setdefault("continue", True)

    # Claude's old hook format sometimes emits a top-level additionalContext.
    if "additionalContext" in parsed and "hookSpecificOutput" not in output:
        output["hookSpecificOutput"] = {
            "hookEventName": event,
            "additionalContext": str(parsed["additionalContext"]),
        }

    if output.get("decision") == "block" and not str(output.get("reason") or "").strip():
        output["reason"] = "Continuous Claude hook requested a block"
    return output


def _safe_session_name(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id)[:96] or "unknown"


def run_hook(raw_input: Any, hook: dict[str, Any], deployment_root: Path) -> dict[str, Any]:
    event = str(hook.get("event") or "")
    data = adapt_input(raw_input, event)
    if not matches_source_matcher(hook.get("matcher"), data, event):
        return {}
    if _forbidden_session_access(data, event):
        return _forbidden_access_output()

    command = hook.get("command")
    if not isinstance(command, str) or not command.strip():
        return {}
    command = rewrite_command(command, deployment_root)
    compact_handoff = _codex_compact_handoff(data) if event == "PreCompact" else None
    session_id = str(data.get("session_id") or "unknown")
    project_dir = Path(str(data.get("cwd") or os.getcwd())).expanduser()
    if not project_dir.is_dir():
        project_dir = Path.cwd()

    runtime_dir = deployment_root / ".runtime"
    runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    env_file = runtime_dir / f"claude-env-{_safe_session_name(session_id)}.sh"
    env = os.environ.copy()
    runtime_env = _load_dotenv(deployment_root / ".env")
    runtime_env.update(_load_dotenv(deployment_root / "opc" / ".env"))
    for key, value in runtime_env.items():
        env.setdefault(key, value)
    # Continuous Claude's bundled hooks locate their global assets through
    # ``Path.home()/~/.claude``.  Point only the hook subprocess at the
    # isolated deployment so the user's real ~/.claude data and credentials
    # remain untouched; retain the original value for hooks that need it.
    env["CODEX_CONTINUOUS_ORIGINAL_HOME"] = env.get("HOME", "")
    env["HOME"] = str(deployment_root)
    env["USERPROFILE"] = str(deployment_root)
    bundled_bin = deployment_root / "bin"
    if bundled_bin.is_dir():
        env["PATH"] = str(bundled_bin) + os.pathsep + env.get("PATH", "")
    env.update(
        {
            "CLAUDE_PROJECT_DIR": str(project_dir),
            "CLAUDE_SESSION_ID": session_id,
            "CLAUDE_HOOK_EVENT_NAME": event,
            "CLAUDE_OPC_DIR": str(
                deployment_root / "opc"
                if (deployment_root / "opc").is_dir()
                else deployment_root / ".claude"
            ),
            "CLAUDE_CONTINUOUS_ROOT": str(deployment_root),
            "CLAUDE_TRANSCRIPT_PATH": str(data.get("transcript_path") or ""),
            "CLAUDE_ENV_FILE": str(env_file),
            "COORDINATION_SESSION_ID": session_id,
            "CODEX_CONTINUOUS_ROOT": str(deployment_root),
            "CODEX_CONTINUOUS_DEPLOYMENT": str(deployment_root),
        }
    )
    if data.get("agent_id"):
        env["CLAUDE_AGENT_ID"] = str(data["agent_id"])
    if data.get("turn_id"):
        env["CODEX_TURN_ID"] = str(data["turn_id"])

    payload = json.dumps(data, ensure_ascii=False)
    timeout_value = hook.get("timeout")
    try:
        timeout = max(1.0, min(float(timeout_value), 600.0)) if timeout_value else 30.0
    except (TypeError, ValueError):
        timeout = 30.0

    try:
        completed = subprocess.run(
            ["/bin/sh", "-c", command],
            input=payload,
            text=True,
            capture_output=True,
            cwd=str(project_dir),
            env=env,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _log(f"{event}/{hook.get('id', 'hook')} timed out after {timeout:g}s")
        if _strict():
            raise
        result = {}
        if compact_handoff:
            result["systemMessage"] = f"[PreCompact:auto] Created YAML handoff: {compact_handoff}"
        return result
    except OSError as error:
        _log(f"{event}/{hook.get('id', 'hook')} failed to start: {error}")
        if _strict():
            raise
        result = {}
        if compact_handoff:
            result["systemMessage"] = f"[PreCompact:auto] Created YAML handoff: {compact_handoff}"
        return result

    if completed.returncode != 0 and not completed.stdout.strip():
        detail = completed.stderr.strip().splitlines()[-1:] if completed.stderr else []
        if detail:
            _log(f"{event}/{hook.get('id', 'hook')} exited {completed.returncode}: {detail[0]}")
        else:
            _log(f"{event}/{hook.get('id', 'hook')} exited {completed.returncode}")
        if _strict():
            raise RuntimeError(f"hook exited with {completed.returncode}")
        result = {}
        if compact_handoff:
            result["systemMessage"] = f"[PreCompact:auto] Created YAML handoff: {compact_handoff}"
        return result

    output = normalize_output(completed.stdout, event)
    if compact_handoff:
        message = f"[PreCompact:auto] Created YAML handoff: {compact_handoff}"
        output["systemMessage"] = "\n".join(
            value for value in (output.get("systemMessage"), message) if value
        )
    return output


def _merge_event_outputs(outputs: list[dict[str, Any]], event: str) -> dict[str, Any]:
    """Merge sequential hook results into one Codex response."""

    merged: dict[str, Any] = {}
    contexts: list[str] = []
    messages: list[str] = []
    for output in outputs:
        if not isinstance(output, dict):
            continue
        if output.get("decision") == "block":
            return output
        specific = output.get("hookSpecificOutput")
        if isinstance(specific, dict):
            if specific.get("permissionDecision") == "deny":
                return output
            context = specific.get("additionalContext")
            if context:
                contexts.append(str(context))
        if output.get("systemMessage"):
            messages.append(str(output["systemMessage"]))
        for key in ("continue", "stopReason", "suppressOutput"):
            if key in output:
                merged[key] = output[key]
    if contexts:
        merged["hookSpecificOutput"] = {
            "hookEventName": event,
            "additionalContext": "\n\n".join(contexts)[-24000:],
        }
    if messages:
        merged["systemMessage"] = "\n\n".join(messages)[-12000:]
    return merged


def run_event(raw_input: Any, event: str, manifest_path: Path, deployment_root: Path) -> dict[str, Any]:
    """Run every manifest hook for one event in one bridge process."""

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    hooks = manifest.get("hooks", {})
    outputs: list[dict[str, Any]] = []
    for hook_id, hook in hooks.items():
        if not isinstance(hook, dict) or hook.get("event") != event:
            continue
        current = dict(hook)
        current.setdefault("id", hook_id)
        output = run_hook(raw_input, current, deployment_root)
        if output:
            outputs.append(output)
            if output.get("decision") == "block":
                break
            specific = output.get("hookSpecificOutput")
            if isinstance(specific, dict) and specific.get("permissionDecision") == "deny":
                break
    return _merge_event_outputs(outputs, event)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hook-id", required=True)
    parser.add_argument("--event", choices=sorted(CONTEXT_EVENTS | TOOL_EVENTS | {"PermissionRequest", "PreCompact", "PostCompact", "Stop", "SessionEnd", "SubagentStart", "SubagentStop", "Interrupt"}), default=None)
    parser.add_argument("--manifest", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        manifest_path = (args.manifest or _manifest_path()).expanduser().resolve()
        raw = json.loads(sys.stdin.read() or "{}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        deployment_root = Path(manifest.get("deployment_root") or manifest_path.parent).expanduser()
        if args.event:
            _write_output(run_event(raw, args.event, manifest_path, deployment_root))
        else:
            hook, deployment_root = load_hook(manifest_path, args.hook_id)
            hook.setdefault("id", args.hook_id)
            _write_output(run_hook(raw, hook, deployment_root))
        return 0
    except Exception as error:  # noqa: BLE001  # fail-open unless explicitly diagnosing
        _log(f"bridge error: {error}")
        if _strict():
            return 1
        _write_output({})
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
