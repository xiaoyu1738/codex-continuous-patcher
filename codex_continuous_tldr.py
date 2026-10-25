#!/usr/bin/env python3
"""Expose the original Python ``llm-tldr`` CLI with Codex-safe routing."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

_DEPLOYMENT_ROOT = Path(__file__).resolve().parent.parent
if str(_DEPLOYMENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_DEPLOYMENT_ROOT))

from codex_continuous_semantic_index import build_index, search as semantic_search
from codex_continuous_semantic_runtime import semantic_environment, semantic_project_guard


def _python() -> str:
    candidate = _DEPLOYMENT_ROOT / "tldr-venv" / "bin" / "python"
    return str(candidate) if candidate.is_file() else sys.executable


def _project(args: list[str]) -> tuple[Path, list[str]]:
    remaining = list(args)
    for index, value in enumerate(remaining):
        if value in {"--project", "-p"} and index + 1 < len(remaining):
            project = Path(remaining[index + 1]).expanduser().resolve()
            del remaining[index : index + 2]
            return project, remaining
        if value.startswith("--project="):
            project = Path(value.split("=", 1)[1]).expanduser().resolve()
            del remaining[index]
            return project, remaining
    return Path.cwd().resolve(), remaining


def _language(args: list[str]) -> tuple[str, list[str]]:
    """Extract the legacy language flag used by converted skills."""

    remaining = list(args)
    for index, value in enumerate(remaining):
        if value in {"--lang", "--language", "-l"} and index + 1 < len(remaining):
            language = remaining[index + 1]
            del remaining[index : index + 2]
            return language, remaining
        for prefix in ("--lang=", "--language="):
            if value.startswith(prefix):
                language = value.split("=", 1)[1]
                del remaining[index]
                return language, remaining
    return "python", remaining


def _model(args: list[str]) -> str:
    for flag in ("--model", "-m"):
        if flag in args:
            index = args.index(flag)
            if index + 1 < len(args):
                return args[index + 1]
    return os.environ.get("CODEX_CONTINUOUS_TLDR_MODEL", "all-MiniLM-L6-v2")


def _requested_model(args: list[str]) -> str | None:
    for flag in ("--model", "-m"):
        if flag in args:
            index = args.index(flag)
            if index + 1 < len(args):
                return args[index + 1]
    configured = os.environ.get("CODEX_CONTINUOUS_TLDR_MODEL")
    return configured or None


def _upstream(args: list[str], project: Path | None = None) -> int:
    cwd = project or Path.cwd()
    result = subprocess.run(
        [_python(), "-m", "tldr.cli", *args],
        cwd=str(cwd),
        env=semantic_environment(),
        check=False,
    )
    return result.returncode


def _semantic(args: list[str]) -> int:
    if not args or args[0] not in {"search", "index"}:
        print("Usage: tldr semantic index <path> | tldr semantic search <query> [--path <path>]", file=sys.stderr)
        return 2
    action, rest = args[0], args[1:]
    project, rest = _project(rest)
    model = _model(rest)
    language = "all"
    if "--lang" in rest:
        index = rest.index("--lang")
        if index + 1 < len(rest):
            language = rest[index + 1]
    if action == "index":
        if rest and not rest[0].startswith("-") and Path(rest[0]).expanduser().is_dir():
            project = Path(rest[0]).expanduser().resolve()
        result = build_index(project, None, model=model, language=language)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "ok" else 1

    if not rest:
        print("Usage: tldr semantic search <query> [--path <path>]", file=sys.stderr)
        return 2
    query = rest[0]
    if len(rest) > 1 and not rest[1].startswith("-") and Path(rest[1]).expanduser().is_dir():
        project = Path(rest[1]).expanduser().resolve()
    if "--path" in rest:
        index = rest.index("--path")
        if index + 1 < len(rest):
            project = Path(rest[index + 1]).expanduser().resolve()
    top_k = 5
    if "--k" in rest:
        index = rest.index("--k")
        if index + 1 < len(rest):
            try:
                top_k = max(1, min(int(rest[index + 1]), 100))
            except ValueError:
                pass
    guard = semantic_project_guard(project)
    if not guard.get("allowed"):
        print(json.dumps({"status": "skipped", "error": guard.get("reason")}, ensure_ascii=False))
        return 2
    result = semantic_search(
        project,
        None,
        query,
        top_k=top_k,
        model=_requested_model(rest),
        build_if_missing=False,
    )
    if result is None:
        print("Semantic search unavailable; normal text search remains available.", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _daemon(args: list[str]) -> int:
    project, rest = _project(args[1:])
    helper = _DEPLOYMENT_ROOT / "codex_continuous_tldr_daemon.py"
    action = args[0] if args else "status"
    flags = {"start": "--start", "stop": "--stop", "status": "--status"}
    if action in flags:
        result = subprocess.run(
            [_python(), str(helper), flags[action], "--project", str(project), "--deployment", str(_DEPLOYMENT_ROOT)],
            cwd=str(project),
            env=semantic_environment(),
            text=True,
            check=False,
        )
        return result.returncode
    return _upstream(["daemon", *args], project)


def main() -> int:
    args = sys.argv[1:]
    if not args:
        return _upstream(["--help"])
    if args[0] == "semantic":
        return _semantic(args[1:])
    if args[0] == "daemon":
        return _daemon(args[1:])
    project, remaining = _project(args[1:])
    return _upstream([args[0], *remaining], project)


if __name__ == "__main__":
    raise SystemExit(main())
