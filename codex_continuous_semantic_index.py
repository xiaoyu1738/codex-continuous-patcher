#!/usr/bin/env python3
"""Thin adapter around the original ``llm-tldr`` semantic index.

Continuous-Claude-v3 uses the Python ``llm-tldr`` package. Its supported
artifact is a FAISS index plus metadata:

    .tldr/cache/semantic/index.faiss
    .tldr/cache/semantic/metadata.json

This module deliberately does not create a second vector format. It only
adds Codex's resource guard, calls the upstream daemon/CLI, and normalizes the
result shape expected by the Codex search hook.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from codex_continuous_semantic_runtime import (
    semantic_environment,
    semantic_lock,
    semantic_project_guard,
)


DEFAULT_MODEL = "all-MiniLM-L6-v2"


def _semantic_dir(project: Path | str) -> Path:
    return Path(project).resolve() / ".tldr" / "cache" / "semantic"


def index_path(project: Path | str) -> Path:
    """Return the upstream FAISS index path."""

    return _semantic_dir(project) / "index.faiss"


def metadata_path(project: Path | str) -> Path:
    return _semantic_dir(project) / "metadata.json"


def _model(model: str | None = None) -> str:
    return (model or os.environ.get("CODEX_CONTINUOUS_TLDR_MODEL") or DEFAULT_MODEL).strip()


def _requested_model(model: str | None = None) -> str | None:
    """Return an explicit model, leaving upstream metadata selection intact."""

    selected = (model or os.environ.get("CODEX_CONTINUOUS_TLDR_MODEL") or "").strip()
    return selected or None


def _deployment_root(binary: Path | None) -> Path:
    configured = os.environ.get("CODEX_CONTINUOUS_DEPLOYMENT") or os.environ.get(
        "CODEX_CONTINUOUS_ROOT"
    )
    if configured and Path(configured).is_dir():
        return Path(configured).expanduser().resolve()
    if binary:
        resolved = binary.expanduser().resolve()
        if resolved.parent.name == "bin":
            return resolved.parent.parent
    return Path(__file__).resolve().parent


def _python(deployment: Path) -> str:
    candidate = deployment / "tldr-venv" / "bin" / "python"
    return str(candidate) if candidate.is_file() else sys.executable


def _helper(deployment: Path) -> Path:
    return deployment / "codex_continuous_tldr_daemon.py"


def _request_via_daemon(
    project: Path,
    deployment: Path,
    payload: dict[str, Any],
    timeout: float = 900.0,
) -> dict[str, Any] | None:
    helper = _helper(deployment)
    if not helper.is_file():
        return None
    command = [
        _python(deployment),
        str(helper),
        "--project",
        str(project),
        "--deployment",
        str(deployment),
        "--request-json",
        json.dumps(payload, ensure_ascii=False),
    ]
    try:
        result = subprocess.run(
            command,
            cwd=str(project),
            env=semantic_environment(),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return response if isinstance(response, dict) else None


def _run_upstream_cli(
    project: Path,
    deployment: Path,
    args: list[str],
    timeout: float = 900.0,
) -> tuple[int, str, str]:
    command = [_python(deployment), "-m", "tldr.cli", *args]
    try:
        result = subprocess.run(
            command,
            cwd=str(project),
            env=semantic_environment(),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, "", type(error).__name__
    return result.returncode, result.stdout, result.stderr


def build_index(
    project: Path,
    binary: Path | None = None,
    model: str | None = None,
    language: str = "all",
) -> dict[str, Any]:
    """Build the upstream FAISS index through the upstream daemon."""

    project = project.resolve()
    guard = semantic_project_guard(project)
    if not guard.get("allowed"):
        return {
            "status": "skipped",
            "error": guard.get("reason", "project is too large"),
            "guard": guard,
        }

    selected_model = _model(model)
    deployment = _deployment_root(binary)
    with semantic_lock(project, timeout=10.0) as acquired:
        if not acquired:
            return {"status": "busy", "error": "semantic index is already being built"}
        # Build through the upstream CLI so the selected model is honored.
        # The upstream daemon's semantic-index handler currently uses its
        # configured default model and does not forward a model field.
        code, stdout, stderr = _run_upstream_cli(
            project,
            deployment,
            [
                "semantic",
                "index",
                str(project),
                "--lang",
                language or "all",
                "--model",
                selected_model,
            ],
        )
        if code != 0:
            return {
                "status": "error",
                "error": stderr.strip() or stdout.strip() or "llm-tldr index failed",
            }
        response = {"status": "ok", "output": stdout.strip()}

    response.setdefault("index_path", str(index_path(project)))
    response.setdefault("metadata_path", str(metadata_path(project)))
    response.setdefault("model", selected_model)
    if response.get("status") == "ok" and not index_path(project).is_file():
        response["status"] = "error"
        response["error"] = "llm-tldr reported success without creating index.faiss"
    return response


def _normalize_results(response: dict[str, Any], query: str, project: Path) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for raw in response.get("results", []):
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        file_path = item.get("file_path") or item.get("file")
        function = item.get("function_name") or item.get("function") or item.get("name")
        line = item.get("line_start") or item.get("line")
        item.setdefault("file", file_path)
        item.setdefault("file_path", file_path)
        item.setdefault("function", function)
        item.setdefault("function_name", function)
        item.setdefault("line", line)
        item.setdefault("line_start", line)
        results.append(item)
    response["results"] = results
    response["query"] = query
    response["index_path"] = str(index_path(project))
    response["semantic_backend"] = "llm-tldr-faiss-daemon"
    return response


def search(
    project: Path,
    binary: Path | None,
    query: str,
    top_k: int = 10,
    threshold: float = 0.0,
    model: str | None = None,
    build_if_missing: bool = True,
) -> dict[str, Any] | None:
    """Search the upstream FAISS index through the long-lived daemon."""

    project = project.resolve()
    guard = semantic_project_guard(project)
    if not guard.get("allowed"):
        return None
    deployment = _deployment_root(binary)
    if build_if_missing and not (index_path(project).is_file() and metadata_path(project).is_file()):
        built = build_index(project, binary, model, language="all")
        if built.get("status") != "ok":
            return None
    if not (index_path(project).is_file() and metadata_path(project).is_file()):
        return None
    payload: dict[str, Any] = {
        "cmd": "semantic",
        "action": "search",
        "query": query,
        "k": max(1, min(int(top_k), 100)),
        "threshold": threshold,
    }
    requested_model = _requested_model(model)
    if requested_model:
        payload["model"] = requested_model
    response = _request_via_daemon(project, deployment, payload, timeout=60.0)
    if response is None or response.get("status") not in {"ok", None}:
        return None
    return _normalize_results(response, query, project)
