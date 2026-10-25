from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_continuous_shell_router as router
import codex_continuous_change_tracker as tracker


class ShellRouterTests(unittest.TestCase):
    def test_sessions_history_is_blocked(self) -> None:
        output = router.route(
            {
                "tool_name": "shell",
                "cwd": "/tmp",
                "tool_input": {"command": "cat ~/.codex/sessions/2026/trace.jsonl"},
            }
        )
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("sessions", output["hookSpecificOutput"]["permissionDecisionReason"])

    def test_unrelated_shell_command_passes(self) -> None:
        self.assertEqual(
            router.route({"tool_name": "shell", "tool_input": {"command": "git status"}}),
            {},
        )

    def test_source_read_is_intercepted_when_tldr_returns_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "app.py"
            source.write_text("def auth():\n    return True\n", encoding="utf-8")
            with patch.object(router, "_tldr_extract", return_value={"status": "ok", "result": {"functions": ["auth"]}}):
                output = router.route(
                    {
                        "tool_name": "exec_command",
                        "cwd": str(root),
                        "tool_input": {"command": f"cat {source}"},
                    }
                )
            self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertIn("auth", output["hookSpecificOutput"]["permissionDecisionReason"])

    def test_absolute_command_paths_find_external_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo-b"
            repo.mkdir()
            (repo / ".git").mkdir()
            source = repo / "auth.py"
            source.write_text("def auth():\n    return False\n", encoding="utf-8")
            with patch.object(tracker, "_git_root", return_value=repo):
                roots = tracker.candidate_roots(
                    {
                        "cwd": str(root),
                        "tool_name": "shell",
                        "tool_input": {"command": f"python3 {source} --rewrite"},
                    }
                )
            self.assertEqual(roots, [repo])

    def test_post_tool_notifies_external_indexed_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo-b"
            repo.mkdir()
            (repo / ".git").mkdir()
            source = repo / "auth.py"
            source.write_text("def auth():\n    return False\n", encoding="utf-8")
            deployment = root / "deployment"
            deployment.mkdir()
            state = {
                "created_at": __import__("time").time(),
                "roots": {str(repo): {"status": [], "hashes": {}, "explicit": []}},
            }
            with patch.object(tracker, "_load_pending", return_value=state), patch.object(
                tracker, "_notify", return_value=1
            ) as notify, patch.object(tracker, "candidate_roots", return_value=[repo]), patch.object(
                tracker, "_command_paths", return_value=[source]
            ), patch.object(tracker, "_snapshot", return_value={"status": ["auth.py"], "hashes": {}, "explicit": []}):
                result = tracker.process(
                    {
                        "hook_event_name": "PostToolUse",
                        "cwd": str(root),
                        "tool_name": "shell",
                        "tool_input": {"command": f"python3 {source}"},
                    },
                    "PostToolUse",
                    deployment,
                )
            self.assertEqual(result["notified"], 1)
            notify.assert_called_once()
