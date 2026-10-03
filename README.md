# Codex → Lens

**One Codex chat. One Lens trace.**

Connect Codex to your LiteLLM gateway, then see the conversation and its tool calls together in Lens. Each completed turn is added to the same trace, including when you reopen a chat.

**Public preview, maintained by BerriAI.** An independent Codex plugin, not an OpenAI product.

## Install

You need **Codex desktop or CLI**, **Python 3.11+**, and a **LiteLLM gateway with tracing enabled**. Automatic setup currently supports **macOS**.

### From Terminal (recommended)

**Same install for desktop and CLI.** Open Terminal and paste:

```bash
curl -fsSL https://raw.githubusercontent.com/BerriAI/litellm-lens-codex-integration/main/install.sh | bash
```

It finds your installed Codex, installs the plugin, and opens setup in your browser. Desktop users don't need to install the CLI separately.

1. Enter your **gateway URL**, **LiteLLM virtual key**, and **agent name**. Review what is recorded and select **Connect to Lens**.
2. Start a **new chat in Codex desktop or CLI**, then open **Lens → Traces** on your gateway.

Recording starts only when you enable it in setup. Enter your key on that local page, not in Terminal or chat. No separate desktop app or Python packages are installed.

### From Codex CLI

These install the same plugin for **both desktop and CLI**, using the default shared Codex profile:

```bash
codex plugin marketplace add BerriAI/litellm-lens-codex-integration
codex plugin add litellm-lens@berriai-lens
```

Start a new desktop or CLI chat and ask: **“Use lens-setup to connect my Codex chats to LiteLLM Lens.”** It opens the same setup page. If you use a custom `CODEX_HOME`, install into the profile your desktop uses.

You don't need a Lens worker to view traces. A worker is needed to run investigations. Your Codex model, login, and provider stay unchanged.

## What you'll see

A trace contains the user prompts, final replies, and local tool calls for one chat. Tool calls appear under the turn that made them. Cancelled turns are marked as interrupted. Your chosen agent name is used in the Traces agent column and Investigations agent selector.

- Reopening a chat continues its existing trace.
- A different chat gets a different trace.
- Only activity after setup is recorded; old conversations are not uploaded.
- A trace updates after each completed or interrupted turn, not on every streamed token.

**Coverage matters:** this preview captures Codex's lifecycle hooks, not its complete internal model traffic. It includes shell, file-editing, and local/MCP tools that emit those hooks. Hosted web-search internals, hidden reasoning, images/binary attachments, and individual model-request prompts are not exported. Nested subagent reconstruction is not supported yet. Don't use this preview to measure complete model-call coverage.

When available, token totals come from the current turn's local Codex transcript. They are attached to the turn; the integration does **not** invent individual LLM spans, model-call durations, or a dollar cost. A missing token count is reported in the setup window. Codex's transcript format is not a stable API, so compatibility tests are required when it changes.

## Pause or remove

Ask Codex to use **lens-setup** to open the control page, then select **Pause recording**. You can also run these commands from a downloaded repository:

```bash
python3 -m lens_codex pause
python3 -m lens_codex resume
python3 -m lens_codex status
```

Pausing stops new capture and delivery. An in-flight request may finish. An incomplete turn is discarded; already completed, queued turns stay on your Mac and are delivered when you resume.

To remove the helper and its hooks:

```bash
python3 -m lens_codex uninstall
```

Then remove **LiteLLM Lens** from Codex’s Plugins page. Uninstalling only the plugin stops future hook capture; remove the helper first to stop delivery of any queued turns and clear the local key. Other Codex hooks are preserved. Queued data remains in `~/.local/share/litellm-lens-codex` for your review; delete that folder if you also want to remove it. Traces already sent to your gateway are not deleted.

## If nothing appears

Ask Codex to open Lens setup first. It will show whether a turn is still in progress, waiting to send, blocked by the gateway, or delivered.

- **No recent activity:** make sure the plugin is enabled, reopen setup, and start a new chat. Check `/hooks` in the Codex CLI if your organization manages hooks centrally.
- **Gateway rejected the key:** open **Settings**, replace the key, and reconnect. The saved turns retry automatically.
- **Delivery not confirmed:** the helper checks Lens before retrying. It will not blindly resend a possibly accepted trace. If the gateway never received it, the queued content is kept for diagnosis rather than silently dropped.
- **Token usage unavailable:** the conversation still arrives. The current Codex version may use a transcript format this preview doesn't recognize.

Remote/SSH Codex sessions need the helper on the machine where Codex runs. This Mac installer does not capture activity from another computer or Codex Cloud.

## Privacy and maintenance

Prompts and tool output can contain source code and secrets. Enable recording only for a gateway you intend to share them with. Common API-key and Bearer-token patterns are redacted, but this is **not** a complete secret detector.

The key and pending turns are stored in a private directory on your Mac. The key is not put in hook commands, browser storage, URLs, or logs. Sent prompt/tool content is removed from the local queue; delivery metadata is retained for seven days. TLS verification stays enabled. No analytics or third-party browser assets are used.

Setup asks Codex to trust **only this installed plugin’s seven hooks**, after you opt in. Updating an existing connection preserves its paused or enabled state. It does not disable hook trust or approve unrelated hooks. The old OTel bridge, if installed, is left alone; disable it separately to prevent double recording.

See [architecture](docs/architecture.md) and [test results](docs/testing.md) for the exact guarantees and preview limits.

To update, run the install command again. It refreshes the plugin and opens setup, keeping your settings and paused/recording state.

Setup refreshes the local exporter and reviews the current plugin hook definitions. Updates are explicit; nothing is downloaded or replaced in the background.

## Contributing

Issues and pull requests are welcome. Public access does not grant push access; changes are reviewed by BerriAI maintainers. See [security reporting](SECURITY.md) for vulnerabilities.
