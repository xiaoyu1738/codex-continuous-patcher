#!/usr/bin/env python3
"""Install Continuous Claude v3's hooks, memory runtime, and database for Codex.

The installer keeps the Claude assets and OPC memory runtime in a private,
versioned directory under ``$CODEX_HOME``. Existing deployment directories and
hooks.json files are moved aside with timestamped backups before replacement.
When Docker and uv are available it starts pgvector PostgreSQL and installs the
Python dependencies needed for semantic memory.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import select
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codex_continuous_native import (
    SKILL_OWNER_FILE,
    _tree_digest,
    convert_agents,
    convert_mcp_config,
    convert_skills,
)

EVENT_NAMES = (
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "PreCompact",
    "PostCompact",
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "SubagentStart",
    "SubagentStop",
    "Stop",
    "Interrupt",
)
STATIC_CLAUDE_DIRS = (
    "agents",
    "hooks",
    "plugins",
    "rules",
    "runtime",
    "scripts",
    "servers",
    "skills",
    "chrome",
)
STATIC_CLAUDE_FILES = ("settings.json", "mcp_config.json")
RUNTIME_DIRS = ("opc", "docker")
STATE_BEGIN = "# BEGIN CODEX_CONTINUOUS_PATCHER_HOOK_STATE"
STATE_END = "# END CODEX_CONTINUOUS_PATCHER_HOOK_STATE"
MCP_BEGIN = "# BEGIN CODEX_CONTINUOUS_PATCHER_MCP"
MCP_END = "# END CODEX_CONTINUOUS_PATCHER_MCP"
AGENTS_BEGIN = "# BEGIN CODEX_CONTINUOUS_PATCHER_AGENTS"
AGENTS_END = "# END CODEX_CONTINUOUS_PATCHER_AGENTS"
RULES_BEGIN = "<!-- BEGIN CODEX_CONTINUOUS_PATCHER_RULES -->"
RULES_END = "<!-- END CODEX_CONTINUOUS_PATCHER_RULES -->"
RULES_CREATED_FILE = "<!-- CODEX_CONTINUOUS_PATCHER_CREATED_AGENTS_FILE -->"
SETTING_MARKER = "codex-continuous-patcher-managed"
LLM_TLDR_PACKAGE = "llm-tldr"
LLM_TLDR_VERSION = "1.5.2"
LLM_TLDR_VENV = "tldr-venv"
INSTALL_CONFIG_NAME = "codex-continuous.config.json"
DEFAULT_SUBAGENTS_ENABLED = False
DEFAULT_DASHBOARD_ENABLED = True


def tldr_runtime_venv(deployment: Path, *, ensure_link: bool = True) -> Path:
    """Return the user-level shared TLDR environment.

    The environment is shared across Codex deployments so repeated installs
    do not duplicate Torch/CUDA wheels.  A small symlink remains in the
    deployment because the bridge and generated wrappers use that stable
    path.  ``CODEX_CONTINUOUS_RUNTIME_DIR`` can override the user data root.
    """

    configured = os.environ.get("CODEX_CONTINUOUS_RUNTIME_DIR")
    if configured:
        root = Path(configured).expanduser()
    else:
        xdg_data = os.environ.get("XDG_DATA_HOME")
        root = Path(xdg_data).expanduser() if xdg_data else Path.home() / ".local" / "share"
        root = root / "codex-continuous"
    target = (root / LLM_TLDR_VENV).resolve()
    link = deployment / LLM_TLDR_VENV
    if ensure_link:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if link.is_symlink() and link.resolve(strict=False) == target:
            pass
        elif link.exists():
            # A legacy deployment-local venv is migrated once by moving its
            # contents to the shared location, then replacing it with a link.
            if not target.exists() and link.is_dir() and not link.is_symlink():
                link.rename(target)
            else:
                shutil.rmtree(link) if link.is_dir() and not link.is_symlink() else link.unlink(missing_ok=True)
            link.symlink_to(target, target_is_directory=True)
        else:
            link.symlink_to(target, target_is_directory=True)
    return target


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")  # noqa: UP017


def log(message: str) -> None:
    print(f"[codex-continuous] {message}")


def default_source() -> Path:
    return (Path(__file__).resolve().parent.parent / "Continuous-Claude-v3").resolve()


def default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser().resolve() if configured else (Path.home() / ".codex")


def default_install_config() -> Path:
    """Return the optional public installer configuration next to this script."""

    return Path(__file__).resolve().with_name(INSTALL_CONFIG_NAME)


def load_install_config(path: Path | None = None, *, required: bool = False) -> dict[str, bool]:
    """Load installer feature switches without reading Codex's config.toml.

    The file is deliberately a small, secret-free JSON document. A missing
    implicit file uses safe defaults; an explicitly supplied missing file is
    an error so a typo cannot silently install the wrong feature set.
    """

    config_path = (path or default_install_config()).expanduser()
    if not config_path.is_file():
        if required:
            raise FileNotFoundError(f"installer config does not exist: {config_path}")
        return {
            "subagents_enabled": DEFAULT_SUBAGENTS_ENABLED,
            "dashboard_enabled": DEFAULT_DASHBOARD_ENABLED,
        }
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot read installer config {config_path}: {error}") from error
    if not isinstance(raw, dict):
        raise RuntimeError(f"installer config {config_path} must contain a JSON object")

    def read_switch(name: str, default: bool) -> bool:
        section = raw.get(name, {})
        if not isinstance(section, dict):
            raise RuntimeError(f"installer config {config_path}: {name} must be an object")
        value = section.get("enabled", default)
        if not isinstance(value, bool):
            raise RuntimeError(f"installer config {config_path}: {name}.enabled must be boolean")
        return value

    return {
        "subagents_enabled": read_switch("subagents", DEFAULT_SUBAGENTS_ENABLED),
        "dashboard_enabled": read_switch("dashboard", DEFAULT_DASHBOARD_ENABLED),
    }


def resolve_install_options(
    config_path: Path | None = None,
    *,
    subagents_override: bool | None = None,
    dashboard_override: bool | None = None,
) -> dict[str, bool]:
    """Resolve CLI overrides over the optional JSON installer configuration."""

    options = load_install_config(config_path, required=config_path is not None)
    if subagents_override is not None:
        options["subagents_enabled"] = subagents_override
    if dashboard_override is not None:
        options["dashboard_enabled"] = dashboard_override
    return options


def backup_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.backup.{timestamp()}")


def copy_deployment(source: Path, deployment: Path) -> Path | None:
    source_claude = source / ".claude"
    if not source_claude.is_dir():
        raise FileNotFoundError(f"Continuous Claude source is missing {source_claude}")
    previous_deployment: Path | None = None
    if deployment.exists() or deployment.is_symlink():
        backup = backup_path(deployment)
        deployment.rename(backup)
        previous_deployment = backup
        log(f"existing deployment moved to {backup}")
    deployment.mkdir(mode=0o700, parents=True)
    target_claude = deployment / ".claude"
    target_claude.mkdir(mode=0o700)
    for name in STATIC_CLAUDE_DIRS:
        src = source_claude / name
        if src.is_dir():
            shutil.copytree(src, target_claude / name, symlinks=True)
    for name in STATIC_CLAUDE_FILES:
        src = source_claude / name
        if src.is_file():
            shutil.copy2(src, target_claude / name)
    for name in RUNTIME_DIRS:
        src = source / name
        if src.is_dir():
            # Virtualenvs and uv caches are rebuilt/reused below. Copying a
            # source checkout's .venv here duplicates Torch/CUDA before the
            # installer can choose the shared deployment environment.
            shutil.copytree(
                src,
                deployment / name,
                symlinks=True,
                ignore=shutil.ignore_patterns(".venv", ".cache", "__pycache__"),
            )
    if previous_deployment is not None:
        old_venv = previous_deployment / "opc" / ".venv"
        new_opc = deployment / "opc"
        if old_venv.is_dir() and new_opc.is_dir():
            new_venv = new_opc / ".venv"
            # The source checkout may carry its own deployment-local venv.
            # Prefer the user's existing managed runtime and remove only the
            # freshly copied destination before moving it into place.
            if new_venv.exists() or new_venv.is_symlink():
                if new_venv.is_dir() and not new_venv.is_symlink():
                    shutil.rmtree(new_venv)
                else:
                    new_venv.unlink(missing_ok=True)
            old_venv.rename(new_venv)
        old_tldr_venv = previous_deployment / LLM_TLDR_VENV
        shared_tldr_venv = tldr_runtime_venv(deployment, ensure_link=False)
        if (
            old_tldr_venv.is_dir()
            and not old_tldr_venv.is_symlink()
            and not shared_tldr_venv.exists()
        ):
            shared_tldr_venv.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            old_tldr_venv.rename(shared_tldr_venv)
    return previous_deployment


def _link_tldr_command(binary: Path) -> None:
    local_bin = Path.home() / ".local" / "bin"
    local_bin.mkdir(mode=0o700, parents=True, exist_ok=True)
    link = local_bin / binary.name
    target = binary.resolve()
    if link.is_symlink():
        current = link.resolve(strict=False)
        if current == target:
            return
        # Replace only a stale symlink created by this installer. Preserve
        # unrelated user-managed command links.
        if "continuous-claude-v3" in str(current) and not current.exists():
            link.unlink(missing_ok=True)
        else:
            log(f"left existing command untouched: {link}")
            return
    if link.exists():
        log(f"left existing command untouched: {link}")
        return
    try:
        link.symlink_to(target)
    except OSError as error:
        log(f"could not add {link} to PATH: {error}")


def _install_llm_tldr(deployment: Path) -> Path:
    """Install the original Python TLDR package in the Codex deployment.

    Continuous-Claude-v3's documented/runtime dependency is the PyPI
    ``llm-tldr`` package. Keeping it in a deployment-local venv avoids
    shadowing an unrelated user ``tldr`` command while retaining the exact
    upstream Python implementation, FAISS index format, and daemon.
    """

    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is required to install llm-tldr")
    venv = tldr_runtime_venv(deployment)
    python = venv / "bin" / "python"
    if not python.is_file():
        result = subprocess.run(
            [uv, "venv", str(venv)],
            cwd=deployment,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0 or not python.is_file():
            detail = (result.stderr or result.stdout).strip().splitlines()[-1:]
            raise RuntimeError(f"creating TLDR virtualenv failed{': ' + detail[0] if detail else ''}")
    result = subprocess.run(
        [uv, "pip", "install", "--python", str(python), "--upgrade", f"{LLM_TLDR_PACKAGE}=={LLM_TLDR_VERSION}"],
        cwd=deployment,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[-1:]
        raise RuntimeError(f"installing {LLM_TLDR_PACKAGE} failed{': ' + detail[0] if detail else ''}")
    probe = subprocess.run(
        [str(python), "-c", "import importlib.metadata as m, tldr; print(m.version('llm-tldr')); print(tldr.__file__)"],
        cwd=deployment,
        text=True,
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        raise RuntimeError("installed llm-tldr cannot be imported")
    return python


def install_tldr_runtime(
    deployment: Path,
    *,
    enable_semantic: bool = False,
    semantic_binary: Path | None = None,
) -> Path:
    """Install the original Continuous TLDR dependency from PyPI.

    ``enable_semantic`` and ``semantic_binary`` remain accepted for installer
    compatibility. ``llm-tldr`` already ships semantic search, Sentence
    Transformers, and FAISS, so no separate Rust binary is built or copied.
    """

    if semantic_binary:
        log("ignored --tldr-semantic-binary: llm-tldr already provides the Python/FAISS semantic implementation")
    python = _install_llm_tldr(deployment)
    binary_dir = deployment / "bin"
    binary_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    wrapper = binary_dir / "tldr"
    shutil.copy2(Path(__file__).resolve().with_name("codex_continuous_tldr.py"), wrapper)
    os.chmod(wrapper, 0o755)
    shutil.copy2(
        Path(__file__).resolve().with_name("codex_continuous_tldr_daemon.py"),
        deployment / "codex_continuous_tldr_daemon.py",
    )
    metadata = {
        "repository": "parcadei/llm-tldr",
        "package": LLM_TLDR_PACKAGE,
        "version": LLM_TLDR_VERSION,
        "semantic_enabled": True,
        "semantic_source": f"pypi:{LLM_TLDR_PACKAGE}",
        "semantic_index_format": "faiss",
        "python": str(python),
    }
    metadata_path = binary_dir / "tldr-runtime.json"
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    os.chmod(metadata_path, 0o600)
    _link_tldr_command(wrapper)
    stale_daemon_link = Path.home() / ".local" / "bin" / "tldr-daemon"
    if stale_daemon_link.is_symlink():
        current = stale_daemon_link.resolve(strict=False)
        if "continuous-claude-v3" in str(current) and not current.exists():
            stale_daemon_link.unlink(missing_ok=True)
    log(f"installed {LLM_TLDR_PACKAGE}=={LLM_TLDR_VERSION} with FAISS semantic support")
    return wrapper


def write_runtime_env(deployment: Path, previous_deployment: Path | None = None) -> Path:
    """Write the default local PostgreSQL/embedding configuration.

    Keep the file in both the deployment root (for Compose) and ``opc/`` (for
    ``uv run`` scripts that call ``load_dotenv()`` from their working tree).
    Existing values are preserved so a user can switch embedding providers or
    point at a remote PostgreSQL instance before rerunning the installer.
    """

    defaults = {
        "POSTGRES_USER": "claude",
        "POSTGRES_PASSWORD": "claude_dev",
        "POSTGRES_DB": "continuous_claude",
        "POSTGRES_PORT": "5432",
        "CONTINUOUS_CLAUDE_DB_URL": "postgresql://claude:claude_dev@127.0.0.1:5432/continuous_claude",
        "AGENTICA_MEMORY_BACKEND": "postgres",
        "EMBEDDING_PROVIDER": "local",
        "EMBEDDING_MODEL": "BAAI/bge-large-en-v1.5",
        "TRACE_TO_BRAINTRUST": "false",
        "BRAINTRUST_CC_PROJECT": "codex",
        "CODEX_CONTINUOUS_DISABLE_SUBAGENTS": "1",
    }

    root_env = deployment / ".env"
    existing: dict[str, str] = {}
    env_files = [root_env]
    if previous_deployment is not None:
        env_files.extend(
            [previous_deployment / ".env", previous_deployment / "opc" / ".env"]
        )
    for env_path in env_files:
        if not env_path.is_file():
            continue
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            existing[key.strip()] = value.strip()
    merged = {**defaults, **existing}
    contents = "# Generated by codex-continuous-patcher; customize as needed.\n"
    contents += "\n".join(f"{key}={value}" for key, value in merged.items()) + "\n"
    root_env.write_text(contents, encoding="utf-8")
    os.chmod(root_env, 0o600)
    opc_dir = deployment / "opc"
    if opc_dir.is_dir():
        opc_env = opc_dir / ".env"
        opc_env.write_text(contents, encoding="utf-8")
        os.chmod(opc_env, 0o600)
    return root_env


def _strip_block(contents: str, begin: str, end: str) -> str:
    pattern = re.compile(rf"\n?{re.escape(begin)}\n.*?{re.escape(end)}\n?", re.DOTALL)
    return pattern.sub("\n", contents)


def _atomic_config_write(path: Path, contents: str) -> None:
    tomllib.loads(contents)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(contents.rstrip() + "\n", encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _atomic_text_write(path: Path, contents: str) -> None:
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(contents.rstrip() + "\n", encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def merge_global_rules(source: Path, codex_home: Path) -> dict[str, Any]:
    """Install only safe, Codex-compatible project rules in the user instruction file."""

    source_rules = source / ".claude" / "rules"
    compatible = [
        "agent-memory-recall.md",
        "claim-verification.md",
        "dynamic-recall.md",
        "proactive-memory-disclosure.md",
        "tldr-cli.md",
    ]
    not_imported = {
        "agent-model-selection.md": "Claude model names do not map to Codex model routing",
        "cross-terminal-db.md": "contains environment-specific database connection details",
        "destructive-commands.md": "conflicts with Codex's explicit user authorization model",
        "hook-auto-execute.md": "would execute commands sourced from hook output",
        "no-haiku.md": "Claude model and Task tool specific",
        "proactive-delegation.md": "requires subagents, disabled by default for this deployment",
        "use-scout-not-explore.md": "Claude agent names and tools are not available in Codex",
    }
    adopted = [name for name in compatible if (source_rules / name).is_file()]
    skipped = {name: reason for name, reason in not_imported.items() if (source_rules / name).is_file()}
    if not adopted:
        return {"installed": False, "adopted": [], "not_imported": skipped}

    def adapted_rule(name: str) -> str:
        source_text = (source_rules / name).read_text(encoding="utf-8", errors="replace").strip()
        # These rules are instruction documents rather than executable code.
        # Rewrite the Claude-specific runtime paths and slash command names so
        # the same guidance points at this deployment's Codex resources.
        replacements = {
            "$CLAUDE_OPC_DIR": "$CODEX_CONTINUOUS_ROOT/opc",
            "$CLAUDE_PROJECT_DIR": "$CODEX_CONTINUOUS_ROOT",
            "~/.claude/cache": "$CODEX_CONTINUOUS_ROOT/.claude/cache",
            "Agents (kraken, architect, phoenix, spark)": "Codex sessions",
            "agents (kraken, architect, phoenix, spark)": "Codex sessions",
        }
        for old, new in replacements.items():
            source_text = source_text.replace(old, new)
        default_root = str((codex_home / "continuous-claude-v3").resolve())
        # Child shells may not inherit the bridge's environment.  Preserve an
        # explicit value while providing a deterministic local deployment
        # default for the generated Codex rules.
        source_text = source_text.replace(
            "${CODEX_CONTINUOUS_ROOT}",
            f"${{CODEX_CONTINUOUS_ROOT:-{default_root}}}",
        )
        source_text = source_text.replace(
            "$CODEX_CONTINUOUS_ROOT",
            f"${{CODEX_CONTINUOUS_ROOT:-{default_root}}}",
        )
        source_text = re.sub(
            r"(?<![A-Za-z0-9_-])/recall(?![A-Za-z0-9_-])",
            "/cc-codex-continuous-v3-recall",
            source_text,
        )
        return source_text

    path = codex_home / "AGENTS.md"
    original = path.read_text(encoding="utf-8") if path.is_file() else ""
    created_file = RULES_CREATED_FILE in original or not path.exists()
    base = _strip_block(original, RULES_BEGIN, RULES_END).rstrip()
    body = [
        RULES_BEGIN,
        *([RULES_CREATED_FILE] if created_file else []),
        "## Continuous Codex",
        "",
        "These adapted rules are advisory. Verify them against the current source and deployment state.",
        "",
    ]
    for name in adopted:
        body.extend([f"### {name}", "", adapted_rule(name), ""])
    body.extend([
        RULES_END,
    ])
    updated = (base + "\n\n" if base else "") + "\n".join(body) + "\n"
    _atomic_text_write(path, updated)
    return {
        "installed": True,
        "path": str(path),
        "adopted": adopted,
        "not_imported": skipped,
        "created_file": created_file,
    }


def remove_global_rules(codex_home: Path) -> bool:
    path = codex_home / "AGENTS.md"
    if not path.is_file():
        return False
    contents = path.read_text(encoding="utf-8")
    if RULES_BEGIN not in contents:
        return False
    created_file = RULES_CREATED_FILE in contents
    cleaned = _strip_block(contents, RULES_BEGIN, RULES_END)
    if created_file and not cleaned.strip():
        path.unlink()
    else:
        _atomic_text_write(path, cleaned)
    return True


def _table_bounds(lines: list[str], table: str) -> tuple[int, int] | None:
    header = re.compile(rf"^\s*\[\s*{re.escape(table)}\s*\]\s*(?:#.*)?$")
    start = next((i for i, line in enumerate(lines) if header.match(line)), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^\s*\[\[?", lines[i])), len(lines))
    return start, end


def _setting_marker(table: str, key: str) -> re.Pattern[str]:
    return re.compile(
        rf"# {SETTING_MARKER}:{re.escape(table)}\.{re.escape(key)}=(true|false):([A-Za-z0-9_-]+|absent)\s*$"
    )


def _encode_original(line: str) -> str:
    return base64.urlsafe_b64encode(line.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_original(value: str) -> str:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")


def set_managed_setting(config_path: Path, table: str, key: str, enabled: bool) -> None:
    """Set one TOML boolean while retaining the user's prior assignment for uninstall."""

    contents = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    if contents.strip():
        parsed = tomllib.loads(contents)
        table_value = parsed.get(table)
        if table_value is not None and not isinstance(table_value, dict):
            raise RuntimeError(f"cannot safely edit inline {table} value in {config_path}")
    else:
        parsed = {}
    lines = contents.splitlines()
    bounds = _table_bounds(lines, table)
    rendered = "true" if enabled else "false"
    marker_re = _setting_marker(table, key)

    def replacement(original: str) -> str:
        previous = "absent"
        if match := marker_re.search(original):
            previous = match.group(2)
        elif re.match(rf"^\s*{re.escape(key)}\s*=", original):
            previous = _encode_original(original)
        return f"{key} = {rendered} # {SETTING_MARKER}:{table}.{key}={rendered}:{previous}"

    if bounds:
        start, end = bounds
        key_line = next(
            (i for i in range(start + 1, end) if re.match(rf"^\s*{re.escape(key)}\s*=", lines[i])),
            None,
        )
        if key_line is not None:
            lines[key_line] = replacement(lines[key_line])
        else:
            lines.insert(end, replacement(""))
    elif isinstance(parsed.get(table), dict):
        # A nested table may have implicitly created [features]/[agents]. A
        # dotted root assignment can coexist without redefining that table.
        root_line = f"{table}.{key} = {rendered} # {SETTING_MARKER}:{table}.{key}={rendered}:absent"
        first_table = next((i for i, line in enumerate(lines) if re.match(r"^\s*\[\[?", line)), len(lines))
        lines.insert(first_table, root_line)
    else:
        if re.search(rf"^\s*{re.escape(table)}\s*=", contents, re.MULTILINE):
            raise RuntimeError(f"cannot safely edit inline {table} value in {config_path}")
        suffix = "\n" if lines and lines[-1].strip() else ""
        lines.extend([suffix, f"[{table}] # {SETTING_MARKER}-created-table", replacement("")])

    _atomic_config_write(config_path, "\n".join(lines))


