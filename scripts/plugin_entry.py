"""Entry point usable from a cached plugin or any working directory."""
import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
os.environ["LENS_CODEX_PLUGIN_ROOT"] = str(root)
from lens_codex.cli import main

main()
