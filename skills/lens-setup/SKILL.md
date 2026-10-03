---
name: lens-setup
description: Set up, pause, resume, inspect, or remove the LiteLLM Lens integration for local Codex chats.
---

Use the bundled launcher at `../../scripts/run.sh`, resolved relative to this skill directory.

## Set up

1. If already configured, run `bash <absolute-launcher-path> setup` to refresh the helper. This preserves the recording/paused state. Use `status` to check without exposing the key.
2. For first-time setup on macOS, run `open -a Terminal <absolute-path-to-scripts/setup.command>` using the sibling `setup.command` file next to `run.sh`. This opens a real Terminal window for the user to enter their gateway URL, hidden LiteLLM key, and agent name. Do not run interactive setup in an agent tool terminal, read the user's terminal input, or submit answers for them. Never ask them to paste a key into chat or put it into a command, environment variable, or tool argument.
3. Setup explains what will be recorded and asks the user to connect. The user must answer themselves. If they prefer a browser, run `bash <absolute-launcher-path> setup --browser` instead; they can enter credentials and select Connect to Lens there. Do not click consent for them.
4. Ask them to start a new local Codex chat after setup, then check Lens > Traces.

The launcher requires Python 3.11+ and macOS for automatic background setup. If it reports a missing requirement, explain it; do not install software or disable security settings automatically.

## Controls

Use the same launcher with `status`, `pause`, `resume`, or `uninstall` only as requested. `status` never returns the key. For settings or visual recording controls, use `setup --browser`; the page is optional and does not need to stay open. Uninstall removes the background helper and local key; the user can then remove the plugin from Plugins. Do not delete queued data or gateway traces without a separate request.

A paused connection stays paused until the user resumes it. Do not send historical chats or manually read/export transcripts. Capture uses approved lifecycle hooks for future activity only. The exporter is independent of the user's model provider and login.