def restore_managed_setting(config_path: Path, table: str, key: str) -> bool:
    if not config_path.is_file():
        return False
    contents = config_path.read_text(encoding="utf-8")
    lines = contents.splitlines()
    bounds = _table_bounds(lines, table)
    marker_re = _setting_marker(table, key)
    dotted_re = re.compile(rf"^\s*{re.escape(table)}\.{re.escape(key)}\s*=\s*(true|false)\s+(# .*?)$")
    changed = False

    def restore(line: str) -> str | None:
        nonlocal changed
        marker = marker_re.search(line)
        dotted = dotted_re.match(line)
        if marker:
            expected, original = marker.groups()
            assignment = re.search(r"=\s*(true|false)\s+#", line)
            if assignment and assignment.group(1) == expected:
                changed = True
                return None if original == "absent" else _decode_original(original)
            changed = True
            return marker_re.sub("", line).rstrip()
        if dotted:
            expected, marker_text = dotted.groups()
            managed = re.search(
                rf"{SETTING_MARKER}:{re.escape(table)}\.{re.escape(key)}=(true|false):(absent|[A-Za-z0-9_-]+)",
                marker_text,
            )
            if managed:
                original_expected, original = managed.groups()
                changed = True
                if expected != original_expected:
                    return line.split("#", 1)[0].rstrip()
                return None if original == "absent" else _decode_original(original)
        return line

    if bounds:
        start, end = bounds
        for i in range(start + 1, end):
            if re.match(rf"^\s*{re.escape(key)}\s*=", lines[i]):
                restored = restore(lines[i])
                if restored is None:
                    lines[i] = ""
                else:
                    lines[i] = restored
                break
        if "-created-table" in lines[start]:
            end = next((i for i in range(start + 1, len(lines)) if re.match(r"^\s*\[\[?", lines[i])), len(lines))
            if not any(line.strip() and not line.lstrip().startswith("#") for line in lines[start + 1 : end]):
                del lines[start:end]
            else:
                lines[start] = re.sub(r"\s+# .*?-created-table", "", lines[start])
    else:
        for i, line in enumerate(lines):
            if dotted_re.match(line):
                restored = restore(line)
                lines[i] = restored or ""
                break
    if changed:
        _atomic_config_write(config_path, "\n".join(lines))
    return changed


