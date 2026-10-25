from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import codex_continuous_bridge as bridge


class BridgeUnitTests(unittest.TestCase):
    def test_tool_aliases_and_input_fields_are_normalized(self) -> None:
        payload = bridge.adapt_input(
            {
                "session_id": "s1",
                "cwd": "/tmp",
                "tool_name": "apply_patch",
                "tool_input": {"path": "demo.py", "new_content": "x"},
            },
            "PreToolUse",
        )
        self.assertEqual(payload["tool_name"], "Edit")
        self.assertEqual(payload["tool_input"]["file_path"], "demo.py")
        self.assertEqual(payload["tool_input"]["content"], "x")

    def test_apply_patch_payload_exposes_changed_paths(self) -> None:
        payload = bridge.adapt_input(
            {
                "tool_name": "apply_patch",
                "tool_input": {
                    "command": "*** Begin Patch\n*** Update File: src/app.py\n@@\n-old\n+new\n*** End Patch"
                },
            },
            "PreToolUse",
        )
        self.assertEqual(payload["tool_input"]["file_path"], "src/app.py")
        self.assertEqual(payload["tool_input"]["file_paths"], ["src/app.py"])

        native_payload = bridge.adapt_input(
            {
                "tool_name": "apply_patch",
                "tool_input": {
                    "patch": "*** Begin Patch\n*** Update File: lib/core.py\n*** End Patch"
                },
            },
            "PostToolUse",
        )
        self.assertEqual(native_payload["tool_input"]["file_path"], "lib/core.py")

    def test_matcher_uses_normalized_tool_name(self) -> None:
        payload = bridge.adapt_input({"tool_name": "shell"}, "PreToolUse")
        self.assertTrue(bridge.matches_source_matcher("Bash|Edit", payload, "PreToolUse"))
        self.assertFalse(bridge.matches_source_matcher("Read", payload, "PreToolUse"))

    def test_smart_search_router_is_rewritten_to_codex_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = root / "codex_continuous_search_router.py"
            adapter.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            rewritten = bridge.rewrite_command(
                "node $HOME/.claude/hooks/dist/smart-search-router.mjs", root
            )
            self.assertEqual(rewritten, f"python3 {adapter}")

    def test_legacy_and_plain_text_outputs_are_codex_outputs(self) -> None:
        blocked = bridge.normalize_output(
            json.dumps({"result": "block", "reason": "use the index"}), "PreToolUse"
        )
        self.assertEqual(blocked["decision"], "block")
        self.assertEqual(blocked["reason"], "use the index")

        context = bridge.normalize_output("continuity loaded", "SessionStart")
        self.assertEqual(
            context["hookSpecificOutput"]["hookEventName"], "SessionStart"
        )
        self.assertIn("continuity loaded", context["hookSpecificOutput"]["additionalContext"])

    def test_bridge_executes_manifest_command_and_applies_matcher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "hooks-manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "deployment_root": str(root),
                        "hooks": {
                            "demo": {
                                "event": "UserPromptSubmit",
                                "matcher": None,
                                "command": "python3 -c 'print(\"hello from hook\")'",
                                "timeout": 5,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            result = bridge.run_hook(
                {"session_id": "s1", "cwd": str(root), "prompt": "hello"},
                {"id": "demo", "event": "UserPromptSubmit", "command": "python3 -c 'print(\"hello from hook\")'", "timeout": 5},
                root,
            )
            self.assertIn("hello from hook", result["hookSpecificOutput"]["additionalContext"])

    def test_event_dispatcher_runs_matching_hooks_in_one_bridge_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "hooks-manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "deployment_root": str(root),
                        "hooks": {
                            "one": {
                                "event": "UserPromptSubmit",
                                "matcher": None,
                                "command": "python3 -c 'print(\"one\")'",
                                "timeout": 5,
                            },
                            "two": {
                                "event": "UserPromptSubmit",
                                "matcher": None,
                                "command": "python3 -c 'print(\"two\")'",
                                "timeout": 5,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            result = bridge.run_event(
                {"session_id": "s1", "cwd": str(root), "prompt": "hello"},
                "UserPromptSubmit",
                manifest,
                root,
            )
            context = result["hookSpecificOutput"]["additionalContext"]
            self.assertIn("one", context)
            self.assertIn("two", context)

    def test_pretooluse_blocks_codex_session_history_before_hook_execution(self) -> None:
        output = bridge.run_hook(
            {
                "tool_name": "shell",
                "tool_input": {"command": "cat ~/.codex/sessions/trace.jsonl"},
            },
            {"id": "guard", "event": "PreToolUse", "matcher": None, "command": "false"},
            Path("/tmp"),
        )
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_codex_compaction_writes_yaml_handoff_without_claude_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "rollout.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "role": "assistant",
                        "content": "Implement the feature and run tests.",
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "function_call",
                        "name": "shell",
                        "arguments": {"command": "pytest"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            handoff = bridge._codex_compact_handoff(
                {
                    "cwd": str(root),
                    "session_id": "session-1234",
                    "trigger": "auto",
                    "transcript_path": str(transcript),
                }
            )
            self.assertIsNotNone(handoff)
            assert handoff is not None
            content = handoff.read_text(encoding="utf-8")
            self.assertIn("type: auto-handoff", content)
            self.assertIn("shell", content)


if __name__ == "__main__":
    unittest.main()
