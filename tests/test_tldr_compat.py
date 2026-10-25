from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_continuous_tldr as cli_adapter
import codex_continuous_tldr_daemon as daemon_adapter
import install


class TldrCompatibilityTests(unittest.TestCase):
    def test_legacy_flow_commands_map_to_upstream_daemon_routes(self) -> None:
        route, payload, fallback = daemon_adapter._request_spec(
            {"cmd": "cfg", "file": "src/main.py", "function": "run", "language": "python"}
        )
        self.assertEqual(route, "/cfg")
        self.assertEqual(payload["function"], "run")
        self.assertFalse(fallback)

        route, payload, fallback = daemon_adapter._request_spec(
            {"cmd": "arch", "language": "rust"}
        )
        self.assertEqual(route, "/arch")
        self.assertEqual(payload, {"language": "rust"})
        self.assertFalse(fallback)

    def test_missing_semantic_feature_falls_back_explicitly_to_lexical_search(self) -> None:
        route, payload, fallback = daemon_adapter._request_spec(
            {"cmd": "semantic", "action": "search", "query": "auth flow", "k": 5}
        )
        self.assertEqual(route, "/search")
        self.assertEqual(payload, {"pattern": "auth flow", "max_results": 5})
        self.assertTrue(fallback)
        route, payload, fallback = daemon_adapter._request_spec(
            {"cmd": "semantic", "query": "auth flow", "k": 5}
        )
        self.assertEqual(route, "/search")
        self.assertEqual(payload, {"pattern": "auth flow", "max_results": 5})
        self.assertTrue(fallback)
        self.assertIsNone(
            daemon_adapter._request_spec({"cmd": "semantic", "action": "index"})
        )

    def test_project_and_language_flags_are_removed_without_shell_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, args = cli_adapter._project(["--project", str(root), "src/main.py", "run"])
            language, args = cli_adapter._language(["--lang", "rust", *args])
            self.assertEqual(project, root.resolve())
            self.assertEqual(args, ["src/main.py", "run"])
            self.assertEqual(language, "rust")

    def test_semantic_index_uses_upstream_faiss_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text("def auth():\n    return True\n", encoding="utf-8")
            with patch(
                "codex_continuous_tldr.build_index",
                return_value={"status": "ok", "index_path": str(root / ".tldr/cache/semantic/index.faiss")},
            ) as build:
                code = cli_adapter._semantic(["index", str(root)])
            self.assertEqual(code, 0)
            build.assert_called_once()

    def test_installer_targets_original_python_package(self) -> None:
        self.assertEqual(install.LLM_TLDR_PACKAGE, "llm-tldr")
        self.assertTrue(install.LLM_TLDR_VERSION)


if __name__ == "__main__":
    unittest.main()