def set_subagent_feature(config_path: Path, enabled: bool) -> None:
    set_managed_setting(config_path, "features", "multi_agent", enabled)
    set_managed_setting(config_path, "agents", "enabled", enabled)
    if not config_path.is_file():
        return
    features = tomllib.loads(config_path.read_text(encoding="utf-8")).get("features", {})
    multi_agent_v2 = features.get("multi_agent_v2") if isinstance(features, dict) else None
    if multi_agent_v2 is not None:
        if isinstance(multi_agent_v2, dict):
            set_managed_setting(config_path, "features.multi_agent_v2", "enabled", enabled)
        elif isinstance(multi_agent_v2, bool):
            set_managed_setting(config_path, "features", "multi_agent_v2", enabled)


def merge_mcp_config(config_path: Path, catalog_path: Path) -> dict[str, Any]:
    """Merge active generated MCP definitions without replacing user servers."""

    catalog = tomllib.loads(catalog_path.read_text(encoding="utf-8")) if catalog_path.is_file() else {}
    generated_servers = catalog.get("mcp_servers", {})
    original = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    base = _strip_block(original, MCP_BEGIN, MCP_END).rstrip()
    parsed = tomllib.loads(base) if base.strip() else {}
    existing = parsed.get("mcp_servers", {})
    if not isinstance(existing, dict):
        raise RuntimeError("config.toml mcp_servers must be a table")
    selected: list[str] = []
    lines = [MCP_BEGIN]
    for name, server in sorted(generated_servers.items()):
        if not isinstance(server, dict) or server.get("enabled") is False:
            continue
        if name in existing:
            continue
        section_lines = [f"[mcp_servers.{json.dumps(name, ensure_ascii=False)}]"]
        env = server.get("env")
        headers = server.get("http_headers")
        env_headers = server.get("env_http_headers")
        for key, value in server.items():
            if key in {"env", "http_headers", "env_http_headers", "enabled"}:
                continue
            section_lines.append(f"{key} = {json.dumps(value, ensure_ascii=False)}" if isinstance(value, str) else f"{key} = {json.dumps(value)}")
        for table_name, table in (("env", env), ("http_headers", headers), ("env_http_headers", env_headers)):
            if not isinstance(table, dict) or not table:
                continue
            section_lines.extend(["", f"[mcp_servers.{json.dumps(name, ensure_ascii=False)}.{table_name}]"])
            for key, value in sorted(table.items()):
                section_lines.append(f"{json.dumps(str(key), ensure_ascii=False)} = {json.dumps(str(value), ensure_ascii=False)}")
        lines.extend(section_lines + [""])
        selected.append(name)
    if selected:
        lines.append(MCP_END)
        new_contents = (base + "\n\n" if base else "") + "\n".join(lines) + "\n"
    else:
        new_contents = base + ("\n" if base else "")
    _atomic_config_write(config_path, new_contents)
    return {"installed": selected, "collisions": sorted(set(generated_servers) - set(selected) - {name for name, value in generated_servers.items() if isinstance(value, dict) and value.get("enabled") is False})}


