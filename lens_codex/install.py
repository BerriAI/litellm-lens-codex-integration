"""Install only our own hooks; leave existing hooks and telemetry untouched."""
from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib

from .state import EVENTS, atomic_json, state_dir

LABEL = "ai.berri.lens-codex"


def enable_hooks(source: str) -> str:
    parsed = tomllib.loads(source)
    if parsed.get("features", {}).get("hooks") is True:
        return source
    lines = source.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if re.match(r'^\s*\[features\]\s*(?:#.*)?$', line.rstrip())), None)
    if start is not None:
        end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
        field = next((i for i in range(start + 1, end) if re.match(r'^\s*hooks\s*=', lines[i])), None)
        if field is not None:
            lines[field] = "hooks = true # Enabled by Lens Codex\n"
        else:
            lines.insert(start + 1, "hooks = true # Enabled by Lens Codex\n")
        result = "".join(lines)
    else:
        # An inline features table requires a TOML-aware editor; do not risk changing unrelated settings.
        if "features" in parsed:
            raise ValueError("Your config uses an inline features table. Set features.hooks = true there, then retry.")
        result = source.rstrip() + "\n\n[features]\nhooks = true # Enabled by Lens Codex\n"
    tomllib.loads(result)
    return result


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".lens-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def remove_user_hooks(manifest: dict) -> None:
    """Remove only the exact legacy command recorded by our own installer."""
    if manifest.get("mode") == "plugin" or not manifest.get("command"):
        return
    path = Path(manifest["codex_home"]) / "hooks.json"
    if not path.exists():
        return
    hooks = json.loads(path.read_text())
    for event, groups in list(hooks.get("hooks", {}).items()):
        for group in groups:
            group["hooks"] = [h for h in group.get("hooks", []) if h.get("command") != manifest["command"]]
        hooks["hooks"][event] = [g for g in groups if g.get("hooks")]
        if not hooks["hooks"][event]:
            del hooks["hooks"][event]
    atomic_json(path, hooks)


def install_plugin_hooks(codex_home: Path, root: Path, binary: str | None = None) -> None:
    """Enable and trust only hooks discovered from this installed plugin."""
    from .codex import approve_installed_hooks

    codex_home, root = codex_home.expanduser().resolve(), root.resolve()
    manifest_path = state_dir() / "installation.json"
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    config_path = codex_home / "config.toml"
    source = config_path.read_text() if config_path.exists() else ""
    updated = enable_hooks(source)
    backup = state_dir() / "backup"
    backup.mkdir(exist_ok=True, mode=0o700)
    if config_path.exists() and not (backup / "config.toml").exists():
        shutil.copy2(config_path, backup / "config.toml")
        (backup / "config.toml").chmod(0o600)
    write_text(config_path, updated)
    manifest = {"mode": "plugin", "plugin_root": str(root), "codex_home": str(codex_home),
                "command": f'bash "{root}/scripts/run.sh" capture'}
    # Validate against actual Codex discovery before replacing any legacy hooks.
    approve_installed_hooks(binary=binary, manifest=manifest)
    remove_user_hooks(previous)
    atomic_json(manifest_path, manifest)


def install_hooks(codex_home: Path) -> None:
    codex_home = codex_home.expanduser().resolve()
    codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = state_dir()
    hooks_path, config_path = codex_home / "hooks.json", codex_home / "config.toml"
    hooks = json.loads(hooks_path.read_text()) if hooks_path.exists() else {}
    if not isinstance(hooks, dict) or not isinstance(hooks.get("hooks", {}), dict):
        raise ValueError("Existing hooks.json has an unsupported format. It was left unchanged.")
    for groups in hooks.get("hooks", {}).values():
        if not isinstance(groups, list) or any(
            not isinstance(group, dict) or not isinstance(group.get("hooks"), list)
            or any(not isinstance(handler, dict) for handler in group["hooks"])
            for group in groups
        ):
            raise ValueError("Existing hooks.json has an unsupported event format. It was left unchanged.")
    source = config_path.read_text() if config_path.exists() else ""
    updated = enable_hooks(source)  # Validate everything before writing either file.
    command = shlex.join([sys.executable, str(state / "app" / "lens.py"), "--state", str(state), "capture"])
    manifest_path = state / "installation.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    old_command = manifest.get("command")
    for event in sorted(EVENTS):
        groups = hooks.setdefault("hooks", {}).setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError("Existing hooks.json has an unsupported event format. It was left unchanged.")
        for group in groups:
            group["hooks"] = [h for h in group.get("hooks", []) if h.get("command") not in {old_command, command}]
        groups[:] = [g for g in groups if g.get("hooks")]
        groups.append({"hooks": [{"type": "command", "command": command, "timeout": 3}]})
    backup = state / "backup"
    backup.mkdir(exist_ok=True, mode=0o700)
    for path in (hooks_path, config_path):
        if path.exists() and not (backup / path.name).exists():
            shutil.copy2(path, backup / path.name)
            (backup / path.name).chmod(0o600)
    atomic_json(hooks_path, hooks)
    write_text(config_path, updated)
    atomic_json(manifest_path, {"command": command, "codex_home": str(codex_home)})


def copy_app() -> None:
    target = state_dir() / "app"
    target.mkdir(exist_ok=True, mode=0o700)
    origin = Path(__file__).resolve().parent
    if origin != target / "lens_codex":
        shutil.copytree(origin, target / "lens_codex", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    write_text(target / "lens.py", "from lens_codex.cli import main\nmain()\n")


def install_service() -> None:
    if sys.platform != "darwin":
        raise ValueError("Automatic desktop setup currently supports macOS. Run `python3 -m lens_codex serve` on Linux.")
    copy_app()
    state = state_dir()
    (state / "service.json").unlink(missing_ok=True)
    path = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
    path.parent.mkdir(parents=True, exist_ok=True)
    plist = {"Label": LABEL, "ProgramArguments": [sys.executable, str(state / "app/lens.py"),
             "--state", str(state), "serve"], "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False},
             "ProcessType": "Background", "ThrottleInterval": 10, "Umask": 0o077,
             "StandardOutPath": str(state / "service.log"), "StandardErrorPath": str(state / "service.log")}
    if os.environ.get("LENS_CODEX_PLUGIN_ROOT"):
        plist["EnvironmentVariables"] = {"LENS_CODEX_PLUGIN_ROOT": os.environ["LENS_CODEX_PLUGIN_ROOT"]}
    path.write_bytes(plistlib.dumps(plist))
    path.chmod(0o600)
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    result = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], capture_output=True)
    if result.returncode:
        raise ValueError("Could not start the local helper. Run `python3 -m lens_codex serve` to see the problem.")


def uninstall() -> None:
    manifest_path = state_dir() / "installation.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        remove_user_hooks(manifest)
        manifest_path.unlink()
    # Keep hooks enabled: another integration may use them now. Never restore an old whole-file backup.
    from .state import config, save_config
    settings = config()
    settings["enabled"] = False
    settings.pop("api_key", None)
    save_config(settings)
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
        (Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")).unlink(missing_ok=True)
