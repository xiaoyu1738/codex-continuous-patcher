from __future__ import annotations

import importlib.util
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import codex_continuous_bridge as bridge
import codex_continuous_mcp_filter as mcp_filter
import codex_continuous_rlm as rlm
import install


def load_harness():
    path = Path(__file__).parents[1] / "rlm" / "tools" / "ouros_harness.py"
    spec = importlib.util.spec_from_file_location("v47_ouros_harness", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load the vendored Ouros harness")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RLMOutputTests(unittest.TestCase):
    def test_pretool_bash_rewrites_command_to_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = rlm.pretool_bash({
                "cwd": str(root),
                "session_id": "session/with unsafe chars",
                "tool_name": "Bash",
                "tool_input": {"command": "printf '%s\\n' hello"},
            })
            specific = result["hookSpecificOutput"]
            wrapped = specific["updatedInput"]["command"]
            self.assertIn("--exec", wrapped)
            self.assertTrue("rlm_runner.py" in wrapped or "codex_continuous_rlm.py" in wrapped)
            script = Path(wrapped.split("--exec", 1)[1].split()[0].strip("'\""))
            self.assertTrue(script.is_file())
            self.assertEqual(stat.S_IMODE(script.stat().st_mode), 0o700)
            self.assertIn("printf", script.read_text())
            completed = subprocess.run(
                shlex.split(wrapped), cwd=root, capture_output=True, text=True, check=False
            )
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(completed.stdout, "hello\n")
            self.assertFalse(script.exists())

    def test_stale_runner_files_are_reaped_without_relying_on_tmp_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commands = root / "commands"
            session = root / "old-session"
            commands.mkdir()
            session.mkdir()
            stale_command = commands / "orphan.sh"
            stale_artifact = session / "old.log"
            stale_command.write_text("#!/bin/sh\n")
            stale_artifact.write_bytes(b"old")
            old = datetime.now().timestamp() - 100
            os.utime(stale_command, (old, old))
            os.utime(stale_artifact, (old, old))
            with patch.dict(
                os.environ,
                {
                    "CODEX_CONTINUOUS_RLM_BASH_ARTIFACT_DIR": str(root),
                    "CODEX_CONTINUOUS_RLM_RETENTION_SECONDS": "1",
                },
                clear=False,
            ):
                rlm._cleanup_stale()
            self.assertFalse(stale_command.exists())
            self.assertFalse(stale_artifact.exists())
            self.assertFalse(commands.exists())
            self.assertFalse(session.exists())

    def test_legacy_tmp_script_is_reaped(self) -> None:
        legacy = Path(tempfile.gettempdir()) / "codex-rlm-cmd-test-orphan.sh"
        legacy.write_text("#!/bin/sh\n")
        old = datetime.now().timestamp() - 100
        os.utime(legacy, (old, old))
        try:
            with patch.dict(os.environ, {"CODEX_CONTINUOUS_RLM_RETENTION_SECONDS": "1"}, clear=False):
                rlm._cleanup_stale()
            self.assertFalse(legacy.exists())
        finally:
            legacy.unlink(missing_ok=True)

    def test_small_output_falls_through_runner_and_preserves_returncode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "cmd.sh"
            script.write_text("#!/bin/sh\nprintf 'small\\n'\nprintf 'err\\n' >&2\nexit 7\n")
            script.chmod(0o700)
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).parents[1] / "codex_continuous_rlm.py"), "--exec", str(script), "--session", "small"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 7)
            self.assertEqual(completed.stdout, "small\n")
            self.assertEqual(completed.stderr, "err\n")

    def test_large_runner_output_is_compressed_and_preserves_returncode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "cmd.sh"
            script.write_text("#!/bin/sh\nprintf 'x%.0s' $(seq 1 20000)\nprintf 'failure\\n' >&2\nexit 9\n")
            script.chmod(0o700)
            env = os.environ.copy()
            env["CODEX_CONTINUOUS_RLM_BASH_ARTIFACT_DIR"] = str(root / "artifacts")
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).parents[1] / "codex_continuous_rlm.py"), "--exec", str(script), "--session", "large"],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(completed.returncode, 9)
            self.assertLess(len(completed.stdout.encode()), 4_000)
            self.assertIn("artifact:", completed.stdout)
            self.assertNotIn("failure", completed.stderr)
            artifacts = list((root / "artifacts" / "large").glob("*.log"))
            self.assertEqual(len(artifacts), 1)
            self.assertGreater(artifacts[0].stat().st_size, 20_000)

    def test_bridge_preserves_pre_tool_aliases(self) -> None:
        data = bridge.adapt_input(
            {"tool_name": "Bash", "tool_input": {}, "result": {"output": "x"}},
            "PreToolUse",
        )
        self.assertEqual(data["tool_name"], "Bash")

    def test_bridge_event_merge_preserves_rlm_updated_input(self) -> None:
        merged = bridge._merge_event_outputs(
            [
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                        "updatedInput": {"file_path": "large.py", "limit": 150},
                        "additionalContext": "nav map",
                    }
                }
            ],
            "PreToolUse",
        )
        specific = merged["hookSpecificOutput"]
        self.assertEqual(specific["updatedInput"]["limit"], 150)
        self.assertEqual(specific["permissionDecision"], "allow")

    def test_mcp_filter_removes_image_data_and_clips_large_text(self) -> None:
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [{"type": "image", "data": "data:image/png;base64," + "A" * 20_000}],
                    "dom": "<html>" + "x" * 20_000,
                },
            }
        ).encode()
        filtered = json.loads(mcp_filter.sanitize_message(payload))
        content = filtered["result"]["content"][0]
        self.assertIn("image data omitted", content["data"])
        self.assertLess(len(filtered["result"]["dom"]), 7_000)

    def test_mcp_stdio_proxy_sanitizes_a_real_child_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child = root / "child.py"
            child.write_text(
                "import json, sys\n"
                "for line in sys.stdin:\n"
                "    json.loads(line)\n"
                "    print(json.dumps({'jsonrpc':'2.0','id':1,'result':{'image':'data:image/png;base64:' + 'B'*12000,'dom':'<body>' + 'y'*12000}}), flush=True)\n"
            )
            proxy = Path(__file__).parents[1] / "codex_continuous_mcp_filter.py"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(proxy),
                    "--command",
                    sys.executable,
                    "--args-json",
                    json.dumps([str(child)]),
                ],
                input='{"jsonrpc":"2.0","id":1,"method":"tools/call"}\n',
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertIn("image data omitted", result["result"]["image"])
            self.assertLess(len(result["result"]["dom"]), 7_000)

    def test_bridge_runs_rlm_pre_tool_hook_and_returns_updated_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hook = Path(__file__).parents[1] / "codex_continuous_rlm.py"
            result = bridge.run_hook(
                {
                    "cwd": str(root),
                    "session_id": "e2e-session",
                    "tool_name": "Bash",
                    "tool_input": {"command": "printf hello"},
                },
                {
                    "id": "codex_rlm_bash_pretool",
                    "event": "PreToolUse",
                    "matcher": "Bash",
                    "command": f"python3 {hook} --pretool-bash",
                    "timeout": 10,
                },
                root,
            )
            updated = result["hookSpecificOutput"]["updatedInput"]["command"]
            self.assertIn("--exec", updated)
            self.assertTrue("rlm_runner.py" in updated or "codex_continuous_rlm.py" in updated)


