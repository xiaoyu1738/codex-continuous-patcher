from __future__ import annotations

import json
import os
import tempfile
import tomllib
import unittest
from unittest.mock import patch
from pathlib import Path

import codex_continuous_native as native
import codex_continuous_status as status
import install


class NativeResourceTests(unittest.TestCase):
    def test_skill_conversion_copies_resources_and_preserves_local_edits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / ".claude"
            skill = source / "skills" / "sample"
            (skill / "references").mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: sample\ndescription: |\n  First line\n  Second line\n---\n\n# Instructions\nRead references/guide.md.\n",
                encoding="utf-8",
            )
            (skill / "references" / "guide.md").write_text("reference content\n", encoding="utf-8")
            codex_home = root / "codex"
            result = native.convert_skills(source, root / "native", codex_home=codex_home)

            self.assertEqual(len(result["converted"]), 1)
            installed = Path(result["converted"][0]["path"]).parent
            converted_text = (installed / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("name: cc-codex-continuous-v3-sample", converted_text)
            self.assertIn('description: "First line\\nSecond line"', converted_text)
            self.assertEqual((installed / "references" / "guide.md").read_text(), "reference content\n")

            (installed / "local-note.md").write_text("keep this edit", encoding="utf-8")
            again = native.convert_skills(source, root / "native", codex_home=codex_home)
            self.assertEqual(again["skipped"][0]["reason"], "managed skill has local edits")
            self.assertEqual((installed / "local-note.md").read_text(), "keep this edit")

    def test_agent_markdown_and_json_prompts_become_codex_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source_agents = Path(temporary) / ".claude" / "agents"
            source_agents.mkdir(parents=True)
            (source_agents / "researcher.md").write_text(
                "---\nname: researcher\ndescription: Research role\nmodel: opus\n---\nFind evidence.\n",
                encoding="utf-8",
            )
            (source_agents / "reviewer.json").write_text(
                json.dumps({"name": "reviewer", "description": "Review role", "prompt": "Find defects."}),
                encoding="utf-8",
            )
            result = native.convert_agents(source_agents.parent, Path(temporary) / "native")
            self.assertEqual(len(result["agents"]), 2)
            role_configs = [tomllib.loads(Path(agent["path"]).read_text()) for agent in result["agents"]]
            prompts = {config["developer_instructions"] for config in role_configs}
            self.assertIn("Find evidence.\n", prompts)
            self.assertIn("Find defects.", prompts)

    def test_mcp_conversion_uses_codex_schema_and_preserves_disabled_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / ".claude"
            source.mkdir()
            (source / "mcp_config.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "fetch": {"command": "uvx", "args": ["mcp-server-fetch"]},
                            "qlty": {
                                "command": "python",
                                "args": ["$HOME/.claude/servers/qlty/server.py"],
                            },
                            "remote": {"type": "http", "url": "https://example.test/mcp"},
                            "disabled": {"command": "npx", "args": ["disabled"], "disabled": True},
                        }
                    }
                ),
                encoding="utf-8",
            )
            target = root / "deployment" / "codex-native"
            deployment_claude = root / "deployment" / ".claude"
            result = native.convert_mcp_config(source, target, codex_claude=deployment_claude)
            catalog = tomllib.loads((target / "mcp-servers.toml").read_text())
            servers = catalog["mcp_servers"]
            self.assertEqual(len(result["servers"]), 4)
            self.assertNotIn("type", servers["codex-continuous-fetch"])
            self.assertFalse(servers["codex-continuous-disabled"]["enabled"])
            self.assertEqual(
                servers["codex-continuous-qlty"]["args"][-1],
                str(deployment_claude / "servers" / "qlty" / "server.py"),
            )
            self.assertEqual(servers["codex-continuous-remote"]["url"], "https://example.test/mcp")


