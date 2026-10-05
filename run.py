"""Run directly from a source checkout without installing a package."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from autodl_gpu_watch.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
