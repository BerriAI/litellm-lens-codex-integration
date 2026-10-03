from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import webbrowser


def main() -> None:
    parser = argparse.ArgumentParser(description="Send Codex sessions to LiteLLM Lens.")
    parser.add_argument("--state", help="Private local data directory")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("setup", "capture", "status", "pause", "resume", "flush", "uninstall"):
        sub.add_parser(name)
    serving = sub.add_parser("serve")
    serving.add_argument("--port", type=int, default=18734)
    args = parser.parse_args()
    if args.state:
        os.environ["LENS_CODEX_HOME"] = args.state
    os.umask(0o077)
    from .state import MAX_EVENT_BYTES, capture, config, health, save_config, set_enabled, state_dir
    try:
        if args.command == "capture":
            try:
                raw = sys.stdin.buffer.read(MAX_EVENT_BYTES + 1)
                if len(raw) > MAX_EVENT_BYTES:
                    raise ValueError("A Codex hook exceeded 16 MB. Capture was skipped without truncation.")
                capture(json.loads(raw))
            except Exception as exc:
                try:
                    health("capture_error", str(exc) if isinstance(exc, ValueError) else
                           "Could not capture a Codex event. Check local disk space and permissions.")
                except Exception:
                    pass  # Even a full disk must not break the user's Codex turn.
            # Observation only: never block tools or inject context, even when capture fails.
            print("{}")
        elif args.command == "serve":
            from .web import serve
            serve(args.port)
        elif args.command == "setup":
            from .install import install_service
            install_service()
            path = state_dir() / "service.json"
            for _ in range(50):
                if path.exists():
                    break
                time.sleep(0.1)
            port = json.loads(path.read_text())["port"] if path.exists() else 18734
            webbrowser.open(f"http://127.0.0.1:{port}/")
            print("Lens Codex is open in your browser. You can close this terminal.")
        elif args.command == "status":
            from .delivery import status
            print(json.dumps(status(), indent=2))
        elif args.command in {"pause", "resume"}:
            settings = config()
            if not settings.get("gateway"):
                raise ValueError("Run setup first.")
            set_enabled(args.command == "resume")
            print("Recording paused. Nothing will be captured or sent." if args.command == "pause" else
                  "Recording resumed. Saved turns will be delivered; new turns will be captured.")
        elif args.command == "flush":
            from .delivery import flush
            flush()
        elif args.command == "uninstall":
            from .install import uninstall
            uninstall()
            print("Lens Codex removed. Other hooks are unchanged. Local queued data is retained for your review.")
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