class InstallerConfigTests(unittest.TestCase):
    def test_installer_config_defaults_and_cli_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "missing.json"
            self.assertEqual(
                install.load_install_config(missing),
                {"subagents_enabled": False, "dashboard_enabled": True},
            )
            with self.assertRaises(FileNotFoundError):
                install.resolve_install_options(missing)
            config = root / "codex-continuous.config.json"
            config.write_text(
                json.dumps({"subagents": {"enabled": True}, "dashboard": {"enabled": False}}),
                encoding="utf-8",
            )
            self.assertEqual(
                install.resolve_install_options(config),
                {"subagents_enabled": True, "dashboard_enabled": False},
            )
            self.assertEqual(
                install.resolve_install_options(
                    config, subagents_override=False, dashboard_override=True
                ),
                {"subagents_enabled": False, "dashboard_enabled": True},
            )
            (root / "invalid.json").write_text(
                json.dumps({"subagents": {"enabled": "yes"}}), encoding="utf-8"
            )
            with self.assertRaises(RuntimeError):
                install.load_install_config(root / "invalid.json")

    def test_optional_resources_follow_install_switches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source_claude = source / ".claude"
            (source_claude / "agents").mkdir(parents=True)
            (source_claude / "agents" / "reviewer.md").write_text(
                "---\nname: reviewer\ndescription: Review\n---\nReview changes.\n",
                encoding="utf-8",
            )
            disabled_deployment = root / "disabled"
            disabled = install.install_native_resources(
                source,
                disabled_deployment,
                root / "codex-disabled",
                enable_subagents=False,
                enable_dashboard=False,
            )
            self.assertFalse(disabled["subagents_enabled"])
            self.assertFalse(disabled["dashboard_enabled"])
            self.assertEqual(disabled["agents"]["agents"], [])
            self.assertFalse((disabled_deployment / "codex_continuous_dashboard.py").exists())
            self.assertFalse((disabled_deployment / "codex-native" / "agents").exists())

            enabled_deployment = root / "enabled"
            enabled = install.install_native_resources(
                source,
                enabled_deployment,
                root / "codex-enabled",
                enable_subagents=True,
                enable_dashboard=True,
            )
            self.assertTrue(enabled["subagents_enabled"])
            self.assertTrue(enabled["dashboard_enabled"])
            self.assertEqual(len(enabled["agents"]["agents"]), 1)
            self.assertTrue((enabled_deployment / "codex_continuous_dashboard.py").exists())
            self.assertTrue((enabled_deployment / "codex-native" / "agents").is_dir())

    def test_merge_is_idempotent_and_uninstall_restores_user_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            config.write_text(
                '[features]\nmulti_agent = true\nother_feature = true\n\n'
                '[agents]\nenabled = true\nmax_depth = 3\n\n'
                '[mcp_servers."codex-continuous-fetch"]\ncommand = "my-fetch"\n',
                encoding="utf-8",
            )
            install.set_subagent_feature(config, False)
            catalog = root / "mcp-servers.toml"
            catalog.write_text(
                '[mcp_servers."codex-continuous-fetch"]\nenabled = true\ncommand = "uvx"\n\n'
                '[mcp_servers."codex-continuous-git"]\nenabled = true\ncommand = "uvx"\nargs = ["mcp-server-git"]\n\n'
                '[mcp_servers."codex-continuous-disabled"]\nenabled = false\ncommand = "npx"\n',
                encoding="utf-8",
            )
            merged = install.merge_mcp_config(config, catalog)
            self.assertEqual(merged["installed"], ["codex-continuous-git"])
            self.assertEqual(install.merge_mcp_config(config, catalog)["installed"], ["codex-continuous-git"])
            agent_result = install.merge_agent_roles(
                config,
                {
                    "agents": [
                        {
                            "name": "codex-continuous-reviewer",
                            "description": "Review changes",
                            "config_file": "continuous-claude-v3/codex-native/agents/reviewer.toml",
                        }
                    ]
                },
                True,
            )
            self.assertEqual(agent_result["installed"], ["codex-continuous-reviewer"])
            install.set_subagent_feature(config, True)

            parsed = tomllib.loads(config.read_text())
            self.assertTrue(parsed["features"]["multi_agent"])
            self.assertTrue(parsed["agents"]["enabled"])
            self.assertIn("codex-continuous-fetch", parsed["mcp_servers"])
            self.assertIn("codex-continuous-git", parsed["mcp_servers"])
            self.assertIn("codex-continuous-reviewer", parsed["agents"])
            self.assertEqual(parsed["mcp_servers"]["codex-continuous-fetch"]["command"], "my-fetch")

            cleaned = install._strip_block(config.read_text(), install.MCP_BEGIN, install.MCP_END)
            cleaned = install._strip_block(cleaned, install.AGENTS_BEGIN, install.AGENTS_END)
            install._atomic_config_write(config, cleaned)
            install.restore_managed_setting(config, "features.multi_agent_v2", "enabled")
            install.restore_managed_setting(config, "features", "multi_agent_v2")
            install.restore_managed_setting(config, "features", "multi_agent")
            install.restore_managed_setting(config, "agents", "enabled")
            restored = tomllib.loads(config.read_text())
            self.assertTrue(restored["features"]["multi_agent"])
            self.assertTrue(restored["features"]["other_feature"])
            self.assertTrue(restored["agents"]["enabled"])
            self.assertEqual(restored["agents"]["max_depth"], 3)
            self.assertEqual(set(restored["mcp_servers"]), {"codex-continuous-fetch"})

    def test_braintrust_plugin_hooks_are_registered_as_codex_handlers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            deployment = root / "deployment"
            plugin = deployment / ".claude" / "plugins" / "braintrust-tracing"
            (source / ".claude").mkdir(parents=True)
            (plugin / "hooks").mkdir(parents=True)
            (source / ".claude" / "settings.json").write_text(
                json.dumps({"hooks": {}}), encoding="utf-8"
            )
            (plugin / "hooks" / "common.sh").write_text(
                '[ -f "$HOME/.claude/.env" ] && set -a && source "$HOME/.claude/.env" && set +a\n'
                'export PROJECT="${BRAINTRUST_CC_PROJECT:-claude-code}"\n',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"CODEX_CONTINUOUS_ENABLE_BRAINTRUST": "1"}):
                specs = install.braintrust_hook_specs(deployment)
            for _, _, path, _ in specs:
                path.write_text("#!/bin/bash\n", encoding="utf-8")
            install.adapt_braintrust_plugin(deployment)
            common = (plugin / "hooks" / "common.sh").read_text()
            self.assertNotIn("$HOME/.claude/.env", common)
            self.assertIn("BRAINTRUST_CC_PROJECT:-codex", common)

            with patch.dict(os.environ, {"CODEX_CONTINUOUS_ENABLE_BRAINTRUST": "1"}):
                manifest, hooks = install.build_hooks(source, deployment)
            self.assertEqual(len([key for key in manifest["hooks"] if key.startswith("braintrust_")]), 5)
            post_tool = hooks["hooks"]["PostToolUse"][0]["hooks"][0]
            self.assertIn("--event", post_tool["command"])
            self.assertIn("PostToolUse", post_tool["command"])

    def test_semantic_search_hook_gets_model_load_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            deployment = root / "deployment"
            (source / ".claude").mkdir(parents=True)
            deployment.mkdir()
            (source / ".claude" / "settings.json").write_text(
                json.dumps(
                    {
                        "hooks": {
                            "PreToolUse": [
                                {
                                    "matcher": "Grep",
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "node $HOME/.claude/hooks/dist/smart-search-router.mjs",
                                            "timeout": 10,
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            manifest, generated = install.build_hooks(source, deployment)
            self.assertEqual(manifest["hooks"]["pretooluse_0_0"]["timeout"], 60)
            self.assertEqual(generated["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"], 60)

    def test_cross_repository_change_tracker_is_registered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            deployment = root / "deployment"
            (source / ".claude").mkdir(parents=True)
            deployment.mkdir()
            (source / ".claude" / "settings.json").write_text(
                json.dumps({"hooks": {}}), encoding="utf-8"
            )
            (deployment / "codex_continuous_change_tracker.py").write_text(
                "#!/usr/bin/env python3\n", encoding="utf-8"
            )
            manifest, _ = install.build_hooks(source, deployment)
            tracked = [
                hook
                for hook in manifest["hooks"].values()
                if str(hook.get("id", "")).startswith("codex_change_tracker_")
            ]
            self.assertEqual({hook["event"] for hook in tracked}, {"PreToolUse", "PostToolUse"})

    def test_status_reports_multi_agent_v2_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            config.write_text(
                "[features]\nmulti_agent = false\n\n"
                "[features.multi_agent_v2]\nenabled = true\n\n"
                "[agents]\nenabled = false\n",
                encoding="utf-8",
            )
            self.assertTrue(status._subagents_enabled(config))
            config.write_text(config.read_text().replace("enabled = true", "enabled = false"), encoding="utf-8")
            self.assertFalse(status._subagents_enabled(config))

    def test_global_rule_merge_preserves_user_instructions_and_skips_claude_only_rules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rules = root / "source" / ".claude" / "rules"
            rules.mkdir(parents=True)
            for name in (
                "claim-verification.md",
                "proactive-delegation.md",
                "hook-auto-execute.md",
            ):
                (rules / name).write_text("source rule\n", encoding="utf-8")
            codex_home = root / "codex"
            codex_home.mkdir()
            agents_file = codex_home / "AGENTS.md"
            agents_file.write_text("User instructions stay here.\n", encoding="utf-8")

            result = install.merge_global_rules(root / "source", codex_home)
            self.assertTrue(result["installed"])
            self.assertIn("User instructions stay here.", agents_file.read_text())
            self.assertEqual(len(result["adopted"]), 1)
            self.assertEqual(len(result["not_imported"]), 2)
            recall_rule = root / "source" / ".claude" / "rules" / "dynamic-recall.md"
            recall_rule.write_text(
                "cd $CODEX_CONTINUOUS_ROOT/opc && Run scripts/core/recall_learnings.py and use /recall.\n",
                encoding="utf-8",
            )
            install.merge_global_rules(root / "source", codex_home)
            adapted = agents_file.read_text(encoding="utf-8")
            self.assertIn("scripts/core/recall_learnings.py", adapted)
            self.assertIn("${CODEX_CONTINUOUS_ROOT:-" + str(codex_home / "continuous-claude-v3"), adapted)
            self.assertIn("/cc-codex-continuous-v3-recall", adapted)
            self.assertNotIn("cc-codex-continuous-v3-recall_learnings.py", adapted)
            install.merge_global_rules(root / "source", codex_home)
            self.assertEqual(agents_file.read_text().count(install.RULES_BEGIN), 1)
            self.assertTrue(install.remove_global_rules(codex_home))
            self.assertEqual(agents_file.read_text().strip(), "User instructions stay here.")

    def test_uninstall_removes_only_an_agents_file_created_by_installer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rules = root / "source" / ".claude" / "rules"
            rules.mkdir(parents=True)
            (rules / "claim-verification.md").write_text("rule", encoding="utf-8")
            codex_home = root / "codex"
            codex_home.mkdir()
            install.merge_global_rules(root / "source", codex_home)
            self.assertTrue((codex_home / "AGENTS.md").is_file())
            install.remove_global_rules(codex_home)
            self.assertFalse((codex_home / "AGENTS.md").exists())



if __name__ == "__main__":
    unittest.main()
