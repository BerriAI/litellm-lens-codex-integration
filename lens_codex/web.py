"""Loopback-only setup and health window, without third-party assets."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import threading
import time
import uuid

from .delivery import flush, retry_blocked, status, verify
from .install import copy_app, install_hooks
from .state import atomic_json, config, gateway_url, save_config, set_enabled, state_dir


def configure(value: dict, check: bool = True) -> None:
    old = config()
    gateway = gateway_url(str(value.get("gateway", "")))
    key = str(value.get("api_key", "")).strip() or old.get("api_key", "")
    name = str(value.get("agent_name", "codex")).strip()
    if not key or "\n" in key or "\r" in key:
        raise ValueError("Enter a LiteLLM virtual key with access to traces.")
    if not name or len(name) > 100:
        raise ValueError("Give this agent a name (up to 100 characters).")
    if old.get("gateway") and old["gateway"] != gateway:
        counts = status()["counts"]
        if any(v for k, v in counts.items() if k not in {"sent", "discarded"}):
            raise ValueError("There are saved turns for the current gateway. Resolve them before changing gateways.")
    settings = {**old, "gateway": gateway, "api_key": key, "agent_name": name,
                "enabled": True, "installation_id": old.get("installation_id", str(uuid.uuid4())),
                "codex_home": old.get("codex_home", str(Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()))}
    if check:
        verify(settings)
    copy_app()
    install_hooks(Path(settings["codex_home"]))
    from .codex import approve_installed_hooks
    approve_installed_hooks()
    save_config(settings)
    retry_blocked()


def serve(port: int = 18734) -> None:
    token = secrets.token_urlsafe(32)
    page = Path(__file__).with_name("setup.html").read_text()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, code, body, content_type="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body.encode())

        def local(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def do_GET(self):
            if not self.local():
                self.reply(403, "{}")
            elif self.path == "/":
                self.reply(200, page.replace("__CSRF__", token), "text/html")
            elif self.path == "/status":
                self.reply(200, json.dumps(status()))
            else:
                self.reply(404, "{}")

        def do_POST(self):
            expected_origin = f"http://127.0.0.1:{self.server.server_port}"
            if (not self.local() or self.headers.get("X-Lens-Token") != token
                    or self.headers.get("Origin", expected_origin) != expected_origin):
                self.reply(403, json.dumps({"error": "Refresh the setup window and try again."}))
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 <= size <= 16384:
                    raise ValueError("Request is too large.")
                value = json.loads(self.rfile.read(size))
                if self.path == "/configure":
                    configure(value)
                elif self.path in {"/pause", "/resume"}:
                    settings = config()
                    if not settings.get("gateway"):
                        raise ValueError("Connect a gateway first.")
                    set_enabled(self.path == "/resume")
                elif self.path == "/retry":
                    retry_blocked()
                else:
                    self.reply(404, "{}")
                    return
                self.reply(200, json.dumps(status()))
            except (ValueError, OSError, KeyError) as exc:
                # Only expected local validation errors reach here; delivery never includes remote bodies.
                self.reply(400, json.dumps({"error": str(exc) if isinstance(exc, ValueError) else
                                           "Could not save setup. Check local file permissions."}))

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    atomic_json(state_dir() / "service.json", {"port": server.server_port, "pid": os.getpid()})
    stopping = threading.Event()

    def deliver():
        while not stopping.wait(1):
            try:
                flush()
            except Exception:
                from .state import health
                health("helper_error", "The helper encountered an error. Restart it and check status before continuing.")

    worker = threading.Thread(target=deliver, daemon=True)
    worker.start()
    try:
        server.serve_forever()
    finally:
        stopping.set()
        server.server_close()
