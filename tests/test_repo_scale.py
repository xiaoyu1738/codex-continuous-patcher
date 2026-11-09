from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_continuous_repo_scale import measure_repository


class RepositoryScaleTests(unittest.TestCase):
    def test_file_count_threshold_short_circuits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index in range(100):
                (root / f"module_{index:03}.py").write_text("x = 1\n", encoding="utf-8")
            result = measure_repository(root)
        self.assertTrue(result["eligible"])
        self.assertEqual(result["files"], 80)
        self.assertEqual(result["visited"], 80)

    def test_byte_threshold_short_circuits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "large.py").write_bytes(b"x" * (512 * 1024))
            result = measure_repository(root)
        self.assertTrue(result["eligible"])
        self.assertEqual(result["files"], 1)
        self.assertEqual(result["visited"], 1)

    def test_small_repository_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "small.py").write_text("x = 1\n", encoding="utf-8")
            result = measure_repository(root)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["files"], 1)


if __name__ == "__main__":
    unittest.main()