def merge_agent_roles(config_path: Path, agents: dict[str, Any], enabled: bool) -> dict[str, Any]:
    """Register converted agent roles only when the user opts in."""

    original = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    base = _strip_block(original, AGENTS_BEGIN, AGENTS_END).rstrip()
    parsed = tomllib.loads(base) if base.strip() else {}
    configured = parsed.get("agents", {})
    if not isinstance(configured, dict):
        raise RuntimeError("config.toml agents must be a table")
    lines = [AGENTS_BEGIN]
    installed: list[str] = []
    skipped: list[str] = []
    if enabled:
        for role in agents.get("agents", []):
            if not isinstance(role, dict) or not role.get("name"):
                continue
            name = str(role["name"])
            if name in configured:
                skipped.append(name)
                continue
            lines.extend(
                [
                    f"[agents.{json.dumps(name, ensure_ascii=False)}]",
                    f"description = {json.dumps(str(role.get('description', name)), ensure_ascii=False)}",
                    f"config_file = {json.dumps(str(role['config_file']), ensure_ascii=False)}",
                    "",
                ]
            )
            installed.append(name)
    if installed:
        lines.append(AGENTS_END)
        updated = (base + "\n\n" if base else "") + "\n".join(lines) + "\n"
    else:
        updated = base + ("\n" if base else "")
    _atomic_config_write(config_path, updated)
    return {"enabled": enabled, "installed": installed, "skipped": skipped}


