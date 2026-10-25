#!/usr/bin/env python3
"""Safety and resource policy shared by Codex semantic-search adapters.

The original Continuous-Claude runtime is the Python ``llm-tldr`` package.
It builds a FAISS index with Sentence Transformers/PyTorch and keeps the
result in ``.tldr/cache/semantic/``.  This module limits the work around that
upstream implementation without replacing its index format or search code.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Linux deployment is the supported target
    fcntl = None  # type: ignore[assignment]


DEFAULT_MAX_CODE_FILES = 4000
DEFAULT_MAX_CODE_BYTES = 96 * 1024 * 1024
DEFAULT_MAX_FILE_BYTES = 200 * 1024
MAX_WALK_ENTRIES = 100_000

CODE_EXTENSIONS = {
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

SKIP_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "target",
    "build",
    "dist",
    "out",
    ".cache",
    "cache",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def semantic_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """Build a low-parallelism environment with a stable user cache."""

    environment = dict(base or os.environ)
    original_home = environment.get("CODEX_CONTINUOUS_ORIGINAL_HOME") or environment.get("HOME")
    if original_home:
        home = Path(original_home).expanduser()
        environment["HOME"] = str(home)
        cache_home = Path(environment.get("XDG_CACHE_HOME", home / ".cache")).expanduser()
        tldr_cache = cache_home / "tldr"
        environment.setdefault("FASTEMBED_CACHE_DIR", str(tldr_cache / "fastembed"))
        environment.setdefault("HF_HOME", str(tldr_cache / "fastembed"))

    # PyTorch/BLAS can multiply the model's working set by the CPU count.
    environment.setdefault("OMP_NUM_THREADS", "2")
    environment.setdefault("MKL_NUM_THREADS", "2")
    environment.setdefault("OPENBLAS_NUM_THREADS", "2")
    environment.setdefault("NUMEXPR_NUM_THREADS", "2")
    environment.setdefault("ORT_INTRA_OP_NUM_THREADS", "2")
    environment.setdefault("ORT_INTER_OP_NUM_THREADS", "1")
    environment.setdefault("RAYON_NUM_THREADS", "2")
    # llm-tldr uses a process pool while extracting code units. Without this
    # cap a SessionStart build can fan out to every CPU before embeddings run.
    environment.setdefault("TLDR_MAX_WORKERS", "2")
    environment.setdefault("TOKENIZERS_PARALLELISM", "false")
    environment.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    environment.setdefault("MALLOC_ARENA_MAX", "2")
    return environment


@contextmanager
def semantic_lock(project: Path, timeout: float = 0.25) -> Iterator[bool]:
    """Serialize semantic work per project and fail open on contention."""

    lock_dir = project / ".tldr" / "cache" / "semantic"
    lock_path = lock_dir / ".semantic.lock"
    handle = None
    acquired = False
    try:
        lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle = lock_path.open("a+")
        if fcntl is None:
            acquired = True
        else:
            deadline = time.monotonic() + max(0.0, timeout)
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.02)
        yield acquired
    finally:
        if handle is not None:
            if acquired and fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()


def _git_code_files(project: Path) -> Iterator[Path] | None:
    git = shutil.which("git")
    if not git or not (project / ".git").exists():
        return None
    try:
        completed = subprocess.run(
            [git, "-C", str(project), "ls-files", "-z"],
            text=False,
            capture_output=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    paths = [item for item in completed.stdout.split(b"\0") if item]

    def iterator() -> Iterator[Path]:
        for raw in paths:
            try:
                relative = Path(os.fsdecode(raw))
            except (OSError, UnicodeDecodeError):
                continue
            if relative.suffix.lower() in CODE_EXTENSIONS:
                yield project / relative

    return iterator()


def _walk_code_files(project: Path) -> Iterator[Path]:
    visited = 0
    for root, directories, files in os.walk(project, topdown=True, followlinks=False):
        directories[:] = [name for name in directories if name not in SKIP_DIRECTORIES]
        for name in files:
            visited += 1
            if visited > MAX_WALK_ENTRIES:
                return
            path = Path(root) / name
            if path.suffix.lower() in CODE_EXTENSIONS:
                yield path


def _tldr_ignore_patterns(project: Path) -> list[str]:
    ignore_file = project / ".tldrignore"
    if not ignore_file.is_file():
        return []
    patterns: list[str] = []
    try:
        lines = ignore_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        pattern = line.strip()
        if not pattern or pattern.startswith("#") or pattern.startswith("!"):
            continue
        patterns.append(pattern.rstrip("/"))
    return patterns


def _ignored(relative: Path, patterns: list[str]) -> bool:
    value = relative.as_posix()
    for pattern in patterns:
        if fnmatch.fnmatchcase(value, pattern) or relative.match(pattern):
            return True
        if "/" not in pattern and any(fnmatch.fnmatchcase(part, pattern) for part in relative.parts):
            return True
        if value == pattern or value.startswith(pattern + "/"):
            return True
    return False


def code_files(project: Path) -> Iterator[Path]:
    """Return source files within the safety budget.

    ``llm-tldr`` follows ``.tldrignore`` rather than requiring a clean Git
    index.  That matters for Codex workspaces because newly-created files are
    commonly untracked while the agent is working.  Set
    ``CODEX_CONTINUOUS_TLDR_TRACKED_ONLY=1`` when a deployment explicitly
    wants the stricter Git-only policy.
    """

    patterns = _tldr_ignore_patterns(project)
    tracked = _git_code_files(project) if os.environ.get("CODEX_CONTINUOUS_TLDR_TRACKED_ONLY") in {"1", "true", "yes"} else None
    source_files = tracked if tracked is not None else _walk_code_files(project)

    def filtered() -> Iterator[Path]:
        for path in source_files:
            try:
                relative = path.relative_to(project)
                if _ignored(relative, patterns):
                    continue
                if path.stat().st_size > DEFAULT_MAX_FILE_BYTES:
                    continue
            except (OSError, ValueError):
                continue
            yield path

    return filtered()


def semantic_project_guard(project: Path) -> dict[str, Any]:
    """Estimate semantic indexing size without reading source contents."""

    max_files = _positive_int("CODEX_CONTINUOUS_TLDR_MAX_FILES", DEFAULT_MAX_CODE_FILES)
    max_bytes = _positive_int("CODEX_CONTINUOUS_TLDR_MAX_CODE_MB", DEFAULT_MAX_CODE_BYTES // (1024 * 1024)) * 1024 * 1024
    max_file_bytes = _positive_int("CODEX_CONTINUOUS_TLDR_MAX_FILE_KB", DEFAULT_MAX_FILE_BYTES // 1024) * 1024
    count = 0
    total_bytes = 0
    try:
        for path in code_files(project):
            count += 1
            try:
                file_size = path.stat().st_size
            except OSError:
                continue
            if file_size > max_file_bytes:
                continue
            total_bytes += file_size
            if count > max_files or total_bytes > max_bytes:
                return {
                    "allowed": False,
                    "reason": "project exceeds semantic safety budget",
                    "files": count,
                    "bytes": total_bytes,
                    "max_files": max_files,
                    "max_bytes": max_bytes,
                    "max_file_bytes": max_file_bytes,
                }
    except OSError as error:
        return {"allowed": False, "reason": f"cannot inspect project: {type(error).__name__}"}
    return {
        "allowed": True,
        "files": count,
        "bytes": total_bytes,
        "max_files": max_files,
        "max_bytes": max_bytes,
        "max_file_bytes": max_file_bytes,
    }
