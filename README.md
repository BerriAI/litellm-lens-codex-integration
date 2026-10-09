# Codex → Lens

**One Codex chat. One Lens trace.**

Connect Codex to Lens, then see the conversation and its tool calls together in Lens. Each completed turn is added to the same trace, including when you reopen a chat.

**Public preview, maintained by BerriAI.** An independent Codex plugin, not an OpenAI product.

## Install

You need **Codex desktop or CLI**, **Python 3.11+**, and a **running Lens service and a Lens tracing key**. Automatic setup currently supports **macOS**.

### From Terminal (recommended)

**Same install for desktop and CLI.** Open Terminal and paste:

```bash
curl -fsSL https://raw.githubusercontent.com/BerriAI/litellm-lens-codex-integration/main/install.sh | bash
```

It finds your installed Codex and installs the plugin. Desktop users don't need to install the CLI separately.

1. Answer three questions in Terminal: your **Lens ingestion URL**, **Lens tracing key** (hidden while typing), and **agent name**. Confirm when you're ready to start recording.
2. Start a **new chat in Codex desktop or CLI**, then open **Lens → Traces** in standalone Lens or the LiteLLM dashboard.

Recording starts only when you confirm. Enter your key at the hidden prompt, never in a command or chat. No separate desktop app or Python packages are installed.

### From Codex CLI

Follow all five steps to install and connect the plugin for **both desktop and CLI**:

1. **Install the plugin** from Terminal:

   ```bash
   codex plugin marketplace add BerriAI/litellm-lens-codex-integration
   codex plugin add litellm-lens@berriai-lens
   ```

2. **Start a new chat** in Codex desktop or CLI.

3. **Send this message to start setup:**

   > Use lens-setup to connect my Codex chats to LiteLLM Lens.

4. **Complete setup in the Terminal window that opens.** Enter your Lens ingestion URL, Lens tracing key (hidden while typing), and agent name. Confirm to start recording.

5. **Start another new chat and complete a turn.** Open **Lens → Traces** in standalone Lens or the LiteLLM dashboard to see it.

Desktop and CLI use the same profile by default. If you use a custom `CODEX_HOME`, install into the profile your desktop uses.

### Ask Codex

Paste this into a Codex desktop or CLI chat:

```text
Install and set up https://github.com/BerriAI/litellm-lens-codex-integration for me. Follow AI_SETUP.md in that repository, handle the installation, and open the private Terminal setup for my Lens ingestion URL, key, and agent name.
```

Codex installs the plugin and opens Terminal. You enter the three values, confirm recording, then start a new chat. Your key stays out of the conversation.

Lens can run with ClickHouse alone. Follow the [Lens quickstart](https://github.com/BerriAI/lens/blob/main/deploy/lens/README.md), then open **Settings > Tracing > Connect an agent** to create your tracing key. For a gateway-bundled release, use its **Lens > Traces > Set up tracing** flow. Copy the full traces endpoint, including `/v1/traces`. Your Codex model, login, and provider stay unchanged.

## What you'll see

A trace contains the user prompts, final replies, and local tool calls for one chat. Tool calls appear under the turn that made them. Cancelled turns are marked as interrupted. Your chosen agent name is used in the Traces agent column and Investigations agent selector.

- Reopening a chat continues its existing trace.
- A different chat gets a different trace.
- Only activity after setup is recorded; old conversations are not uploaded.
- A trace updates after each completed or interrupted turn, not on every streamed token.

**Coverage matters:** this preview combines lifecycle hooks with visible transcript items for newly recorded turns. It includes commentary, repeated messages, shell and file changes, MCP results, web-search activity, compaction markers, and subagents. Resumed turns stay in the same chat trace, while child agents have separate branches. Model names and tool failures follow the recorded items. Assistant messages do not count as additional model calls.

Hidden reasoning, images, binary attachments, and complete model-request prompts are not exported. Media gets an omission marker, and unknown item types get an unsupported-item marker. Missing or unreadable transcripts fall back to hook content with a warning. The transcript reader streams long files, but token usage can remain unknown for very long turns. The integration was exercised with Codex 0.160.0; future transcript formats may need an update. Do not use it to measure complete model-call coverage.

Large tool outputs are shortened with a visible marker. Images, internal transport metadata, and duplicate MCP results are removed. Completed turns upload in small, recoverable batches; a busy local database does not block your Codex chat.

When available, token totals come from the current turn's local Codex transcript. They are attached to the turn; the integration does **not** invent individual LLM spans, model-call durations, or a dollar cost. A missing token count is reported in the setup window. Codex's transcript format is not a stable API, so compatibility tests are required when it changes.

## Pause or remove

Ask Codex to **“Use lens-setup to pause recording.”** You can also ask it to open Lens settings for optional browser controls. That page does not need to stay open. From a downloaded repository, the equivalent commands are:

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

Then remove **LiteLLM Lens** from Codex’s Plugins page. Uninstalling only the plugin stops future hook capture; remove the helper first to stop delivery of any queued turns and clear the local key. Other Codex hooks are preserved. Queued data remains in `~/.local/share/litellm-lens-codex` for your review; delete that folder if you also want to remove it. Traces already sent to Lens are not deleted.

## If nothing appears

Ask Codex to open Lens settings. The optional page shows whether a turn is still in progress, waiting to send, blocked by Lens, or delivered.

- **No recent activity:** make sure the plugin is enabled, reopen setup, and start a new chat. Check `/hooks` in the Codex CLI if your organization manages hooks centrally.
- **Lens rejected the key:** open **Settings**, replace the key, and reconnect. The saved turns retry automatically.
- **Delivery not confirmed:** the helper checks Lens before retrying. It will not blindly resend a possibly accepted trace. If Lens never received it, the queued content is kept for diagnosis rather than silently dropped.
- **Token usage unavailable:** the conversation still arrives. The current Codex version may use a transcript format this preview doesn't recognize.

Remote/SSH Codex sessions need the helper on the machine where Codex runs. This Mac installer does not capture activity from another computer or Codex Cloud.

## Privacy and maintenance

Prompts and tool output can contain source code and secrets. Enable recording only for a Lens deployment you intend to share them with. Common API-key and Bearer-token patterns are redacted, but this is **not** a complete secret detector.

The key and pending turns are stored in a private directory on your Mac. The key is not put in hook commands, browser storage, URLs, or logs. Sent prompt/tool content is removed from the local queue; delivery metadata is retained for seven days. TLS verification stays enabled. No analytics or third-party browser assets are used.

Setup asks Codex to trust **only this installed plugin’s seven hooks**, after you opt in. Updating an existing connection preserves its paused or enabled state. It does not disable hook trust or approve unrelated hooks. The old OTel bridge, if installed, is left alone; disable it separately to prevent double recording.

See [architecture](docs/architecture.md) and [test results](docs/testing.md) for the exact guarantees and preview limits.

To update, run the install command again. It refreshes the plugin and helper, keeping your settings and paused/recording state. An existing connection skips the setup questions.

Setup refreshes the local exporter and reviews the current plugin hook definitions. Updates are explicit; nothing is downloaded or replaced in the background.

## Contributing

Issues and pull requests are welcome. Public access does not grant push access; changes are reviewed by BerriAI maintainers. See [security reporting](SECURITY.md) for vulnerabilities.
