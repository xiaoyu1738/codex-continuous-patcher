#!/usr/bin/env python3
"""Codex bridge to the original Python ``llm-tldr`` daemon.

Continuous-Claude-v3 speaks newline-delimited JSON over the TLDR Unix socket.
The upstream Python package already implements that protocol, including the
semantic action and dirty-file reindexing. This adapter only starts the
upstream daemon, forwards requests, and normalizes a few legacy response
fields used by the Claude hooks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from codex_continuous_semantic_runtime import semantic_environment, semantic_lock, semantic_project_guard


SOCKET_VERSION = "python-llm-tldr-1.0"
DEFAULT_SEMANTIC_MODEL = "all-MiniLM-L6-v2"


def _request_spec(payload: dict[str, Any]) -> tuple[str, dict[str, Any], bool] | None:
    """Compatibility mapper retained for converted legacy tests/skills.

    The live backend forwards JSON to the Python llm-tldr daemon directly;
    this helper preserves the old route description API for callers that used
    the previous adapter as a library.
    """

    command = str(payload.get("cmd") or "")
    if command == "semantic":
        if payload.get("action") in {None, "search"}:
            return "/search", {
                "pattern": str(payload.get("query") or ""),
                "max_results": int(payload.get("k") or 10),
            }, True
        return None
    routes = {
        "ping": "/ping",
        "status": "/status",
        "search": "/search",
        "impact": "/impact",
        "extract": "/extract",
        "dead": "/dead",
        "arch": "/arch",
        "cfg": "/cfg",
        "dfg": "/dfg",
        "slice": "/slice",
        "calls": "/calls",
        "tree": "/tree",
        "structure": "/structure",
        "context": "/context",
        "imports": "/imports",
        "importers": "/importers",
    }
    route = routes.get(command)
    if route is None:
        return None
    fields = {
        "search": ("pattern", "extensions", "context_lines", "max_results", "max_files"),
        "impact": ("func", "depth", "file", "language"),
        "extract": ("file", "language"),
        "dead": ("entry_points", "language"),
        "arch": ("language",),
        "cfg": ("file", "function", "language"),
        "dfg": ("file", "function", "language"),
        "slice": ("file", "function", "line", "direction", "variable", "language"),
        "calls": ("language",),
        "tree": ("extensions", "exclude_hidden"),
        "structure": ("language", "max_results"),
        "context": ("entry", "depth", "language", "include_docstrings", "file"),
        "imports": ("file", "language"),
        "importers": ("module", "language"),
        "ping": (),
        "status": (),
    }[command]
    return route, {key: payload[key] for key in fields if key in payload}, False


def _paths(project: Path) -> tuple[Path, Path, Path]:
    digest = hashlib.md5(str(project.expanduser().resolve()).encode()).hexdigest()[:8]
    temporary = Path(tempfile.gettempdir())
    return (
        temporary / f"tldr-{digest}.sock",
        temporary / f"tldr-{digest}.pid",
        temporary / f"tldr-{digest}-v{SOCKET_VERSION}.sock",
    )


def _deployment_python(deployment: Path) -> Path:
    candidate = deployment / "tldr-venv" / "bin" / "python"
    if candidate.is_file():
        return candidate
    configured = os.environ.get("CODEX_CONTINUOUS_TLDR_PYTHON")
    if configured and Path(configured).is_file():
        return Path(configured)
    return Path(sys.executable)


def _send(socket_path: Path, payload: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(socket_path))
        client.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
        chunks = bytearray()
        while b"\n" not in chunks:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.extend(chunk)
    if not chunks:
        raise ConnectionError("llm-tldr daemon returned no response")
    response = json.loads(bytes(chunks).split(b"\n", 1)[0].decode("utf-8"))
    return response if isinstance(response, dict) else {"status": "error"}


def _run_upstream_cli(
    project: Path,
    deployment: Path,
    args: list[str],
    timeout: float = 900.0,
) -> tuple[int, str, str]:
    python = _deployment_python(deployment)
    try:
        result = subprocess.run(
            [str(python), "-m", "tldr.cli", *args],
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


def _start_upstream(project: Path, deployment: Path) -> int:
    socket_path = _paths(project)[0]
    if socket_path.exists():
        try:
            if _send(socket_path, {"cmd": "ping"}, timeout=0.5).get("status") == "ok":
                return 0
        except (OSError, ValueError, TimeoutError, ConnectionError):
            socket_path.unlink(missing_ok=True)

    python = _deployment_python(deployment)
    try:
        process = subprocess.Popen(
            [str(python), "-m", "tldr.daemon", str(project)],
            cwd=str(project),
            env=semantic_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return 1

    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if socket_path.exists():
            try:
                if _send(socket_path, {"cmd": "ping"}, timeout=0.5).get("status") == "ok":
                    return 0
            except (OSError, ValueError, TimeoutError, ConnectionError):
                pass
        time.sleep(0.1)
    return 1


def _stop_upstream(project: Path, deployment: Path) -> int:
    socket_path = _paths(project)[0]
    if socket_path.exists():
        try:
            _send(socket_path, {"cmd": "shutdown"}, timeout=5)
            return 0
        except (OSError, ValueError, TimeoutError, ConnectionError):
            pass
    python = _deployment_python(deployment)
    try:
        subprocess.run(
            [str(python), "-m", "tldr.daemon", str(project), "--stop"],
            cwd=str(project),
            env=semantic_environment(),
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1
    return 0


def _semantic_index(project: Path, deployment: Path, payload: dict[str, Any]) -> dict[str, Any]:
    guard = semantic_project_guard(project)
    if not guard.get("allowed"):
        return {"status": "skipped", "error": guard.get("reason", "project is too large"), "guard": guard}
    language = str(payload.get("language") or "all")
    model = str(
        payload.get("model")
        or os.environ.get("CODEX_CONTINUOUS_TLDR_MODEL")
        or DEFAULT_SEMANTIC_MODEL
    )
    with semantic_lock(project, timeout=10.0) as acquired:
        if not acquired:
            return {"status": "busy", "error": "semantic index is already being built"}
        code, stdout, stderr = _run_upstream_cli(
            project,
            deployment,
            ["semantic", "index", str(project), "--lang", language, "--model", model],
        )
    index_file = project / ".tldr" / "cache" / "semantic" / "index.faiss"
    metadata_file = project / ".tldr" / "cache" / "semantic" / "metadata.json"
    if code != 0 or not index_file.is_file() or not metadata_file.is_file():
        return {"status": "error", "error": stderr.strip() or stdout.strip() or "llm-tldr semantic index failed"}
    return {
        "status": "ok",
        "index_path": str(index_file),
        "metadata_path": str(metadata_file),
        "model": model,
        "output": stdout.strip(),
    }


def _normalize_response(command: str, response: dict[str, Any]) -> dict[str, Any]:
    result = response.get("result")
    if command == "search" and isinstance(result, dict):
        response.setdefault("results", result.get("matches", []))
    if command == "impact" and isinstance(result, dict):
        response.setdefault("callers", result.get("callers", []))
    return response


def request(project: Path, deployment: Path, payload: dict[str, Any]) -> dict[str, Any]:
    command = str(payload.get("cmd") or "")
    if command == "semantic" and payload.get("action") == "index":
        return _semantic_index(project, deployment, payload)

    if _start_upstream(project, deployment) != 0:
        return {"status": "unavailable", "error": "llm-tldr daemon is unavailable"}
    try:
        response = _send(_paths(project)[0], payload, timeout=900 if command == "semantic" else 30)
    except (OSError, ValueError, TimeoutError, ConnectionError) as error:
        return {"status": "unavailable", "error": type(error).__name__}
    return _normalize_response(command, response)


def start(project: Path, deployment: Path) -> int:
    return _start_upstream(project.resolve(), deployment.resolve())


def stop(project: Path, deployment: Path) -> int:
    return _stop_upstream(project.resolve(), deployment.resolve())


def status(project: Path) -> dict[str, Any]:
    socket_path = _paths(project.resolve())[0]
    if not socket_path.exists():
        return {"status": "stopped"}
    try:
        return _send(socket_path, {"cmd": "status"}, timeout=3)
    except (OSError, ValueError, TimeoutError, ConnectionError):
        return {"status": "stopped"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--deployment", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--request-json")
    args = parser.parse_args()
    project = args.project.expanduser().resolve()
    deployment = args.deployment.expanduser().resolve()
    if args.start:
        return start(project, deployment)
    if args.stop:
        return stop(project, deployment)
    if args.status:
        print(json.dumps(status(project), ensure_ascii=False))
        return 0
    if args.request_json:
        try:
            response = request(project, deployment, json.loads(args.request_json))
        except (OSError, ValueError, TypeError) as error:
            print(json.dumps({"status": "error", "error": type(error).__name__}))
            return 1
        print(json.dumps(response, ensure_ascii=False))
        return 0 if response.get("status") not in {"error", "unavailable", "skipped", "busy"} else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
