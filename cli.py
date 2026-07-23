#!/usr/bin/env python3
"""Repo-root entry point: delegates to the packaged Typer app.

Usage without installing:  python cli.py extract --config config.yaml
After `pip install -e .`:  lineage extract --config config.yaml
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from lineage.cli import app  # noqa: E402

if __name__ == "__main__":
    app()
