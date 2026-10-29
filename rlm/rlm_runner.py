#!/usr/bin/env python3
"""Installed-path entry point for the Codex pre-execution RLM runner."""

from __future__ import annotations

import runpy
from pathlib import Path


if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "codex_continuous_rlm.py"), run_name="__main__")
