#!/usr/bin/env python3
"""Convert Continuous Claude resources into Codex-native resources."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any


NAMESPACE = "codex-continuous"
SKILL_OWNER_FILE = ".codex-continuous-patcher.json"


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_key(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return value
    return _toml_string(value)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ValueError("non-finite floats are not valid generated MCP values")
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return _toml_string(str(value))


def _frontmatter(content: str) -> tuple[dict[str, Any], str]:
    match = re.match(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", content, re.DOTALL)
    if not match:
        return {}, content
    raw, body = match.group(1), content[match.end() :]
    try:
        import yaml  # type: ignore[import-not-found]

        metadata = yaml.safe_load(raw)
        if isinstance(metadata, dict):
            return metadata, body
    except Exception:  # YAML is optional for the standalone installer.
        pass
    metadata: dict[str, Any] = {}
    lines = raw.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if ":" not in line or line[:1].isspace():
            index += 1
            continue
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if value in {"|", "|-", "|+", ">", ">-", ">+"}:
            block: list[str] = []
            index += 1
            while index < len(lines) and (not lines[index] or lines[index][:1].isspace()):
                block.append(lines[index][2:] if lines[index].startswith("  ") else lines[index].lstrip())
                index += 1
            separator = " " if value.startswith(">") else "\n"
            metadata[key] = separator.join(block).strip()
            continue
        if value.startswith('"'):
            try:
                metadata[key] = json.loads(value)
            except json.JSONDecodeError:
                metadata[key] = value.strip('"')
        else:
            metadata[key] = value.strip("'")
        index += 1
    return metadata, body


def _slug(value: str, limit: int = 42) -> str:
    result = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-_ ").lower()
    return (result or "resource")[:limit].strip("-_") or "resource"


def _tree_digest(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.name == SKILL_OWNER_FILE:
            continue
        relative = path.relative_to(directory).as_posix().encode()
        digest.update(relative + b"\0")
        if path.is_symlink():
            digest.update(b"link\0" + os.readlink(path).encode(errors="surrogateescape"))
        elif path.is_file():
            digest.update(b"file\0" + path.read_bytes())
        elif path.is_dir():
            digest.update(b"dir\0")
    return digest.hexdigest()


def _adapt_rlm_runtime_text(content: str) -> str:
    """Point v4.7 prompt examples at the installed Codex launcher."""

    content = content.replace("python tools/ouros_harness.py", "ouros-harness")
    return content.replace("tools/ouros_harness.py", "ouros-harness")


def _skill_text(
    source_file: Path,
    target_name: str,
    original_name: str,
    *,
    source_label: str = "Continuous-Claude-v3",
) -> str:
    content = source_file.read_text(encoding="utf-8", errors="replace")
    metadata, body = _frontmatter(content)
    description = str(metadata.get("description") or f"Continuous Claude skill: {original_name}")
    compatibility = ""
    if original_name.lower().startswith("tldr-"):
        compatibility = (
            "\n## Codex runtime note\n\n"
            "The Codex install uses the original Python `llm-tldr` package through a local adapter. "
            "Its semantic command is `tldr semantic index`; it stores the upstream FAISS index and metadata under "
            "`.tldr/cache/semantic/`.\n"
        )
    if source_label == "ContinuousClaudeV4.7":
        compatibility += (
            "\n## Codex RLM runtime note\n\n"
            "The v4.7 RLM runtime is installed as `ouros-harness` and keeps raw state "
            "under `continuum/rlm/`. Bash is pre-wrapped through `rlm_runner.py`; "
            "large command output is stored under `/tmp/codex-continuous-rlm/` and "
            "only a bounded card is returned to the main context.\n"
        )
        body = _adapt_rlm_runtime_text(body)
    return (
        "---\n"
        f"name: {target_name}\n"
        f"description: {_toml_string(description)}\n"
        "---\n"
        + compatibility
        + body
    )


def convert_skills(
    source_claude: Path,
    target_root: Path,
    *,
    codex_home: Path | None = None,
    skill_prefix: str = "cc-codex-continuous-v3",
    source_label: str = "Continuous-Claude-v3",
    manifest_name: str = "skills-manifest.json",
) -> dict[str, Any]:
    """Install complete skill directories, translating Codex frontmatter only."""

    source_skills = source_claude / "skills"
    target_skills = (codex_home or target_root) / "skills"
    target_skills.mkdir(parents=True, exist_ok=True)
    target_root.mkdir(parents=True, exist_ok=True)
    converted: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []

    for directory in sorted(source_skills.iterdir()) if source_skills.is_dir() else []:
        source_file = directory / "SKILL.md"
        if not directory.is_dir() or not source_file.is_file():
            continue
        content = source_file.read_text(encoding="utf-8", errors="replace")
        metadata, _ = _frontmatter(content)
        original_name = str(metadata.get("name") or directory.name)
        name = f"{skill_prefix}-{_slug(original_name, 34)}"
        target_dir = target_skills / name
        if target_dir.exists():
            owner_path = target_dir / SKILL_OWNER_FILE
            if not owner_path.is_file():
                suffix = hashlib.sha256(directory.name.encode()).hexdigest()[:8]
                target_dir = target_skills / f"{name}-{suffix}"
                name = target_dir.name
                if target_dir.exists() and not (target_dir / SKILL_OWNER_FILE).is_file():
                    skipped.append({"source": original_name, "reason": "name collision"})
                    continue
            else:
                try:
                    owner = json.loads(owner_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    owner = {}
                if owner.get("installed_hash") != _tree_digest(target_dir):
                    skipped.append({"source": original_name, "reason": "managed skill has local edits"})
                    continue

        temporary = target_skills / f".{name}.staging"
        if temporary.exists():
            shutil.rmtree(temporary)
        shutil.copytree(source_file.parent, temporary, symlinks=True)
        (temporary / "SKILL.md").write_text(
            _skill_text(source_file, name, original_name, source_label=source_label), encoding="utf-8"
        )
        source_hash = _tree_digest(temporary)
        (temporary / SKILL_OWNER_FILE).write_text(
            json.dumps(
                {"source": source_label, "source_name": directory.name, "installed_hash": source_hash},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        if target_dir.exists():
            shutil.rmtree(target_dir)
        temporary.replace(target_dir)
        converted.append({"source": original_name, "name": name, "path": str(target_dir / "SKILL.md")})

    manifest = {
        "version": 1,
        "source": source_label,
        "skills": converted,
        "skipped": skipped,
        "resources_copied": True,
    }
    (target_root / manifest_name).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return {"converted": converted, "skipped": skipped}


def convert_agents(
    source_claude: Path,
    target_root: Path,
    *,
    role_prefix: str = NAMESPACE,
    source_label: str = "Continuous-Claude-v3",
    manifest_name: str = "agents-manifest.json",
    config_root: str = "continuous-claude-v3/codex-native/agents",
) -> dict[str, Any]:
    """Convert Claude agent prompts to Codex role config files."""

    source_agents = source_claude / "agents"
    target_agents = target_root / "agents"
    target_agents.mkdir(parents=True, exist_ok=True)
    agents: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    if source_agents.is_dir():
        sources = sorted(source_agents.glob("*.md"))
        sources.extend(path for path in sorted(source_agents.glob("*.json")) if not (source_agents / f"{path.stem}.md").is_file())
        for source in sources:
            if source.suffix.lower() == ".json":
                try:
                    data = json.loads(source.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as error:
                    skipped.append({"source": source.stem, "reason": f"invalid JSON agent: {error}"})
                    continue
                if not isinstance(data, dict) or not isinstance(data.get("prompt"), str):
                    skipped.append({"source": source.stem, "reason": "JSON agent has no prompt string"})
                    continue
                metadata, body = data, data["prompt"]
            else:
                metadata, body = _frontmatter(source.read_text(encoding="utf-8", errors="replace"))
            if source_label == "ContinuousClaudeV4.7":
                body = _adapt_rlm_runtime_text(body)
            original_name = str(metadata.get("name") or source.stem)
            role = f"{role_prefix}-{_slug(original_name)}"
            target = target_agents / f"{role}.toml"
            if target.exists():
                role = f"{role}-{hashlib.sha256(source.name.encode()).hexdigest()[:8]}"
                target = target_agents / f"{role}.toml"
            description = str(metadata.get("description") or f"Adapted role from {original_name}")
            target.write_text(
                "# Generated from a Continuous Claude agent prompt.\n"
                f"developer_instructions = {_toml_string(body)}\n",
                encoding="utf-8",
            )
            agents.append(
                {
                    "source": original_name,
                    "name": role,
                    "description": description,
                    "source_file": source.name,
                    "config_file": f"{config_root}/{target.name}",
                    "path": str(target),
                }
            )
    manifest = {
        "version": 1,
        "enabled": False,
        "reason": "Role prompts are installed and registered only when the subagent option is enabled.",
        "source": source_label,
        "agents": agents,
        "skipped": skipped,
    }
    (target_root / manifest_name).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return manifest


def _rewrite_server_path(value: str, codex_claude: Path | None) -> str:
    if not codex_claude or ".claude/servers/" not in value:
        return value
    suffix = value.split(".claude/servers/", 1)[1]
    return str(codex_claude / "servers" / suffix)


def _server_values(raw: dict[str, Any], codex_claude: Path | None) -> tuple[dict[str, Any] | None, str | None]:
    result: dict[str, Any] = {}
    url = raw.get("url")
    command = raw.get("command")
    if isinstance(url, str) and url:
        result["url"] = url
        headers = raw.get("headers", raw.get("http_headers"))
        if isinstance(headers, dict) and headers:
            result["http_headers"] = {str(k): str(v) for k, v in headers.items()}
        env_headers = raw.get("env_http_headers")
        if isinstance(env_headers, dict) and env_headers:
            result["env_http_headers"] = {str(k): str(v) for k, v in env_headers.items()}
    elif isinstance(command, str) and command:
        result["command"] = _rewrite_server_path(command, codex_claude)
        args = raw.get("args", [])
        if not isinstance(args, list) or not all(isinstance(item, (str, int, float, bool)) for item in args):
            return None, "unsupported argument format"
        result["args"] = [_rewrite_server_path(str(item), codex_claude) for item in args]
        if (
            codex_claude
            and Path(command).name in {"python", "python3"}
            and any("/servers/qlty/server.py" in item for item in result["args"])
        ):
            deployment = codex_claude.parent
            script_index = next(i for i, item in enumerate(result["args"]) if "/servers/qlty/server.py" in item)
            script = result["args"][script_index]
            result["command"] = "uv"
            result["args"] = ["run", "--project", str(deployment / "opc"), "python", script]
        env = raw.get("env")
        if isinstance(env, dict) and env:
            result["env"] = {str(k): str(v) for k, v in env.items()}
    else:
        return None, "missing supported command or URL transport"
    if "startup_timeout_sec" in raw:
        result["startup_timeout_sec"] = raw["startup_timeout_sec"]
    result["enabled"] = raw.get("disabled") is not True
    return result, None


def _mcp_filter_requested(source_name: str, raw: dict[str, Any]) -> bool:
    """Enable the stdio response filter explicitly or for CUA-like servers."""

    if "rlm_filter" in raw:
        return raw.get("rlm_filter") is True
    name = source_name.lower()
    return any(marker in name for marker in ("cua", "computer-use", "browser", "chrome"))


def _wrap_mcp_stdio(
    source_name: str,
    raw: dict[str, Any],
    values: dict[str, Any],
    wrapper: Path | None,
) -> bool:
    """Replace a selected stdio server command with the filtering proxy."""

    if wrapper is None or not _mcp_filter_requested(source_name, raw):
        return False
    command = values.get("command")
    args = values.get("args")
    if not isinstance(command, str) or not isinstance(args, list):
        return False
    values["command"] = "python3"
    values["args"] = [
        str(wrapper),
        "--command",
        command,
        "--args-json",
        json.dumps([str(item) for item in args], ensure_ascii=False),
    ]
    return True


def _render_server(name: str, config: dict[str, Any]) -> list[str]:
    lines = [f"[mcp_servers.{_toml_key(name)}]"]
    for key, value in config.items():
        if key == "env" or key in {"http_headers", "env_http_headers"}:
            continue
        lines.append(f"{key} = {_toml_value(value)}")
    for table in ("env", "http_headers", "env_http_headers"):
        values = config.get(table)
        if not isinstance(values, dict) or not values:
            continue
        lines.extend(["", f"[mcp_servers.{_toml_key(name)}.{table}]"])
        for key, value in sorted(values.items()):
            lines.append(f"{_toml_key(str(key))} = {_toml_value(value)}")
    return lines + [""]


def convert_mcp_config(
    source_claude: Path,
    target_root: Path,
    *,
    codex_claude: Path | None = None,
    mcp_filter_path: Path | None = None,
) -> dict[str, Any]:
    """Generate Codex-compatible MCP entries and a reviewable TOML catalog."""

    source_file = source_claude / "mcp_config.json"
    output_file = target_root / "mcp-servers.toml"
    config = json.loads(source_file.read_text(encoding="utf-8")) if source_file.is_file() else {}
    servers = config.get("mcpServers", {}) if isinstance(config, dict) else {}
    converted: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    lines = [
        "# Generated Codex MCP catalog. Active source servers are registered by the installer.",
        "# Disabled source servers remain disabled and are not added to config.toml.",
        "",
    ]
    for source_name, raw in sorted(servers.items()):
        if not isinstance(raw, dict):
            skipped.append({"source": str(source_name), "reason": "server entry is not a table"})
            continue
        values, reason = _server_values(raw, codex_claude)
        if values is None:
            skipped.append({"source": str(source_name), "reason": reason or "unsupported server"})
            continue
        name = f"{NAMESPACE}-{_slug(str(source_name))}"
        filtered = _wrap_mcp_stdio(str(source_name), raw, values, mcp_filter_path)
        item = {"source": str(source_name), "name": name, "enabled": values["enabled"]}
        if filtered:
            item["rlm_filter"] = True
        converted.append(item)
        lines.extend(_render_server(name, values))
    target_root.mkdir(parents=True, exist_ok=True)
    output_file.write_text("\n".join(lines), encoding="utf-8")
    os.chmod(output_file, 0o600)
    manifest = {"servers": converted, "skipped": skipped, "path": str(output_file)}
    (target_root / "mcp-manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return manifest


def convert_all(source_claude: Path, target_root: Path, *, codex_home: Path | None = None) -> dict[str, Any]:
    target_root.mkdir(parents=True, exist_ok=True)
    return {
        "skills": convert_skills(source_claude, target_root, codex_home=codex_home),
        "agents": convert_agents(source_claude, target_root),
        "mcp": convert_mcp_config(source_claude, target_root),
    }
