"""Terminal-first setup, with an optional browser control page."""
from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
import time
import warnings
import webbrowser

from .install import install_plugin_hooks, install_service
from .state import config, gateway_url, save_config, set_enabled, state_dir
from .web import configure


def refresh_plugin() -> None:
    settings = config()
    root = os.environ.get("LENS_CODEX_PLUGIN_ROOT")
    if root and settings.get("gateway"):
        install_plugin_hooks(Path(settings["codex_home"]), Path(root))
        save_config({**settings, "capture_mode": "plugin"})


def start_helper() -> int:
    install_service()
    path = state_dir() / "service.json"
    for _ in range(300):
        if path.exists():
            return json.loads(path.read_text())["port"]
        time.sleep(0.1)
    raise ValueError("The local helper did not start. Check service.log in the Lens data directory.")


def ask(reader, writer, prompt: str) -> str:
    writer.write(prompt)
    writer.flush()
    value = reader.readline()
    if not value:
        raise EOFError
    return value.strip()


def connect(reader, writer) -> bool:
    writer.write("\nConnect Codex to Lens\n\n")
    while True:
        try:
            gateway = gateway_url(ask(reader, writer, "Gateway URL: "))
            # Never fall back to displaying a key if the terminal cannot hide input.
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                key = getpass.getpass("LiteLLM virtual key (hidden): ", stream=writer).strip()
            name = ask(reader, writer, "Agent name [codex]: ") or "codex"
            writer.write(f"\nNew prompts, replies, and tool output will be sent to {gateway}.\n"
                         "Past chats and hidden reasoning are not uploaded.\n")
            if ask(reader, writer, "Connect and start recording? [y/N]: ").lower() not in {"y", "yes"}:
                writer.write("Setup cancelled. Recording has not been enabled.\n")
                return False
            # Start the helper before enabling capture, so failed setup stays paused.
            configure({"gateway": gateway, "api_key": key, "agent_name": name}, enabled=False)
            return True
        except getpass.GetPassWarning:
            raise ValueError("This terminal cannot hide the key. Open a normal Terminal window and run setup again.") from None
        except ValueError as exc:
            writer.write(f"\n{exc}\n")
            if ask(reader, writer, "Try again? [Y/n]: ").lower() not in {"", "y", "yes"}:
                return False


def terminal_setup() -> None:
    settings = config()
    configured = bool(settings.get("gateway") and settings.get("api_key"))
    if configured:
        refresh_plugin()
    else:
        # curl | bash consumes stdin. Read human input from the controlling terminal instead.
        try:
            reader = open("/dev/tty", "r")
        except OSError:
            raise ValueError("Run setup in your own Terminal window to enter the key securely, "
                             "or use setup --browser.") from None
        with reader, open("/dev/tty", "w", buffering=1) as writer:
            if not connect(reader, writer):
                return
    port = start_helper()
    if not configured:
        set_enabled(True)
    if config().get("enabled"):
        print("\nLens is ready. Start a new Codex chat, then open Lens > Traces on your gateway.")
    else:
        print("\nLens is updated. Recording is still paused.")
    print(f"Settings and recording controls: http://127.0.0.1:{port}/")


def browser_setup() -> None:
    refresh_plugin()
    port = start_helper()
    webbrowser.open(f"http://127.0.0.1:{port}/")
    print("Lens settings are open in your browser. You can close this terminal.")