def install_native_resources(
    source: Path,
    deployment: Path,
    codex_home: Path,
    *,
    enable_subagents: bool = False,
    enable_dashboard: bool = True,
) -> dict[str, Any]:
    """Install Codex-discoverable skills and selected optional resources."""

    native_dir = deployment / "codex-native"
    native_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    skills_result = convert_skills(source / ".claude", native_dir, codex_home=codex_home)
    if enable_subagents:
        agents_result = convert_agents(source / ".claude", native_dir)
        agents_result["enabled"] = True
        agents_result["reason"] = "Converted roles are registered in Codex by this installation."
        (native_dir / "agents-manifest.json").write_text(
            json.dumps(agents_result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    else:
        agents_result = {
            "version": 1,
            "enabled": False,
            "reason": "Subagent installation is disabled by installer configuration.",
            "agents": [],
            "skipped": [],
        }
        (native_dir / "agents-manifest.json").write_text(
            json.dumps(agents_result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    mcp_result = convert_mcp_config(
        source / ".claude", native_dir, codex_claude=deployment / ".claude"
    )
    native_manifest = {
        "version": 1,
        "skills": skills_result,
        "agents": agents_result,
        "mcp": mcp_result,
        "rules": merge_global_rules(source, codex_home),
        "subagents_enabled": enable_subagents,
        "dashboard_enabled": enable_dashboard,
    }
    (native_dir / "native-manifest.json").write_text(
        json.dumps(native_manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    scripts = [
        "codex_continuous_status.py",
        "codex_continuous_tldr_daemon.py",
        "codex_continuous_search_router.py",
        "codex_continuous_semantic_runtime.py",
        "codex_continuous_semantic_index.py",
        "codex_continuous_shell_router.py",
        "codex_continuous_semantic_warm.py",
        "codex_continuous_change_tracker.py",
    ]
    if enable_dashboard:
        scripts.append("codex_continuous_dashboard.py")
    for script_name in scripts:
        script = Path(__file__).resolve().with_name(script_name)
        target = deployment / script_name
        shutil.copy2(script, target)
        os.chmod(target, 0o755)
    # Keep the compatibility CLI entry point present even when an install is
    # deliberately run with --skip-tldr-setup.  The real binaries may have
    # been preserved from the previous deployment, while the Python wrapper
    # itself is part of this adapter and must track the source tree.
    tldr_wrapper = deployment / "bin" / "tldr"
    tldr_wrapper.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copy2(Path(__file__).resolve().with_name("codex_continuous_tldr.py"), tldr_wrapper)
    os.chmod(tldr_wrapper, 0o755)
    return native_manifest


def sync_python_runtime(deployment: Path) -> None:
    """Install OPC dependencies into the shared TLDR virtualenv when present."""

    opc_dir = deployment / "opc"
    uv = shutil.which("uv")
    if not opc_dir.is_dir() or not uv:
        log("uv or opc/ is unavailable; skipped Python runtime sync")
        return
    tldr_venv = tldr_runtime_venv(deployment)
    environment = os.environ.copy()
    # The memory runtime uses pgvector for archival semantic recall. It is an
    # OPC optional extra, so request it explicitly while retaining llm-tldr's
    # packages in the same environment.
    command = [uv, "sync", "--inexact", "--extra", "postgres"]
    if tldr_venv.is_dir() and (tldr_venv / "bin" / "python").is_file():
        opc_venv = opc_dir / ".venv"
        if opc_venv.exists() or opc_venv.is_symlink():
            if opc_venv.is_symlink() or not opc_venv.is_dir():
                opc_venv.unlink(missing_ok=True)
            else:
                shutil.rmtree(opc_venv)
        # Keep `uv run` from opc/ working while both runtimes share one copy
        # of Torch, CUDA, Sentence Transformers, and their transitive wheels.
        opc_venv.symlink_to(Path("..") / LLM_TLDR_VENV, target_is_directory=True)
        environment["UV_PROJECT_ENVIRONMENT"] = str(tldr_venv)
        log("using one shared uv environment for OPC and llm-tldr")
    result = subprocess.run(
        command,
        cwd=opc_dir,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[-1:]
        raise RuntimeError(f"uv sync failed{': ' + detail[0] if detail else ''}")
    log("installed OPC Python runtime (asyncpg, pgvector, sentence-transformers)")


def _docker_env(deployment: Path) -> dict[str, str]:
    """Use an isolated Docker client config without Windows credential helpers."""

    config_dir = deployment / ".docker-config"
    config_dir.mkdir(mode=0o700, exist_ok=True)
    config_path = config_dir / "config.json"
    if not config_path.exists():
        config_path.write_text("{}\n", encoding="utf-8")
        os.chmod(config_path, 0o600)
    env = os.environ.copy()
    env["DOCKER_CONFIG"] = str(config_dir)
    return env


def _run_docker(command: list[str], deployment: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = _docker_env(deployment)
    result = subprocess.run(
        command, cwd=cwd, env=env, text=True, capture_output=True, check=False
    )
    if result.returncode == 0 or not shutil.which("newgrp"):
        return result
    return subprocess.run(
        ["newgrp", "docker", "-c", shlex.join(command)],
        cwd=cwd,
        check=False,
        env=env,
        text=True,
        capture_output=True,
    )


def start_database_stack(deployment: Path) -> None:
    """Start the upstream PostgreSQL + pgvector Compose service."""

    compose_dir = deployment / "docker"
    compose_file = compose_dir / "docker-compose.yml"
    env_file = deployment / ".env"
    docker = shutil.which("docker")
    if not compose_file.is_file() or not docker:
        log("Docker/Compose unavailable; PostgreSQL stack was not started")
        return
    command = [docker, "compose", "--env-file", str(env_file), "up", "-d"]
    result = _run_docker(command, deployment, compose_dir)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[-1:]
        raise RuntimeError(f"Docker pgvector startup failed{': ' + detail[0] if detail else ''}")
    ready_command = [
        docker,
        "exec",
        "continuous-claude-postgres",
        "pg_isready",
        "-U",
        "claude",
        "-d",
        "continuous_claude",
    ]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        ready = _run_docker(ready_command, deployment, compose_dir)
        if ready.returncode == 0:
            ensure_core = [
                docker,
                "exec",
                "continuous-claude-postgres",
                "psql",
                "-U",
                "claude",
                "-d",
                "continuous_claude",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                (
                    "CREATE TABLE IF NOT EXISTS core_memory ("
                    "session_id TEXT NOT NULL, agent_id TEXT, key TEXT NOT NULL, "
                    "value TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()); "
                    "CREATE INDEX IF NOT EXISTS idx_core_memory_session "
                    "ON core_memory(session_id, agent_id, key);"
                ),
            ]
            schema = _run_docker(ensure_core, deployment, compose_dir)
            if schema.returncode != 0:
                detail = (schema.stderr or schema.stdout).strip().splitlines()[-1:]
                raise RuntimeError(
                    f"core memory schema setup failed{': ' + detail[0] if detail else ''}"
                )
            log("pgvector PostgreSQL is healthy")
            return
        time.sleep(1)
    raise RuntimeError("PostgreSQL did not become healthy within 60 seconds")


def load_settings(source: Path) -> dict[str, Any]:
    settings_path = source / ".claude" / "settings.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot read {settings_path}: {error}") from error
    if not isinstance(settings, dict) or not isinstance(settings.get("hooks"), dict):
        raise RuntimeError(f"{settings_path} has no hooks object")  # noqa: TRY004
    return settings


def hook_timeout(event: str, handler: dict[str, Any]) -> int:
    raw = handler.get("timeout")
    if isinstance(raw, (int, float)) and raw > 0:
        return max(1, min(int(raw), 600))
    defaults = {
        # Codex caps these lifecycle hooks at three seconds.
        "SessionEnd": 3,
        "Interrupt": 3,
        "SessionStart": 15,
        "UserPromptSubmit": 15,
        "PreToolUse": 30,
        "PostToolUse": 30,
        "PreCompact": 30,
        "PostCompact": 30,
        "Stop": 30,
    }
    return defaults.get(event, 30)


def effective_hook_timeout(event: str, handler: dict[str, Any], command: str) -> int:
    """Return a timeout that accounts for the optional semantic search model.

    The source smart-search hook is configured for a fast local Claude path.
    Codex's llm-tldr Sentence Transformer model may load on the first query,
    which can take longer than that source default. Extend only this hook;
    preserve every other source timeout exactly.
    """

    timeout = hook_timeout(event, handler)
    if event == "PreToolUse" and "smart-search-router" in command:
        return max(timeout, 60)
    return timeout


def adapt_braintrust_plugin(deployment: Path) -> None:
    """Make the bundled Braintrust scripts use the isolated Codex deployment."""

    plugin = deployment / ".claude" / "plugins" / "braintrust-tracing"
    common = plugin / "hooks" / "common.sh"
    if not common.is_file():
        return
    contents = common.read_text(encoding="utf-8")
    contents = contents.replace(
        '[ -f "$HOME/.claude/.env" ] && set -a && source "$HOME/.claude/.env" && set +a',
        "# The Codex bridge loads the deployment .env before launching this hook.",
    )
    contents = contents.replace('BRAINTRUST_CC_PROJECT:-claude-code', 'BRAINTRUST_CC_PROJECT:-codex')
    common.write_text(contents, encoding="utf-8")
    for script in (plugin / "hooks").glob("*.sh"):
        content = script.read_text(encoding="utf-8")
        content = content.replace("Claude Code", "Codex").replace("claude-code", "codex")
        script.write_text(content, encoding="utf-8")


def braintrust_hook_specs(deployment: Path) -> list[tuple[str, str, Path, bool]]:
    if os.environ.get("CODEX_CONTINUOUS_ENABLE_BRAINTRUST", "").lower() not in {"1", "true", "yes"}:
        return []
    hooks = deployment / ".claude" / "plugins" / "braintrust-tracing" / "hooks"
    return [
        ("SessionStart", "braintrust_session_start", hooks / "session_start.sh", False),
        ("UserPromptSubmit", "braintrust_user_prompt_submit", hooks / "user_prompt_submit.sh", False),
        ("PostToolUse", "braintrust_post_tool_use", hooks / "post_tool_use.sh", True),
        ("Stop", "braintrust_stop", hooks / "stop_hook.sh", False),
        ("SessionEnd", "braintrust_session_end", hooks / "session_end.sh", False),
    ]


def bridge_command(deployment: Path, hook_id: str) -> str:
    bridge = deployment / "codex_continuous_bridge.py"
    return f"python3 {json.dumps(str(bridge))} --hook-id {json.dumps(hook_id)}"


def bridge_event_command(deployment: Path, event: str) -> str:
    bridge = deployment / "codex_continuous_bridge.py"
    return (
        f"python3 {json.dumps(str(bridge))} --hook-id {json.dumps(f'{event.lower()}_bundle')} "
        f"--event {json.dumps(event)}"
    )


def build_hooks(source: Path, deployment: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    settings = load_settings(source)
    manifest: dict[str, Any] = {
        "version": 1,
        "deployment_root": str(deployment),
        "source": str(source),
        "hooks": {},
    }
    generated: dict[str, list[dict[str, Any]]] = {}
    source_hooks = settings.get("hooks", {})
    for event in EVENT_NAMES:
        groups = source_hooks.get(event, [])
        if not isinstance(groups, list):
            continue
        generated_groups: list[dict[str, Any]] = []
        for group_index, group in enumerate(groups):
            if not isinstance(group, dict):
                continue
            matcher = group.get("matcher")
            handlers = group.get("hooks", [])
            if not isinstance(handlers, list):
                continue
            for handler_index, handler in enumerate(handlers):
                if not isinstance(handler, dict) or handler.get("type") != "command":
                    # Prompt/agent/MCP handlers have no Claude command-line
                    # equivalent in this adapter and are left for a future
                    # native Codex implementation.
                    continue
                command = handler.get("command")
                if not isinstance(command, str) or not command.strip():
                    continue
                hook_id = f"{event.lower()}_{group_index}_{handler_index}"
                timeout = effective_hook_timeout(event, handler, command)
                manifest["hooks"][hook_id] = {
                    "id": hook_id,
                    "event": event,
                    "matcher": matcher,
                    "command": command,
                    "timeout": timeout,
                    "async": bool(handler.get("async", False)),
                }
                generated_groups.append(
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": bridge_command(deployment, hook_id),
                                "timeout": timeout,
                                **({"async": True} if handler.get("async") else {}),
                            }
                        ]
                    }
                )
        if generated_groups:
            generated[event] = generated_groups

    for event, hook_id, command_path, is_async in braintrust_hook_specs(deployment):
        if not command_path.is_file():
            continue
        command = f"bash {shlex.quote(str(command_path))}"
        manifest["hooks"][hook_id] = {
            "id": hook_id,
            "event": event,
            "matcher": None,
            "command": command,
            "timeout": hook_timeout(event, {}),
            "async": is_async,
            "plugin": "braintrust-tracing",
        }
        generated.setdefault(event, []).append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": bridge_command(deployment, hook_id),
                        "timeout": hook_timeout(event, {}),
                        **({"async": True} if is_async else {}),
                    }
                ]
            }
        )

    shell_router = deployment / "codex_continuous_shell_router.py"
    if shell_router.is_file():
        hook_id = "codex_shell_router"
        shell_command = f"python3 {shlex.quote(str(shell_router))}"
        shell_matcher = "Bash|Shell|shell|exec_command|command|terminal"
        manifest["hooks"][hook_id] = {
            "id": hook_id,
            "event": "PreToolUse",
            "matcher": shell_matcher,
            "command": shell_command,
            "timeout": 30,
            "async": False,
            "plugin": "codex-continuous-patcher",
        }
        generated.setdefault("PreToolUse", []).append(
            {
                "matcher": shell_matcher,
                "hooks": [
                    {
                        "type": "command",
                        "command": bridge_command(deployment, hook_id),
                        "timeout": 30,
                    }
                ],
            }
        )

    change_tracker = deployment / "codex_continuous_change_tracker.py"
    if change_tracker.is_file():
        tracker_matcher = "Bash|Edit|Write|MultiEdit"
        for event in ("PreToolUse", "PostToolUse"):
            hook_id = f"codex_change_tracker_{event.lower()}"
            tracker_command = f"python3 {shlex.quote(str(change_tracker))}"
            manifest["hooks"][hook_id] = {
                "id": hook_id,
                "event": event,
                "matcher": tracker_matcher,
                "command": tracker_command,
                "timeout": 30,
                "async": False,
                "plugin": "codex-continuous-patcher",
            }

    # One Codex process per lifecycle event.  The manifest retains each source
    # hook for traceability, while the generated hooks.json invokes the bridge
    # once and lets it apply source matchers and run commands sequentially.
    bundled: dict[str, list[dict[str, Any]]] = {}
    for event in EVENT_NAMES:
        event_hooks = [
            hook
            for hook in manifest["hooks"].values()
            if isinstance(hook, dict) and hook.get("event") == event
        ]
        if not event_hooks:
            continue
        timeout = min(
            600,
            max(30, sum(int(hook.get("timeout") or hook_timeout(event, {})) for hook in event_hooks)),
        )
        bundled[event] = [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": bridge_event_command(deployment, event),
                        "timeout": timeout,
                    }
                ]
            }
        ]
    generated = bundled

    manifest_path = deployment / "hooks-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(manifest_path, 0o600)
    return manifest, {"description": "Continuous Claude v3 adapted for Codex", "hooks": generated}


