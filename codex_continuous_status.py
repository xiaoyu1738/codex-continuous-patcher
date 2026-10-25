#!/usr/bin/env python3
"""Report the installed Continuous Codex runtime and project state."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _run(
    command: list[str], cwd: Path | None = None, env: dict[str, str] | None = None
) -> str:
    try:
        result = subprocess.run(
            command, cwd=cwd, env=env, text=True, capture_output=True, check=False, timeout=5
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _latest_handoff(root: Path) -> dict[str, Any] | None:
    handoff_root = root / "thoughts" / "shared" / "handoffs"
    if not handoff_root.is_dir():
        return None
    files = [
        path
        for path in handoff_root.glob("**/*")
        if path.is_file() and path.suffix in {".yaml", ".yml", ".md"}
    ]
    if not files:
        return None
    path = max(files, key=lambda item: item.stat().st_mtime)
    return {
        "path": str(path),
        "name": path.name,
        "mtime": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
    }


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def _memory_counts(deployment: Path) -> dict[str, Any]:
    env_file = deployment / ".env"
    values = _read_env(env_file)
    database_url = values.get("CONTINUOUS_CLAUDE_DB_URL") or os.environ.get(
        "CONTINUOUS_CLAUDE_DB_URL"
    )
    if not database_url:
        return {"available": False, "reason": "database URL is not configured"}

    virtualenv_python = deployment / "opc" / ".venv" / "bin" / "python"
    python = str(virtualenv_python) if virtualenv_python.is_file() else sys.executable
    probe = (
        "import asyncio, json, os, asyncpg\n"
        "async def main():\n"
        " conn = await asyncpg.connect(os.environ['CONTINUOUS_CLAUDE_DB_URL'], timeout=3)\n"
        " try:\n"
        "  result = {}\n"
        "  for table in ('archival_memory', 'core_memory'):\n"
        "   exists = await conn.fetchval('SELECT to_regclass($1)', 'public.' + table)\n"
        "   result[table] = await conn.fetchval('SELECT count(*) FROM ' + table) if exists else None\n"
        "  print(json.dumps({'available': True, **result}))\n"
        " finally:\n"
        "  await conn.close()\n"
        "asyncio.run(main())\n"
    )
    env = os.environ.copy()
    env["CONTINUOUS_CLAUDE_DB_URL"] = database_url
    try:
        result = subprocess.run(
            [python, "-c", probe],
            cwd=deployment / "opc" if (deployment / "opc").is_dir() else deployment,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"available": False, "reason": type(error).__name__}
    if result.returncode != 0:
        return {
            "available": False,
            "reason": "asyncpg is not installed" if "No module named 'asyncpg'" in result.stderr else "database unavailable",
        }
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"available": False, "reason": "invalid database probe response"}


def _subagents_enabled(config_path: Path) -> bool:
    if not config_path.is_file():
        return True
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    features = config.get("features", {})
    agents = config.get("agents", {})
    if isinstance(features, dict):
        v2 = features.get("multi_agent_v2")
        if isinstance(v2, dict):
            return bool(v2.get("enabled", True))
        if isinstance(v2, bool):
            return v2
        if isinstance(features.get("multi_agent"), bool):
            return features["multi_agent"]
    if isinstance(agents, dict) and isinstance(agents.get("enabled"), bool):
        return agents["enabled"]
    return True


def _hook_count(hooks_file: Path) -> int:
    try:
        hooks = json.loads(hooks_file.read_text(encoding="utf-8")) if hooks_file.is_file() else {}
    except (OSError, json.JSONDecodeError):
        return 0
    return sum(
        len(group.get("hooks", []))
        for groups in hooks.get("hooks", {}).values()
        if isinstance(groups, list)
        for group in groups
        if isinstance(group, dict) and isinstance(group.get("hooks"), list)
    )


def collect(root: Path, deployment: Path, codex_home: Path) -> dict[str, Any]:
    git_status = _run(["git", "status", "--short"], root)
    docker_status = "not installed"
    docker = shutil.which("docker")
    if docker:
        docker_env = os.environ.copy()
        docker_config = deployment / ".docker-config"
        if docker_config.is_dir():
            docker_env["DOCKER_CONFIG"] = str(docker_config)
        docker_status = _run(
            [docker, "ps", "--filter", "name=continuous-claude-postgres", "--format", "{{.Status}}"],
            env=docker_env,
        ) or "not running"

    native_dir = deployment / "codex-native"
    manifests: dict[str, Any] = {}
    for name in ("skills-manifest.json", "agents-manifest.json", "mcp-manifest.json"):
        path = native_dir / name
        if path.is_file():
            try:
                manifests[name] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                manifests[name] = {}
    skill_manifest = manifests.get("skills-manifest.json", {})
    agent_manifest = manifests.get("agents-manifest.json", {})
    mcp_manifest = manifests.get("mcp-manifest.json", {})
    mcp_registration: dict[str, Any] = {}
    agent_registration: dict[str, Any] = {}
    rule_manifest: dict[str, Any] = {}
    native_manifest = native_dir / "native-manifest.json"
    if native_manifest.is_file():
        try:
            manifest = json.loads(native_manifest.read_text(encoding="utf-8"))
            mcp_registration = manifest.get("mcp", {}).get("registration", {})
            agent_registration = manifest.get("agents", {}).get("registration", {})
            rule_manifest = manifest.get("rules", {})
        except (OSError, json.JSONDecodeError):
            pass
    codex = shutil.which("codex")
    tldr_binary = deployment / "bin" / "tldr"
    if not tldr_binary.is_file():
        tldr_command = shutil.which("tldr")
        tldr_binary = Path(tldr_command) if tldr_command else Path()
    tldr_version = _run([str(tldr_binary), "--version"]) if tldr_binary.is_file() else ""
    tldr_runtime: dict[str, Any] = {}
    runtime_metadata = deployment / "bin" / "tldr-runtime.json"
    if runtime_metadata.is_file():
        try:
            loaded_runtime = json.loads(runtime_metadata.read_text(encoding="utf-8"))
            if isinstance(loaded_runtime, dict):
                tldr_runtime = loaded_runtime
        except (OSError, json.JSONDecodeError):
            pass
    runtime_env = _read_env(deployment / ".env")
    trace_enabled = runtime_env.get("TRACE_TO_BRAINTRUST", os.environ.get("TRACE_TO_BRAINTRUST", "false"))
    api_key_present = bool(runtime_env.get("BRAINTRUST_API_KEY") or os.environ.get("BRAINTRUST_API_KEY"))
    return {
        "project": str(root),
        "deployment": str(deployment),
        "codex_version": _run([codex, "--version"]) if codex else "not installed",
        "hooks_registered": _hook_count(codex_home / "hooks.json"),
        "subagents_enabled": _subagents_enabled(codex_home / "config.toml"),
        "skills_installed": len(skill_manifest.get("skills", [])),
        "skill_collisions": len(skill_manifest.get("skipped", [])),
        "agent_roles_available": len(agent_manifest.get("agents", [])),
        "agent_roles_registered": len(agent_registration.get("installed", [])),
        "global_rules_installed": bool(rule_manifest.get("installed")),
        "mcp_configured": len(mcp_registration.get("installed", [])),
        "mcp_available": len(mcp_manifest.get("servers", [])),
        "tldr_available": bool(tldr_version),
        "tldr_version": tldr_version,
        "tldr_semantic_enabled": bool(tldr_runtime.get("semantic_enabled"))
        and (deployment / "tldr-venv" / "bin" / "python").is_file(),
        "tldr_semantic_source": tldr_runtime.get("semantic_source"),
        "braintrust_enabled": trace_enabled.lower() == "true" and api_key_present,
        "docker_status": docker_status,
        "memory": _memory_counts(deployment),
        "latest_handoff": _latest_handoff(root),
        "git_status": git_status.splitlines(),
    }


def main() -> int:
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser().resolve()
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--deployment", type=Path, default=codex_home / "continuous-claude-v3")
    parser.add_argument("--codex-home", type=Path, default=codex_home)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = collect(args.project.expanduser().resolve(), args.deployment.expanduser().resolve(), args.codex_home.expanduser().resolve())
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    print(f"Continuous Codex status: {report['project']}")
    print(f"  Codex: {report['codex_version']}")
    print(f"  hooks: {report['hooks_registered']}")
    print(f"  subagents: {'enabled' if report['subagents_enabled'] else 'disabled'}")
    print(f"  skills: {report['skills_installed']} installed ({report['skill_collisions']} skipped)")
    print(f"  agent roles: {report['agent_roles_registered']} registered / {report['agent_roles_available']} available")
    print(f"  MCP: {report['mcp_configured']} configured / {report['mcp_available']} available")
    print(f"  tldr: {'available' if report['tldr_available'] else 'not installed'} ({report['tldr_version'] or 'n/a'})")
    print(f"  tldr semantic: {'enabled' if report['tldr_semantic_enabled'] else 'lexical fallback'}")
    print(f"  PostgreSQL: {report['docker_status']}")
    print(f"  memory: {json.dumps(report['memory'], ensure_ascii=False)}")
    print(f"  latest handoff: {report['latest_handoff'] or 'none'}")
    print(f"  git changes: {len(report['git_status'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
