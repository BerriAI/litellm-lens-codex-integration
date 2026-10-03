# Set up Lens for the user

Install this repository's Codex plugin and open its private setup prompt. Do the installation work yourself; the user only needs to enter their gateway URL, key, and agent name in their own Terminal window, then confirm recording.

## 1. Check requirements

Automatic setup supports macOS and requires Python 3.11 or newer. Check `python3`, `/opt/homebrew/bin/python3`, and `/usr/local/bin/python3` as needed. If no supported Python is installed, explain the requirement and link to https://www.python.org/downloads/macos/. Do not silently install or upgrade runtimes.

Use the user's installed Codex executable. The desktop engine can be at:

```text
/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex
~/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex
```

Otherwise use `codex` from PATH. Check that the executable exists. If it does not support `plugin`, ask the user to update Codex. Desktop and CLI normally share the same profile; preserve an existing `CODEX_HOME` and explain any mismatch with the desktop profile before installing into another profile.

## 2. Install through Codex

Run these commands using the executable found above, quoting its path if necessary:

```bash
codex plugin marketplace add BerriAI/litellm-lens-codex-integration --json
codex plugin marketplace upgrade berriai-lens
codex plugin add litellm-lens@berriai-lens --json
```

Stop and explain any failed command. Read `installedPath` from the final JSON response; do not guess the cache directory. Use files from that installed directory for the next steps. There is no need to clone the repository or download a ZIP.

## 3. Open private setup

Read `skills/lens-setup/SKILL.md` inside `installedPath`, then open the bundled setup in the user's Terminal app:

```bash
open -a Terminal "$installedPath/scripts/setup.command"
```

This uses macOS Terminal, not the agent's execution terminal. For a new connection, the user enters their gateway URL, hidden virtual key, and agent name, and explicitly confirms recording. An existing connection skips the questions and preserves its paused/recording state. Do not ask for the key in chat, pass it in command arguments or environment variables, inspect Terminal input, or answer the consent prompt for them. An install or update is not permission to resume a paused connection.

If the user prefers browser setup, `bash "$installedPath/scripts/run.sh" setup --browser` opens the optional local page instead. Do not run the one-line interactive installer inside an agent execution tool for a new connection: it needs the user's own terminal.

## 4. Verify and finish

After the user says setup has completed, check:

```bash
bash "$installedPath/scripts/run.sh" status
```

`status` never returns the key. Confirm that `configured` is true, and distinguish enabled recording from a configured but paused connection. Explain any setup or delivery error rather than claiming a trace has arrived.

Ask the user to start a **new Codex desktop or CLI chat** and complete one turn. The plugin hooks load in the new chat. Their trace should appear in **Lens → Traces** on their gateway under their chosen agent name. Reopening a chat continues its trace; another chat has a different trace.

Do not change model/provider/login settings, configure Codex's diagnostic OTel exporter, remove unrelated hooks, or upload existing conversations. A Lens worker is not needed to view traces.
