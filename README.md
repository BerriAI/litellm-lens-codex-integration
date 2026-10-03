# Codex → Lens

**One Codex chat. One Lens trace.**

Connect Codex to your LiteLLM gateway, then see the conversation and its tool calls together in Lens. Each completed turn is added to the same trace, including when you reopen a chat.

**Private preview, maintained by BerriAI.** This is an independent integration, not an OpenAI product. Keep this repository private while we test it.

## Set up on your Mac

You need Codex installed and signed in, Python 3.11 or newer, and a LiteLLM gateway with tracing enabled.

1. **[Download the repository](https://github.com/BerriAI/litellm-lens-codex-integration/archive/refs/heads/main.zip)** and unzip it.
2. Open **`Setup.command`**. If macOS asks, right-click it and choose **Open**. A setup window opens in your browser.
3. Enter your **gateway URL** and **LiteLLM virtual key**, name your agent, and select **Connect to Lens**.
4. Start a new chat in Codex. If the app was already open, restart it to load the new hooks.

That's it. Open **Lens → Traces** on your gateway and select your agent name. You don't need a Lens worker to view traces; a worker is needed to run investigations.

The setup window shows recent deliveries and lets you pause recording. Open `Setup.command` again whenever you need it.

### Prefer a terminal?

```bash
git clone git@github.com:BerriAI/litellm-lens-codex-integration.git
cd litellm-lens-codex-integration
python3 -m lens_codex setup
```

No Python packages need to be installed. The Mac helper starts when you sign in. It does not change your Codex model, login, provider, or other integrations.

## What you'll see

A trace contains the user prompts, final replies, and local tool calls for one chat. Tool calls appear under the turn that made them. Cancelled turns are marked as interrupted. Your chosen agent name is used in the Traces agent column and Investigations agent selector.

- Reopening a chat continues its existing trace.
- A different chat gets a different trace.
- Only activity after setup is recorded; old conversations are not uploaded.
- A trace updates after each completed or interrupted turn, not on every streamed token.

**Coverage matters:** this preview captures Codex's lifecycle hooks, not its complete internal model traffic. It includes shell, file-editing, and local/MCP tools that emit those hooks. Hosted web-search internals, hidden reasoning, images/binary attachments, and individual model-request prompts are not exported. Nested subagent reconstruction is not supported yet. Don't use this preview to measure complete model-call coverage.

When available, token totals come from the current turn's local Codex transcript. They are attached to the turn; the integration does **not** invent individual LLM spans, model-call durations, or a dollar cost. A missing token count is reported in the setup window. Codex's transcript format is not a stable API, so compatibility tests are required when it changes.

## Pause or remove

Use **Pause recording** in the setup window, or:

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

Other Codex hooks are preserved. The local key is removed. Queued data remains in `~/.local/share/litellm-lens-codex` for your review; delete that folder if you also want to remove it. Traces already sent to your gateway are not deleted.

## If nothing appears

Open the setup window first. It will show whether a turn is still in progress, waiting to send, blocked by the gateway, or delivered.

- **No recent activity:** restart Codex and start a new chat. Check `/hooks` in the Codex CLI if your organization manages hooks centrally.
- **Gateway rejected the key:** open **Settings**, replace the key, and reconnect. The saved turns retry automatically.
- **Delivery not confirmed:** the helper checks Lens before retrying. It will not blindly resend a possibly accepted trace. If the gateway never received it, the queued content is kept for diagnosis rather than silently dropped.
- **Token usage unavailable:** the conversation still arrives. The current Codex version may use a transcript format this preview doesn't recognize.

Remote/SSH Codex sessions need the helper on the machine where Codex runs. This Mac installer does not capture activity from another computer or Codex Cloud.

## Privacy and maintenance

Prompts and tool output can contain source code and secrets. Enable recording only for a gateway you intend to share them with. Common API-key and Bearer-token patterns are redacted, but this is **not** a complete secret detector.

The key and pending turns are stored in a private directory on your Mac. The key is not put in hook commands, browser storage, URLs, or logs. Sent prompt/tool content is removed from the local queue; delivery metadata is retained for seven days. TLS verification stays enabled. No analytics or third-party browser assets are used.

Setup asks Codex to trust **only this integration's seven hooks**, after you opt in. It does not disable hook trust or approve unrelated hooks. The old OTel bridge, if installed, is left alone; disable it separately to prevent double recording.

See [architecture](docs/architecture.md) and [test results](docs/testing.md) for the exact guarantees and preview limits.

To update, download the latest private repository version and open `Setup.command` again. Updates are explicit; nothing is downloaded or replaced in the background.
