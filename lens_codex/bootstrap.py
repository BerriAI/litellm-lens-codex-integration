"""Standalone installer: also downloaded directly by install.sh."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys


def engine() -> str:
    relative = "ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
    bundled = next((path for base in (Path("/Applications"), Path.home() / "Applications")
                    if (path := base / relative).is_file()), None)
    result = str(bundled) if bundled else shutil.which("codex")
    if not result:
        raise ValueError("Install Codex and sign in, then run setup again.")
    return result


def main() -> None:
    if sys.platform != "darwin":
        raise SystemExit("Automatic setup currently supports macOS.")
    try:
        binary = engine()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    print("Installing LiteLLM Lens in Codex…", flush=True)
    try:
        subprocess.run([binary, "plugin", "marketplace", "add",
                        "BerriAI/litellm-lens-codex-integration", "--json"],
                       capture_output=True, check=True, timeout=120)
        subprocess.run([binary, "plugin", "marketplace", "upgrade", "berriai-lens"],
                       capture_output=True, check=True, timeout=120)
        result = subprocess.run([binary, "plugin", "add", "litellm-lens@berriai-lens", "--json"],
                                capture_output=True, text=True, check=True, timeout=120)
        installed = json.loads(result.stdout)
        root = Path(installed["installedPath"])
        if not root.is_absolute():
            raise ValueError("Invalid plugin path")
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        raise SystemExit("Codex could not install the plugin. Update Codex, check your connection, and try again.") from None
    launcher = root / "scripts/plugin_entry.py"
    if not launcher.is_file():
        raise SystemExit("The installed plugin is incomplete. Update its marketplace and try again.")
    print("Opening Lens setup…", flush=True)
    # Use the interpreter already checked by install.sh, including when PATH has an older Python.
    try:
        subprocess.run([sys.executable, str(launcher), "setup"], check=True)
    except (OSError, subprocess.SubprocessError):
        raise SystemExit("The plugin is installed, but setup did not finish. Run the install command again to retry.") from None


if __name__ == "__main__":
    main()