def merge_hooks_file(path: Path, generated: dict[str, Any]) -> dict[str, Any]:
    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except json.JSONDecodeError as error:
            backup = backup_path(path)
            path.rename(backup)
            log(f"invalid hooks.json moved to {backup}: {error}")
    hooks = existing.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    bridge_marker = "codex_continuous_bridge.py"
    for event, generated_groups in generated.get("hooks", {}).items():
        old_groups = hooks.get(event, [])
        if not isinstance(old_groups, list):
            old_groups = []
        retained = []
        for group in old_groups:
            serialized = json.dumps(group, ensure_ascii=False)
            if bridge_marker not in serialized:
                retained.append(group)
        hooks[event] = retained + generated_groups
    existing["description"] = generated.get("description", existing.get("description", ""))
    existing["hooks"] = hooks
    path.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return existing


def _read_json_lines(process: subprocess.Popen[bytes], response_id: int, timeout: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = max(0.05, deadline - time.monotonic())
        ready, _, _ = select.select([process.stdout], [], [], remaining)
        if not ready or process.stdout is None:
            continue
        line = process.stdout.readline()
        if not line:
            break
        try:
            value = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if value.get("id") == response_id:
            return value
    return None


def discover_hook_entries(codex_bin: str, cwd: Path) -> list[dict[str, Any]]:
    process = subprocess.Popen(
        [codex_bin, "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        messages = (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "codex-continuous-patcher", "version": "1"},
                    "capabilities": {},
                },
            },
            {"jsonrpc": "2.0", "method": "initialized", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "hooks/list",
                "params": {"cwds": [str(cwd)]},
            },
        )
        if process.stdin is None:
            raise RuntimeError("app-server stdin is unavailable")
        process.stdin.write((json.dumps(messages[0]) + "\n").encode())
        process.stdin.flush()
        initialize = _read_json_lines(process, 1, 15)
        if not initialize or "error" in initialize:
            raise RuntimeError(f"Codex app-server initialize failed: {initialize}")
        process.stdin.write((json.dumps(messages[1]) + "\n").encode())
        process.stdin.write((json.dumps(messages[2]) + "\n").encode())
        process.stdin.flush()
        response = _read_json_lines(process, 2, 30)
        if not response or "error" in response:
            raise RuntimeError(f"Codex hooks/list failed: {response}")
        entries: list[dict[str, Any]] = []
        for item in response.get("result", {}).get("data", []):
            if isinstance(item, dict):
                entries.extend(hook for hook in item.get("hooks", []) if isinstance(hook, dict))
        return entries
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()


