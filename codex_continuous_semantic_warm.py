#!/usr/bin/env python3
"""Schedule safe, detached TLDR warming for a Codex session.

The upstream SessionStart hook warms the structural call-graph cache.  It does
not build the FAISS semantic index.  Codex adds the missing convenience here:
when a new session opens inside a small Git worktree, a single detached worker
builds the upstream ``llm-tldr`` index while the session remains responsive.

The hook itself never waits for PyTorch or reads the whole project.  A second
session sees the warm lock and exits, and semantic search continues to fail
open until the normal upstream artifacts are present.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex_continuous_semantic_index import build_index, index_path, metadata_path  # noqa: E402
from codex_continuous_semantic_runtime import (  # noqa: E402
    semantic_environment,
    semantic_project_guard,
)


WARM_LOCK_NAME = ".semantic-warm.lock"
STRUCTURAL_LOCK_NAME = ".structural-warm.lock"
STATUS_NAME = ".semantic-warm.json"
DEFAULT_MODEL = "all-MiniLM-L6-v2"
MAX_CACHE_AGE_SECONDS = 24 * 60 * 60


def _source_allows_warm(source: object) -> bool:
    return str(source or "startup") in {"startup", "resume"}


def _git_root(path: Path) -> Path | None:
    """Return the Git worktree root, or None outside a worktree."""

    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            text=True,
            capture_output=True,
            check=False,
            timeout=1.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return Path(value).resolve() if value else None


def _project_from_input(payload: dict[str, object]) -> Path | None:
    raw = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    try:
        path = Path(str(raw)).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    if not path.is_dir():
        return None
    return _git_root(path)


def _index_ready(project: Path) -> bool:
    return index_path(project).is_file() and metadata_path(project).is_file()


def _status_path(project: Path) -> Path:
    return project / ".tldr" / "cache" / "semantic" / STATUS_NAME


def _write_status(project: Path, status: str, **extra: object) -> None:
    path = _status_path(project)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = {"status": status, "updated_at": time.time(), **extra}
        path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        # Status is observability only; it must never break SessionStart.
        pass


@contextmanager
def _exclusive_file_lock(project: Path, name: str) -> Iterator[object | None]:
    """Acquire a project lock without waiting."""

    lock_path = project / ".tldr" / "cache" / "semantic" / name
    handle = None
    try:
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle = lock_path.open("a+")
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ImportError, BlockingIOError, OSError):
            handle.close()
            handle = None
        yield handle
    finally:
        if handle is not None:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            handle.close()


def _deployment() -> Path:
    configured = os.environ.get("CODEX_CONTINUOUS_DEPLOYMENT") or os.environ.get(
        "CODEX_CONTINUOUS_ROOT"
    )
    if configured and Path(configured).is_dir():
        return Path(configured).expanduser().resolve()
    return ROOT


def _python_for(deployment: Path) -> Path:
    for candidate in (
        deployment / "tldr-venv" / "bin" / "python",
        deployment / "opc" / ".venv" / "bin" / "python",
    ):
        if candidate.is_file():
            return candidate
    return Path(sys.executable)


def _worker_command(project: Path, deployment: Path, model: str) -> list[str]:
    return [
        str(_python_for(deployment)),
        str(Path(__file__).resolve()),
        "--worker",
        "--project",
        str(project),
        "--deployment",
        str(deployment),
        "--model",
        model,
    ]


def _spawn_worker(project: Path, deployment: Path, model: str, lock_handle: object) -> bool:
    """Spawn the semantic worker while passing ownership of the lock into it."""

    fd = getattr(lock_handle, "fileno")()
    environment = semantic_environment()
    environment.update(
        {
            "CODEX_CONTINUOUS_AUTOWARM": "1",
            "CODEX_CONTINUOUS_TLDR_TRACKED_ONLY": "1",
            "TLDR_AUTO_DOWNLOAD": "1",
        }
    )
    try:
        subprocess.Popen(
            _worker_command(project, deployment, model),
            cwd=str(project),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            pass_fds=(fd,),
        )
    except OSError:
        return False
    return True


def _spawn_structural_worker(project: Path, deployment: Path) -> bool:
    """Warm the upstream call-graph cache when it is missing or stale."""

    cache = project / ".tldr" / "cache" / "call_graph.json"
    try:
        if cache.is_file() and time.time() - cache.stat().st_mtime < MAX_CACHE_AGE_SECONDS:
            return False
    except OSError:
        pass

    with _exclusive_file_lock(project, STRUCTURAL_LOCK_NAME) as lock_handle:
        if lock_handle is None:
            return False
        fd = getattr(lock_handle, "fileno")()
        environment = semantic_environment()
        try:
            subprocess.Popen(
                [
                    str(_python_for(deployment)),
                    "-m",
                    "tldr.cli",
                    "warm",
                    str(project),
                    "--lang",
                    "all",
                ],
                cwd=str(project),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
                pass_fds=(fd,),
            )
        except OSError:
            return False
    return True


def _worker(project: Path, deployment: Path, model: str) -> int:
    """Build the upstream FAISS index in the detached process."""

    try:
        os.nice(19)
    except OSError:
        pass

    _write_status(project, "indexing", model=model, pid=os.getpid())
    try:
        result = build_index(
            project,
            deployment / "bin" / "tldr",
            model=model,
            language="all",
        )
        if result.get("status") == "ok":
            _write_status(
                project,
                "ready",
                model=model,
                index_path=str(index_path(project)),
                metadata_path=str(metadata_path(project)),
            )
            return 0
        _write_status(project, "error", model=model, error=result.get("error", "index failed"))
        return 1
    except Exception as error:  # noqa: BLE001 - detached hooks must be fail-safe
        _write_status(project, "error", model=model, error=f"{type(error).__name__}: {error}")
        return 1


def start(payload: dict[str, object]) -> dict[str, object]:
    """Schedule warming and return immediately to the lifecycle hook."""

    if not _source_allows_warm(payload.get("source")):
        return {"status": "skipped", "reason": "session source does not warm indexes"}
    project = _project_from_input(payload)
    if project is None:
        return {"status": "skipped", "reason": "not a Git worktree"}

    deployment = _deployment()
    if _index_ready(project):
        return {
            "status": "ready",
            "project": str(project),
            "structural_started": False,
        }

    model = (os.environ.get("CODEX_CONTINUOUS_TLDR_MODEL") or DEFAULT_MODEL).strip()
    # The guard runs before spawning any PyTorch process.  It is deliberately
    # stricter for automatic work than for an explicit `tldr semantic index`.
    old_tracked = os.environ.get("CODEX_CONTINUOUS_TLDR_TRACKED_ONLY")
    os.environ["CODEX_CONTINUOUS_TLDR_TRACKED_ONLY"] = "1"
    try:
        guard = semantic_project_guard(project)
    finally:
        if old_tracked is None:
            os.environ.pop("CODEX_CONTINUOUS_TLDR_TRACKED_ONLY", None)
        else:
            os.environ["CODEX_CONTINUOUS_TLDR_TRACKED_ONLY"] = old_tracked
    if not guard.get("allowed"):
        return {
            "status": "skipped",
            "project": str(project),
            "guard": guard,
        }
    if not int(guard.get("files") or 0):
        return {
            "status": "skipped",
            "project": str(project),
            "reason": "no tracked source files",
            "guard": guard,
        }

    structural_started = _spawn_structural_worker(project, deployment)

    with _exclusive_file_lock(project, WARM_LOCK_NAME) as lock_handle:
        if lock_handle is None:
            return {
                "status": "busy",
                "project": str(project),
                "structural_started": structural_started,
            }
        _write_status(project, "queued", model=model, guard=guard)
        if not _spawn_worker(project, deployment, model, lock_handle):
            _write_status(project, "error", model=model, error="could not spawn worker")
            return {
                "status": "error",
                "project": str(project),
                "structural_started": structural_started,
            }
    return {
        "status": "started",
        "project": str(project),
        "model": model,
        "files": guard.get("files"),
        "bytes": guard.get("bytes"),
        "structural_started": structural_started,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--project", type=Path)
    parser.add_argument("--deployment", type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args(argv)

    if args.worker:
        project = args.project.expanduser().resolve()
        deployment = (args.deployment or _deployment()).expanduser().resolve()
        return _worker(project, deployment, args.model)

    try:
        raw = json.loads(sys.stdin.read() or "{}")
        result = start(raw if isinstance(raw, dict) else {})
    except (OSError, TypeError, ValueError) as error:
        result = {"status": "error", "error": type(error).__name__}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
