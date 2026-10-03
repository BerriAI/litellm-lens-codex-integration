"""Use Codex's own config API to approve only the hooks explicitly installed by setup."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading

from .state import state_dir


def engine() -> str:
    bundled = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex")
    result = str(bundled) if bundled.exists() else shutil.which("codex")
    if not result:
        raise ValueError("Install Codex and sign in, then open Setup again.")
    return result


class Client:
    def __init__(self, binary: str, home: Path):
        self.process = subprocess.Popen([binary, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, env={**os.environ, "CODEX_HOME": str(home)})
        self.messages = queue.Queue()
        self.counter = 0
        self.notifications = []
        def receive():
            for line in self.process.stdout:
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    continue
            self.messages.put(None)
        threading.Thread(target=receive, daemon=True).start()

    def __enter__(self):
        try:
            self.call("initialize", {"clientInfo": {"name": "lens_codex_setup", "version": "0.1.0"},
                                     "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized", "params": {}})
            return self
        except BaseException:
            self.__exit__()
            raise

    def __exit__(self, *_):
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()

    def send(self, message: dict):
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def read(self, timeout=30):
        try:
            message = self.messages.get(timeout=timeout)
        except queue.Empty:
            raise ValueError("Codex did not respond. Update Codex, then try setup again.") from None
        if message is None:
            raise ValueError("Codex closed during setup. Update Codex, then try again.")
        return message

    def call(self, method: str, params: dict):
        self.counter += 1
        identity = self.counter
        self.send({"id": identity, "method": method, "params": params})
        while True:
            message = self.read()
            if message.get("id") != identity:
                self.notifications.append(message)
                continue
            if "error" in message:
                raise ValueError("This Codex version could not configure Lens hooks. Update Codex and try again.")
            return message.get("result")


def own_hooks(result: dict, command: str, home: Path) -> list[dict]:
    path = (home / "hooks.json").resolve()
    unique = {}
    for entry in result.get("data", []):
        for hook in entry.get("hooks", []):
            if (hook.get("handlerType") == "command" and hook.get("command") == command
                    and Path(hook.get("sourcePath", "")).resolve() == path and hook.get("source") == "user"):
                unique[hook["key"]] = hook
    return list(unique.values())


def approve_installed_hooks(binary: str | None = None) -> None:
    """Called only by explicit setup. Never approve project/plugin/unrelated user hooks."""
    manifest = json.loads((state_dir() / "installation.json").read_text())
    home = Path(manifest["codex_home"])
    with Client(binary or engine(), home) as client:
        hooks = own_hooks(client.call("hooks/list", {"cwds": [str(home)]}), manifest["command"], home)
        if len(hooks) != 7:
            raise ValueError("Codex could not find all seven Lens hooks. Update Codex, then try setup again.")
        edits = [{"keyPath": "hooks.state." + json.dumps(hook["key"]),
                  "value": {"enabled": True, "trusted_hash": hook["currentHash"]}, "mergeStrategy": "upsert"}
                 for hook in hooks]
        client.call("config/batchWrite", {"edits": edits, "filePath": str(home / "config.toml")})