def strip_state_block(contents: str) -> str:
    pattern = re.compile(
        rf"\n?{re.escape(STATE_BEGIN)}\n.*?{re.escape(STATE_END)}\n?",
        re.DOTALL,
    )
    return pattern.sub("\n", contents)


def write_trust_state(config_path: Path, entries: list[dict[str, Any]], hooks_path: Path) -> int:
    trusted = [
        entry
        for entry in entries
        if (entry.get("enabled") if "enabled" in entry else entry.get("isEnabled")) is True
        and (entry.get("current_hash") or entry.get("currentHash"))
        and str(entry.get("source_path") or entry.get("sourcePath") or "") == str(hooks_path)
    ]
    original = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    base = strip_state_block(original).rstrip() + ("\n" if original.strip() else "")
    lines = [STATE_BEGIN, "# Generated from Codex hooks/list; rerun install.py after hook changes."]
    for entry in sorted(trusted, key=lambda value: str(value.get("key", ""))):
        key = entry.get("key")
        digest = entry.get("current_hash") or entry.get("currentHash")
        if not isinstance(key, str) or not isinstance(digest, str):
            continue
        lines.extend([f"[hooks.state.{json.dumps(key, ensure_ascii=False)}]", f"trusted_hash = {json.dumps(digest)}", ""])
    lines.append(STATE_END)
    new_contents = base + "\n".join(lines) + "\n"
    config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if config_path.exists():
        mode = config_path.stat().st_mode & 0o777
    else:
        mode = 0o600
    temp = config_path.with_name(f".{config_path.name}.tmp-{os.getpid()}")
    temp.write_text(new_contents, encoding="utf-8")
    os.chmod(temp, mode)
    os.replace(temp, config_path)
    return len(trusted)


