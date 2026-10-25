#!/usr/bin/env python3
"""Guard and selectively route Codex shell/exec commands.

Codex can express file exploration as shell commands instead of Claude's
dedicated Read/Grep tools.  This hook handles only unambiguous cases:

* block reads from ``~/.codex/sessions``;
* route a single source-file ``cat/head/tail/sed`` to TLDR extract when it
  succeeds;
* pass shell searches through the normal semantic search router;
* fail open for every other command.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import codex_continuous_bridge as bridge
import codex_continuous_search_router as search_router


SESSION_PATH = re.compile(r"(?:^|[\s'\"])(?:~|\$HOME|/home/[^\s/'\"]+)/\.codex/sessions(?:[/\s'\"]|$)")
READ_COMMANDS = {"cat", "head", "tail", "sed"}
SEARCH_COMMANDS = {"rg", "grep", "git-grep"}
CODE_SUFFIXES = {".c", ".cc", ".cpp", ".go", ".h", ".hpp", ".java", ".js", ".jsx", ".py", ".rb", ".rs", ".sh", ".swift", ".ts", ".tsx"}


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return []


def _single_source_path(tokens: list[str]) -> Path | None:
    if not tokens:
        return None
    executable = Path(tokens[0]).name
    if executable not in READ_COMMANDS:
        return None
    candidates = [token for token in tokens[1:] if not token.startswith("-") and not re.fullmatch(r"\d*,?\d*[a-z]*", token)]
    if len(candidates) != 1:
        return None
    path = Path(candidates[0]).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if path.is_file() and path.suffix.lower() in CODE_SUFFIXES:
        return path.resolve()
    return None


def _search_payload(tokens: list[str], cwd: Path) -> dict[str, Any] | None:
    if not tokens:
        return None
    executable = Path(tokens[0]).name
    if executable == "git" and len(tokens) > 1 and tokens[1] == "grep":
        executable = "git-grep"
        tokens = [tokens[0], *tokens[2:]]
    if executable not in SEARCH_COMMANDS:
        return None
    values = [token for token in tokens[1:] if not token.startswith("-")]
    if not values:
        return None
    pattern = values[0]
    path = values[1] if len(values) > 1 else str(cwd)
    return {
        "tool_name": "Grep",
        "cwd": str(cwd),
        "tool_input": {"pattern": pattern, "path": path},
        "session_id": os.environ.get("CODEX_SESSION_ID", "shell-router"),
    }


def _tldr_extract(path: Path, cwd: Path) -> dict[str, Any] | None:
    deployment = Path(__file__).resolve().parent
    helper = deployment / "codex_continuous_tldr_daemon.py"
    if not helper.is_file():
        return None
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(helper),
                "--project",
                str(cwd),
                "--deployment",
                str(deployment),
                "--request-json",
                json.dumps({"cmd": "extract", "file": str(path)}, ensure_ascii=False),
            ],
            cwd=str(cwd),
            text=True,
            capture_output=True,
            check=False,
            timeout=12,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return response if isinstance(response, dict) and response.get("status") == "ok" else None


def route(payload: dict[str, Any]) -> dict[str, Any]:
    tool_name = bridge.canonical_tool_name(payload.get("tool_name") or payload.get("name"))
    if tool_name != "Bash":
        return {}
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return {}
    command = tool_input.get("command") or tool_input.get("cmd") or tool_input.get("shell_command")
    if not isinstance(command, str) or not command.strip():
        return {}
    if SESSION_PATH.search(command) or "/.codex/sessions/" in command:
        return _deny("Blocked: reading /home/.../.codex/sessions history is disabled by Continuous Codex.")

    cwd = Path(str(payload.get("cwd") or os.getcwd())).expanduser().resolve()
    tokens = _tokens(command)
    search = _search_payload(tokens, cwd)
    if search is not None:
        routed = search_router.route(search)
        if routed:
            return routed
        return {}

    source_path = _single_source_path(tokens)
    if source_path is None:
        return {}
    response = _tldr_extract(source_path, cwd)
    if response is None:
        return {}
    result = response.get("result", response)
    return _deny(
        "TLDR structured read intercepted the full-source shell command.\n\n"
        + json.dumps(result, ensure_ascii=False, indent=2)[:12000]
        + "\n\nUse a focused line-range command if exact source text is required."
    )


def main() -> int:
    try:
        value = json.loads(sys.stdin.read() or "{}")
        output = route(value) if isinstance(value, dict) else {}
    except (OSError, TypeError, ValueError):
        output = {}
    sys.stdout.write(json.dumps(output, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
