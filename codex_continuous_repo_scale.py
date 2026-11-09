"""Fast repository-size policy shared by automatic TLDR entry points."""

from __future__ import annotations

import fnmatch
import os
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

DEFAULT_MIN_CODE_FILES = 80
DEFAULT_MIN_CODE_BYTES = 512 * 1024
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
    ".mjs",
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


def _positive_int(value: int | str | None, default: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return parsed if parsed > 0 else default


def _ignore_patterns(project: Path) -> list[str]:
    path = project / ".tldrignore"
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [
        line.strip().rstrip("/")
        for line in lines
        if line.strip() and not line.lstrip().startswith(("#", "!"))
    ]


def _ignored(relative: Path, patterns: list[str]) -> bool:
    value = relative.as_posix()
    for pattern in patterns:
        if fnmatch.fnmatchcase(value, pattern) or relative.match(pattern):
            return True
        if "/" not in pattern and any(
            fnmatch.fnmatchcase(part, pattern) for part in relative.parts
        ):
            return True
        if value == pattern or value.startswith(pattern + "/"):
            return True
    return False


def _git_code_files(project: Path) -> Iterable[Path]:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(project),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return (
        project / item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    )


def measure_repository(
    project: Path,
    *,
    min_code_files: int | None = None,
    min_code_bytes: int | None = None,
    tracked_only: bool = False,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Measure source size and stop as soon as either large-repo threshold wins."""

    project = project.expanduser().resolve()
    environment = environment or os.environ
    min_code_files = _positive_int(
        min_code_files
        if min_code_files is not None
        else environment.get("CODEX_CONTINUOUS_TLDR_MIN_FILES"),
        DEFAULT_MIN_CODE_FILES,
    )
    min_code_bytes = _positive_int(
        min_code_bytes
        if min_code_bytes is not None
        else (
            _positive_int(
                environment.get("CODEX_CONTINUOUS_TLDR_MIN_CODE_KB"),
                DEFAULT_MIN_CODE_BYTES // 1024,
            )
            * 1024
        ),
        DEFAULT_MIN_CODE_BYTES,
    )
    if not project.is_dir():
        return {
            "eligible": False,
            "reason": "project is not a directory",
            "files": 0,
            "bytes": 0,
        }

    patterns = _ignore_patterns(project)
    count = 0
    total_bytes = 0
    visited = 0

    def consider(path: Path) -> dict[str, Any] | None:
        nonlocal count, total_bytes, visited
        visited += 1
        if visited > MAX_WALK_ENTRIES:
            return {
                "eligible": False,
                "reason": "repository walk limit reached",
                "files": count,
                "bytes": total_bytes,
                "visited": visited,
                "min_files": min_code_files,
                "min_bytes": min_code_bytes,
            }
        try:
            relative = path.relative_to(project)
            if (
                _ignored(relative, patterns)
                or path.suffix.lower() not in CODE_EXTENSIONS
            ):
                return None
            size = path.stat().st_size
        except (OSError, ValueError):
            return None
        count += 1
        total_bytes += size
        if count >= min_code_files or total_bytes >= min_code_bytes:
            return {
                "eligible": True,
                "reason": "repository scale threshold reached",
                "files": count,
                "bytes": total_bytes,
                "visited": visited,
                "min_files": min_code_files,
                "min_bytes": min_code_bytes,
                "scale": max(count / min_code_files, total_bytes / min_code_bytes),
            }
        return None

    if tracked_only:
        candidates = _git_code_files(project)
        for path in candidates:
            result = consider(path)
            if result is not None:
                return result
    else:
        try:
            for root, directories, names in os.walk(project, topdown=True):
                relative_root = Path(root).relative_to(project)
                directories[:] = [
                    name
                    for name in directories
                    if name not in SKIP_DIRECTORIES
                    and not _ignored(relative_root / name, patterns)
                ]
                for name in names:
                    result = consider(Path(root) / name)
                    if result is not None:
                        return result
        except OSError as error:
            return {
                "eligible": False,
                "reason": f"cannot inspect project: {type(error).__name__}",
                "files": count,
                "bytes": total_bytes,
            }

    return {
        "eligible": False,
        "reason": "repository is below TLDR scale threshold",
        "files": count,
        "bytes": total_bytes,
        "visited": visited,
        "min_files": min_code_files,
        "min_bytes": min_code_bytes,
        "scale": max(count / min_code_files, total_bytes / min_code_bytes),
    }