def write_record(deployment: Path, source: Path, hooks_path: Path, config_path: Path) -> None:
    record = {
        "installed_at": datetime.now(timezone.utc).isoformat(),  # noqa: UP017
        "source": str(source),
        "deployment": str(deployment),
        "hooks": str(hooks_path),
        "config": str(config_path),
        "platform": "linux",
    }
    (deployment / "install-record.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def uninstall(codex_home: Path) -> int:
    """Remove generated Codex registrations and retain the deployment as a backup."""

    codex_home = codex_home.expanduser().resolve()
    hooks_path = codex_home / "hooks.json"
    config_path = codex_home / "config.toml"
    deployment = codex_home / "continuous-claude-v3"
    if hooks_path.is_file():
        try:
            hooks_config = json.loads(hooks_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise RuntimeError(f"cannot safely update {hooks_path}: {error}") from error
        hooks = hooks_config.get("hooks", {})
        if isinstance(hooks, dict):
            for event, groups in list(hooks.items()):
                if not isinstance(groups, list):
                    continue
                hooks[event] = [
                    group
                    for group in groups
                    if "codex_continuous_bridge.py" not in json.dumps(group, ensure_ascii=False)
                ]
                if not hooks[event]:
                    del hooks[event]
            hooks_config["hooks"] = hooks
            hooks_path.write_text(json.dumps(hooks_config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.chmod(hooks_path, hooks_path.stat().st_mode & 0o777)

    if config_path.is_file():
        contents = config_path.read_text(encoding="utf-8")
        contents = _strip_block(contents, MCP_BEGIN, MCP_END)
        contents = _strip_block(contents, AGENTS_BEGIN, AGENTS_END)
        _atomic_config_write(config_path, contents)
        restore_managed_setting(config_path, "features.multi_agent_v2", "enabled")
        restore_managed_setting(config_path, "features", "multi_agent_v2")
        restore_managed_setting(config_path, "features", "multi_agent")
        restore_managed_setting(config_path, "agents", "enabled")
    remove_global_rules(codex_home)

    skills_dir = codex_home / "skills"
    preserved_skills: list[str] = []
    removed_skills = 0
    if skills_dir.is_dir():
        for owner_path in skills_dir.glob(f"*/{SKILL_OWNER_FILE}"):
            skill_dir = owner_path.parent
            try:
                owner = json.loads(owner_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                owner = {}
            if owner.get("source") != "Continuous-Claude-v3":
                continue
            if owner.get("installed_hash") == _tree_digest(skill_dir):
                shutil.rmtree(skill_dir)
                removed_skills += 1
            else:
                owner_path.unlink(missing_ok=True)
                preserved_skills.append(skill_dir.name)

    if deployment.exists():
        local_bin = Path.home() / ".local" / "bin"
        for name in ("tldr", "tldr-daemon"):
            link = local_bin / name
            expected = (deployment / "bin" / name).resolve()
            if link.is_symlink() and link.resolve(strict=False) == expected:
                link.unlink()
        backup = backup_path(deployment)
        deployment.rename(backup)
        log(f"deployment retained at {backup}")
    log(f"removed {removed_skills} generated skills; preserved edited skills: {len(preserved_skills)}")
    log("removed generated Codex hooks, MCP entries, and agent roles")
    return 0


def install(
    source: Path,
    codex_home: Path,
    no_trust: bool = False,
    setup_runtime: bool = True,
    enable_subagents: bool = False,
    setup_tldr: bool = True,
    enable_tldr_semantic: bool = False,
    tldr_semantic_binary: Path | None = None,
    enable_dashboard: bool = True,
) -> int:
    source = source.resolve()
    codex_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    deployment = codex_home / "continuous-claude-v3"
    hooks_path = codex_home / "hooks.json"
    config_path = codex_home / "config.toml"
    previous_deployment = copy_deployment(source, deployment)
    adapt_braintrust_plugin(deployment)
    bridge_source = Path(__file__).resolve().with_name("codex_continuous_bridge.py")
    bridge_target = deployment / bridge_source.name
    shutil.copy2(bridge_source, bridge_target)
    os.chmod(bridge_target, 0o755)
    write_runtime_env(deployment, previous_deployment)
    if setup_tldr:
        install_tldr_runtime(
            deployment,
            enable_semantic=enable_tldr_semantic,
            semantic_binary=tldr_semantic_binary,
        )
    elif enable_tldr_semantic or tldr_semantic_binary:
        raise RuntimeError("TLDR semantic setup requires TLDR setup; remove --skip-tldr-setup")
    native_manifest = install_native_resources(
        source,
        deployment,
        codex_home,
        enable_subagents=enable_subagents,
        enable_dashboard=enable_dashboard,
    )
    config_path = codex_home / "config.toml"
    set_subagent_feature(config_path, enable_subagents)
    native_manifest["mcp"]["registration"] = merge_mcp_config(
        config_path, deployment / "codex-native" / "mcp-servers.toml"
    )
    native_manifest["agents"]["registration"] = merge_agent_roles(
        config_path, native_manifest["agents"], enable_subagents
    )
    (deployment / "codex-native" / "native-manifest.json").write_text(
        json.dumps(native_manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (deployment / "install-config.json").write_text(
        json.dumps(
            {
                "subagents": {"enabled": enable_subagents},
                "dashboard": {"enabled": enable_dashboard},
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    log(
        f"converted {len(native_manifest['skills']['converted'])} skills; "
        f"subagents {'enabled' if enable_subagents else 'disabled'}; "
        f"dashboard {'installed' if enable_dashboard else 'omitted'}"
    )
    log(
        f"registered {len(native_manifest['mcp']['registration']['installed'])} MCP servers; "
        f"registered {len(native_manifest['agents']['registration']['installed'])} agent roles"
    )
    if setup_runtime:
        sync_python_runtime(deployment)
        start_database_stack(deployment)
    _, generated = build_hooks(source, deployment)
    if hooks_path.exists():
        backup = backup_path(hooks_path)
        shutil.copy2(hooks_path, backup)
        log(f"existing hooks.json copied to {backup}")
    merge_hooks_file(hooks_path, generated)
    log(f"deployed Continuous Claude assets to {deployment / '.claude'}")
    log(f"installed Codex hooks at {hooks_path}")

    if no_trust:
        log("skipped automatic hook trust (--no-trust)")
    else:
        codex_bin = os.environ.get("CODEX_BIN") or shutil.which("codex")
        if not codex_bin:
            log("codex executable not found; hooks remain untrusted")
        else:
            try:
                entries = discover_hook_entries(codex_bin, Path.cwd())
                count = write_trust_state(config_path, entries, hooks_path)
                log(f"trusted {count} generated Codex hook handlers")
            except Exception as error:  # noqa: BLE001  # trust persistence is best effort
                log(f"could not persist hook trust automatically: {error}")
                log("run the Codex hook review once, or rerun install.py later")
    write_record(deployment, source, hooks_path, config_path)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=default_source())
    parser.add_argument("--codex-home", type=Path, default=default_codex_home())
    parser.add_argument("--no-trust", action="store_true", help="do not call codex app-server to persist hashes")
    parser.add_argument(
        "--skip-runtime-setup",
        action="store_true",
        help="deploy files without running uv sync or starting PostgreSQL",
    )
    subagent_group = parser.add_mutually_exclusive_group()
    subagent_group.add_argument(
        "--enable-subagents",
        dest="subagents_override",
        action="store_true",
        help="enable Codex agents and register converted roles (overrides installer config)",
    )
    subagent_group.add_argument(
        "--disable-subagents",
        dest="subagents_override",
        action="store_false",
        help="disable Codex agents and remove generated role registrations (overrides installer config)",
    )
    dashboard_group = parser.add_mutually_exclusive_group()
    dashboard_group.add_argument(
        "--enable-dashboard",
        dest="dashboard_override",
        action="store_true",
        help="install the optional localhost dashboard (overrides installer config)",
    )
    dashboard_group.add_argument(
        "--disable-dashboard",
        dest="dashboard_override",
        action="store_false",
        help="omit the optional localhost dashboard (overrides installer config)",
    )
    parser.set_defaults(subagents_override=None, dashboard_override=None)
    parser.add_argument(
        "--config",
        type=Path,
        help=f"installer JSON config (default: {INSTALL_CONFIG_NAME} next to install.py)",
    )
    parser.add_argument("--uninstall", action="store_true", help="remove generated registrations and retain files as a backup")
    parser.add_argument("--skip-tldr-setup", action="store_true", help="do not install the deployment-local llm-tldr package")
    parser.add_argument(
        "--enable-tldr-semantic",
        action="store_true",
        help="deprecated compatibility flag; llm-tldr includes semantic search",
    )
    parser.add_argument(
        "--tldr-semantic-binary",
        type=Path,
        help="deprecated compatibility flag; llm-tldr includes semantic search",
    )
    args = parser.parse_args(argv)
    try:
        if args.uninstall:
            return uninstall(args.codex_home)
        options = resolve_install_options(
            args.config.expanduser() if args.config else None,
            subagents_override=args.subagents_override,
            dashboard_override=args.dashboard_override,
        )
        log(
            f"installer options: subagents {'enabled' if options['subagents_enabled'] else 'disabled'}, "
            f"dashboard {'enabled' if options['dashboard_enabled'] else 'disabled'}"
        )
        return install(
            args.source.expanduser(),
            args.codex_home.expanduser(),
            args.no_trust,
            setup_runtime=not args.skip_runtime_setup,
            enable_subagents=options["subagents_enabled"],
            enable_dashboard=options["dashboard_enabled"],
            setup_tldr=not args.skip_tldr_setup,
            enable_tldr_semantic=args.enable_tldr_semantic,
            tldr_semantic_binary=args.tldr_semantic_binary.expanduser() if args.tldr_semantic_binary else None,
        )
    except Exception as error:  # noqa: BLE001  # CLI reports a concise installation error
        print(f"[codex-continuous] installation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
