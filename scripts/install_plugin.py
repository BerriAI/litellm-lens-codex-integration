"""Install the public plugin through Codex, then open its local setup page."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lens_codex.bootstrap import main


if __name__ == "__main__":
    main()
