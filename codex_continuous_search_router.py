#!/usr/bin/env python3
"""Codex-compatible smart search hook.

The upstream hook was written for Claude Code and uses the Python
``llm-tldr`` FAISS index at ``.tldr/cache/semantic/index.faiss``.
This adapter keeps the useful routing rules while preserving ordinary Grep:

* exact identifiers, paths, and regex-like patterns pass through unchanged;
* structural queries receive the existing TLDR/AST guidance;
* natural-language queries call the deployed ``llm-tldr`` daemon;
* a missing binary, timeout, malformed result, or empty result fails open so
  the native Grep tool can still answer the request.

It emits Codex's PreToolUse schema directly.  The bridge also understands this
schema, so the same script works when called through a generated hook entry.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from codex_continuous_semantic_runtime import semantic_project_guard
from codex_continuous_semantic_index import search as persistent_semantic_search


CONTEXT_DIR = Path("/tmp/claude-search-context")
# llm-tldr 1.5.2 ships these model keys. Use the smaller supported model for
# automatic searches; an unrelated model name would be incompatible with the
# dimensions stored in an existing FAISS index.
DEFAULT_SEMANTIC_MODEL = "all-MiniLM-L6-v2"


def classify_query(pattern: str) -> str:
    """Classify a Grep pattern without changing literal-search semantics."""

    structural_patterns = (
        r"^(class|function|def|async\s+def|const|let|var|interface|type|export)\s+\w+",
        r"^(import|from|require)\s",
        r"^\w+\s*\([^)]*\)",
        r"^async\s+(function|def)",
        r"^@\w+",
    )
    if any(re.search(expression, pattern) for expression in structural_patterns):
        return "structural"
    if "\\" in pattern or "[" in pattern or re.search(r"\([^)]*\|", pattern):
        return "literal"
    if (
        re.fullmatch(r"[A-Z][a-zA-Z0-9]*", pattern)
        or re.fullmatch(r"[a-z_][a-z0-9_]*", pattern)
        or re.fullmatch(r"[A-Z_][A-Z0-9_]*", pattern)
    ):
        return "literal"
    if "/" in pattern or re.search(r"\.(ts|tsx|py|js|jsx|go|rs|md)(?:$|\s)", pattern):
        return "literal"

    words = pattern.split()
    if len(words) <= 2 and not re.match(r"^(how|what|where|why|when|find|show|list)\b", pattern, re.I):
        return "literal"
    semantic_patterns = (
        r"^(how|what|where|why|when|which)\s",
        r"\?$",
        r"^(find|show|list|get|explain)\s+(all|the|every|any)\b",
        r"\s+(implementation|architecture|flow|pattern|logic|system)$",
    )
    if any(re.search(expression, pattern, re.I) for expression in semantic_patterns):
        return "semantic"
    return "semantic" if len(words) >= 3 else "literal"


def _project_dir(tool_input: dict[str, Any]) -> Path:
    configured = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    project = Path(configured).expanduser().resolve()
    requested = tool_input.get("path")
    if isinstance(requested, str) and requested.strip():
        candidate = Path(requested).expanduser()
        if not candidate.is_absolute():
            candidate = project / candidate
        if candidate.is_dir():
            project = candidate.resolve()
    return project if project.is_dir() else Path.cwd().resolve()


def _deployment_root() -> Path | None:
    for name in ("CODEX_CONTINUOUS_DEPLOYMENT", "CODEX_CONTINUOUS_ROOT"):
        value = os.environ.get(name)
        if value:
            path = Path(value).expanduser().resolve()
            if path.is_dir():
                return path
    return None


def semantic_binary() -> Path | None:
    root = _deployment_root()
    if root:
        candidate = root / "bin" / "tldr"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    configured = os.environ.get("CODEX_CONTINUOUS_TLDR_SEMANTIC")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / "tldr"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _store_context(session_id: str, query_type: str, pattern: str) -> None:
    try:
        CONTEXT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id)[:96] or "default"
        (CONTEXT_DIR / f"{safe_id}.json").write_text(
            json.dumps(
                {
                    "timestamp": int(__import__("time").time() * 1000),
                    "queryType": query_type,
                    "pattern": pattern,
                    "target": None,
                    "targetType": "unknown",
                    "suggestedLayers": (
                        ["ast", "call_graph", "cfg"]
                        if query_type == "semantic"
                        else ["ast", "call_graph"]
                    ),
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _semantic_search(query: str, project: Path, top_k: int = 5) -> dict[str, Any] | None:
    binary = semantic_binary()
    if binary is None:
        return None
    guard = semantic_project_guard(project)
    if not guard.get("allowed"):
        return None
    persistent = persistent_semantic_search(
        project,
        binary,
        query,
        top_k=top_k,
        # Let llm-tldr read the model recorded in metadata for an existing
        # index. The default is used by the automatic builder, not forced on
        # every query (which would break explicitly built BGE indexes).
        model=os.environ.get("CODEX_CONTINUOUS_TLDR_MODEL") or None,
        build_if_missing=False,
    )
    if persistent is not None:
        return persistent
    # Never fall back to a second full-project scan from an automatic hook.
    # Native Grep remains available when the persistent build is unavailable.
    return None


def _semantic_output(query: str, payload: dict[str, Any]) -> dict[str, Any]:
    lines = []
    for item in payload["results"][:10]:
        location = f"{item['file']}:{item.get('line') or '?'}"
        function = item.get("function")
        if function and function != "module":
            location += f" ({function})"
        score = item.get("score")
        suffix = f" [{float(score) * 100:.0f}%]" if isinstance(score, (int, float)) else ""
        lines.append(f"- {location}{suffix}")
    reason = (
        "🧠 Semantic search results (llm-tldr/FAISS):\n\n"
        + "\n".join(lines)
        + f"\n\nQuery: {query}\nRead the most relevant file or run a focused TLDR command next."
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _structural_output(pattern: str) -> dict[str, Any]:
    reason = (
        "🎯 Structural search pattern detected. Use TLDR/AST analysis for this query, "
        f"then read the relevant result:\n\n{pattern}"
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def route(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("tool_name") != "Grep":
        return {}
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or not isinstance(tool_input.get("pattern"), str):
        return {}
    pattern = tool_input["pattern"]
    query_type = classify_query(pattern)
    _store_context(str(payload.get("session_id") or "default"), query_type, pattern)
    if query_type == "literal":
        # Preserve ordinary Grep exactly as Codex provides it.
        return {}
    if query_type == "structural":
        return _structural_output(pattern)
    semantic = _semantic_search(pattern, _project_dir(tool_input))
    if semantic and semantic.get("results"):
        return _semantic_output(pattern, semantic)
    # Semantic is optional.  A failed or empty semantic query must never make
    # a normal search unusable; allow the native Grep operation to continue.
    return {}


def main() -> int:
    try:
        raw = json.loads(sys.stdin.read() or "{}")
        output = route(raw) if isinstance(raw, dict) else {}
    except (OSError, ValueError, TypeError):
        output = {}
    sys.stdout.write(json.dumps(output, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
