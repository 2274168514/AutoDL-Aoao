"""Windowed entry point for source runs and the native PyInstaller bundle."""

from __future__ import annotations

import sys
from pathlib import Path

# A source checkout can run this launcher without first installing the package.
# Frozen builds resolve the package from their bundled import archive instead.
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from autodl_gpu_watch.gui import main


if __name__ == "__main__":
    raise SystemExit(main())
