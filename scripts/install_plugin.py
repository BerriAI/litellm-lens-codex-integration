"""Install the public plugin through Codex, then open its local setup page."""
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lens_codex.codex import engine


def main():
    binary = engine()
    print("Installing LiteLLM Lens in Codex…", flush=True)
    try:
        subprocess.run([binary, "plugin", "marketplace", "add",
                        "BerriAI/litellm-lens-codex-integration", "--json"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=120)
        result = subprocess.run([binary, "plugin", "add", "litellm-lens@berriai-lens", "--json"],
                                capture_output=True, text=True, check=True, timeout=120)
        root = Path(json.loads(result.stdout)["installedPath"])
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError, KeyError):
        raise SystemExit("Codex could not install the plugin. Update Codex, check your connection, and try again.") from None
    launcher = root / "scripts/run.sh"
    if not launcher.is_file():
        raise SystemExit("The installed plugin is incomplete. Update its marketplace and try again.")
    subprocess.run(["bash", str(launcher), "setup"], check=True)


if __name__ == "__main__":
    main()
