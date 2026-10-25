from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_continuous_semantic_warm as warm


class SemanticWarmTests(unittest.TestCase):
    def test_non_git_session_is_skipped_before_guard_or_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(warm, "_git_root", return_value=None), patch.object(
                warm, "semantic_project_guard"
            ) as guard:
                result = warm.start({"source": "startup", "cwd": temporary})
        self.assertEqual(result["status"], "skipped")
        guard.assert_not_called()

    def test_existing_upstream_artifacts_are_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            semantic = root / ".tldr" / "cache" / "semantic"
            semantic.mkdir(parents=True)
            (semantic / "index.faiss").write_bytes(b"index")
            (semantic / "metadata.json").write_text("{}", encoding="utf-8")
            with patch.object(warm, "_git_root", return_value=root), patch.object(
                warm, "semantic_project_guard"
            ) as guard, patch.object(warm, "_spawn_worker") as spawn, patch.object(
                warm, "_spawn_structural_worker", return_value=False
            ):
                result = warm.start({"source": "resume", "cwd": str(root)})
        self.assertEqual(result["status"], "ready")
        guard.assert_not_called()
        spawn.assert_not_called()

    def test_small_git_project_spawns_detached_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(warm, "_git_root", return_value=root), patch.object(
                warm,
                "semantic_project_guard",
                return_value={"allowed": True, "files": 2, "bytes": 20},
            ), patch.object(warm, "_spawn_worker", return_value=True) as spawn, patch.object(
                warm, "_spawn_structural_worker", return_value=False
            ):
                result = warm.start({"source": "startup", "cwd": str(root)})
        self.assertEqual(result["status"], "started")
        spawn.assert_called_once()

    def test_existing_warm_lock_prevents_second_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(warm, "_git_root", return_value=root), patch.object(
                warm,
                "semantic_project_guard",
                return_value={"allowed": True, "files": 2, "bytes": 20},
            ), patch.object(warm, "_spawn_worker") as spawn, patch.object(
                warm, "_spawn_structural_worker", return_value=False
            ):
                with warm._exclusive_file_lock(root, warm.WARM_LOCK_NAME):
                    result = warm.start({"source": "startup", "cwd": str(root)})
        self.assertEqual(result["status"], "busy")
        spawn.assert_not_called()

    def test_session_hook_does_not_warm_on_compact(self) -> None:
        self.assertFalse(warm._source_allows_warm("compact"))


if __name__ == "__main__":
    unittest.main()