class RLMIntegrationTests(unittest.TestCase):
    def test_v47_resources_are_converted_without_replacing_v3_resources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            (source / ".claude").mkdir(parents=True)
            (source / ".claude" / "settings.json").write_text('{"hooks": {}}\n')
            deployment = root / "deployment"
            manifest = install.install_native_resources(
                source,
                deployment,
                root / "codex-home",
                enable_subagents=True,
                enable_dashboard=False,
            )
            self.assertEqual(manifest["rlm"]["version"], "ContinuousClaudeV4.7")
            self.assertGreaterEqual(len(manifest["rlm"]["skills"]["converted"]), 5)
            self.assertEqual(len(manifest["rlm"]["agents"]["agents"]), 2)
            skill_names = {item["name"] for item in manifest["rlm"]["skills"]["converted"]}
            self.assertIn("cc-codex-continuous-v47-research", skill_names)
            self.assertTrue((deployment / "codex-native" / "rlm-skills-manifest.json").is_file())
            converted_research = (root / "codex-home" / "skills" / "cc-codex-continuous-v47-research" / "SKILL.md").read_text()
            self.assertIn("ouros-harness", converted_research)
            self.assertNotIn("tools/ouros_harness.py", converted_research)
            agent_prompts = [Path(item["path"]).read_text() for item in manifest["rlm"]["agents"]["agents"]]
            self.assertTrue(all("tools/ouros_harness.py" not in prompt for prompt in agent_prompts))

    def test_rlm_hooks_are_added_to_existing_hook_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            (source / ".claude").mkdir(parents=True)
            (source / ".claude" / "settings.json").write_text('{"hooks": {}}\n')
            deployment = root / "deployment"
            (deployment / "rlm" / "claude" / "hooks").mkdir(parents=True)
            (deployment / "rlm" / "claude" / "hooks" / "tldr-read.mjs").write_text("// test\n")
            (deployment / "codex_continuous_rlm.py").write_text("# test\n")
            manifest, generated = install.build_hooks(source, deployment)
            self.assertEqual(manifest["hooks"]["codex_rlm_tldr_read"]["event"], "PreToolUse")
            self.assertEqual(manifest["hooks"]["codex_rlm_bash_pretool"]["event"], "PreToolUse")
            self.assertTrue(any("codex_rlm_bash_pretool" in json.dumps(hook) for hook in manifest["hooks"].values()))
            self.assertFalse(any("codex_rlm" in json.dumps(group) for group in generated["hooks"].get("PostToolUse", [])))

    def test_cua_like_stdio_mcp_server_is_wrapped_in_generated_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            (source / ".claude").mkdir(parents=True)
            (source / ".claude" / "settings.json").write_text('{"hooks": {}}\n')
            (source / ".claude" / "mcp_config.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "cua_repl": {
                                "command": "python3",
                                "args": ["/tmp/cua-server.py"],
                            }
                        }
                    }
                )
            )
            deployment = root / "deployment"
            manifest = install.install_native_resources(
                source, deployment, root / "codex-home", enable_subagents=False, enable_dashboard=False
            )
            server = manifest["mcp"]["servers"][0]
            self.assertTrue(server["rlm_filter"])
            catalog = (deployment / "codex-native" / "mcp-servers.toml").read_text()
            self.assertIn("codex_continuous_mcp_filter.py", catalog)
            self.assertIn("/tmp/cua-server.py", catalog)

    def test_large_read_hook_returns_nav_map_and_truncated_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_file = root / "large.py"
            source_file.write_text("def function_%04d():\n    return %r\n" % (1, "x" * 2000) * 4_000)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            fake_tldr = bin_dir / "tldr"
            fake_tldr.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' '{\"imports\": [], \"functions\": [{\"name\": \"main\", \"params\": [], \"line_number\": 1}]}'\n"
            )
            fake_tldr.chmod(0o755)
            payload = json.dumps(
                {"tool_name": "Read", "tool_input": {"file_path": str(source_file)}}
            )
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
            hook = Path(__file__).parents[1] / "rlm" / "claude" / "hooks" / "tldr-read.mjs"
            completed = subprocess.run(
                ["node", str(hook)],
                input=payload,
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            specific = result["hookSpecificOutput"]
            self.assertEqual(specific["permissionDecision"], "allow")
            self.assertEqual(specific["updatedInput"]["limit"], 150)
            self.assertIn("main()", specific["additionalContext"])

    def test_harness_writes_only_project_continuum_and_bounds_stdout(self) -> None:
        harness = load_harness()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = Path.cwd()
            os.chdir(root)
            try:
                allowed = harness._call_write_file("continuum/research/findings.md", "findings")
                denied = harness._call_write_file("outside.txt", "blocked")
                self.assertTrue(allowed["ok"])
                self.assertIn("denied", denied["error"])
                card = harness._write_off_context_output("x" * 7_000, storage_dir=root / "outputs")
                state = root / "state"
                state.mkdir()
                state_file = state / "session.bin"
                state_file.write_text("state")
                state.chmod(0o755)
                state_file.chmod(0o644)
                harness._harden_storage(state)
            finally:
                os.chdir(original)
            self.assertIn("persisted off-context", card)
            output_files = list((root / "outputs").glob("*.txt"))
            self.assertEqual(len(output_files), 1)
            self.assertEqual(stat.S_IMODE(output_files[0].stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(state_file.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
