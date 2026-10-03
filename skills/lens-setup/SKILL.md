---
name: lens-setup
description: Set up, pause, resume, inspect, or remove the LiteLLM Lens integration for local Codex chats.
---

Use the bundled launcher at `../../scripts/run.sh`, resolved relative to this skill directory.

## Set up

1. Run `bash <absolute-launcher-path> setup`. It opens a local setup page.
2. Ask the user to enter the gateway URL, LiteLLM key, and agent name in that page. Never ask them to paste a key into chat or put it into a command, environment variable, or tool argument.
3. The page explains what will be recorded. The user enables recording by selecting Connect to Lens; do not click this for them.
4. Ask them to start a new local Codex chat after setup, then check Lens > Traces.

The launcher requires Python 3.11+ and macOS for automatic background setup. If it reports a missing requirement, explain it; do not install software or disable security settings automatically.

## Controls

Use the same launcher with `status`, `pause`, `resume`, or `uninstall` only as requested. `status` never returns the key. `setup` also opens the control page for an existing connection. Uninstall removes the background helper and local key; the user can then remove the plugin from Plugins. Do not delete queued data or gateway traces without a separate request.

A paused connection stays paused until the user resumes it. Do not send historical chats or manually read/export transcripts. Capture uses approved lifecycle hooks for future activity only. The exporter is independent of the user's model provider and login.
