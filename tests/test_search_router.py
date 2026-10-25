from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_continuous_search_router as router
from codex_continuous_semantic_runtime import semantic_environment, semantic_project_guard


class SearchRouterTests(unittest.TestCase):
    def test_classification_keeps_identifiers_and_paths_literal(self) -> None:
        self.assertEqual(router.classify_query("login_token"), "literal")
        self.assertEqual(router.classify_query("src/auth.py"), "literal")
        self.assertEqual(router.classify_query("where is the authentication flow implemented?"), "semantic")

    def test_literal_grep_is_passed_through(self) -> None:
        result = router.route({"tool_name": "Grep", "tool_input": {"pattern": "login_token"}})
        self.assertEqual(result, {})

    def test_semantic_query_uses_optional_binary_and_blocks_with_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "tldr-semantic"
            binary.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "print(json.dumps({'results': [{'file_path': 'src/auth.py', 'function_name': 'login', 'score': 0.8, 'line_start': 4}]}))\n",
                encoding="utf-8",
            )
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            with patch.dict(os.environ, {"CODEX_CONTINUOUS_TLDR_SEMANTIC": str(binary), "CLAUDE_PROJECT_DIR": str(root)}), patch.object(
                router,
                "persistent_semantic_search",
                return_value={
                    "results": [
                        {
                            "file": "src/auth.py",
                            "function": "login",
                            "score": 0.8,
                            "line": 4,
                        }
                    ]
                },
            ):
                result = router.route(
                    {
                        "tool_name": "Grep",
                        "session_id": "test-session",
                        "tool_input": {"pattern": "where is authentication implemented?"},
                    }
                )
            specific = result["hookSpecificOutput"]
            self.assertEqual(specific["permissionDecision"], "deny")
            self.assertIn("src/auth.py:4", specific["permissionDecisionReason"])

    def test_semantic_model_defaults_to_small_model_but_is_overridable(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODEX_CONTINUOUS_TLDR_MODEL", None)
            self.assertEqual(router.DEFAULT_SEMANTIC_MODEL, "all-MiniLM-L6-v2")
        with patch.dict(os.environ, {"CODEX_CONTINUOUS_TLDR_MODEL": "bge-large-en-v1.5"}):
            self.assertEqual(os.environ["CODEX_CONTINUOUS_TLDR_MODEL"], "bge-large-en-v1.5")

    def test_semantic_child_reuses_original_user_cache_when_bridge_isolated(self) -> None:
        with patch.dict(
            os.environ,
            {
                "HOME": "/deployment",
                "CODEX_CONTINUOUS_ORIGINAL_HOME": "/user",
            },
            clear=False,
        ):
            environment = semantic_environment()
        self.assertEqual(environment["HOME"], "/user")
        self.assertEqual(environment["FASTEMBED_CACHE_DIR"], "/user/.cache/tldr/fastembed")
        self.assertEqual(environment["HF_HOME"], "/user/.cache/tldr/fastembed")

    def test_large_project_is_rejected_before_starting_semantic_child(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index in range(3):
                (root / f"module_{index}.py").write_text("x = 1\n", encoding="utf-8")
            with patch.dict(os.environ, {"CODEX_CONTINUOUS_TLDR_MAX_FILES": "2"}):
                guard = semantic_project_guard(root)
        self.assertFalse(guard["allowed"])
        self.assertIn("safety budget", guard["reason"])

    def test_semantic_environment_limits_embedding_threads_without_virtual_memory_cap(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            environment = semantic_environment()
        self.assertEqual(environment["OMP_NUM_THREADS"], "2")
        self.assertEqual(environment["MKL_NUM_THREADS"], "2")
        self.assertEqual(environment["TOKENIZERS_PARALLELISM"], "false")


if __name__ == "__main__":
    unittest.main()
