# How it works

```
Codex hooks → private event files → SQLite batches → HTTPS OTLP/JSON → Lens
                                ↑
                   current-turn token totals
```

The hook command only writes locally. It does not contact the gateway, call another model, alter a tool result, or inject conversation context. The background helper handles delivery. All dependencies are in Python's standard library.

## Identity and grouping

The trace ID is a deterministic hash of the installation ID and Codex session ID. A turn's span ID is derived from its session and turn IDs; tool span IDs also include Codex's tool-use ID. New sessions and installations cannot collide accidentally. A resumed session retains its trace ID.

Each completed turn is an immutable root agent span within the session's trace, with that turn's tool spans as children. There is deliberately no synthetic long-lived session root: a desktop chat can stay open indefinitely, and LiteLLM's append-based storage is not an upsert API. Repeatedly rewriting a root would corrupt counts or select stale content. Multiple turns in one trace are multiple roots, with real per-turn timing.

A whole session is one Lens run. Investigating a session with many turns therefore investigates those turns together. Trace duration spans the earliest captured turn to the latest, including time between turns; it is not a measure of active compute time.

## Sources and compatibility

The primary input is Codex's [documented lifecycle hooks](https://learn.chatgpt.com/docs/hooks): SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, Interrupt, and SessionEnd. Tool output is the output exposed to Codex. Media, internal MCP metadata, and duplicate structured/text results are removed before storage. Content fields are bounded to 128 KiB of UTF-8 text with a visible beginning/end excerpt marker. An incoming hook over 16 MB is rejected with a capture error.

A narrow transcript reader uses only the hook-supplied path under the configured Codex home. It looks for exact turn IDs, completed-turn markers and token counts. A bounded lookbehind finds the turn identity written just before the prompt hook. It never exports raw transcripts, rate-limit/account fields, system instructions, or reasoning text. Unknown or missing usage remains unknown.

The supported desktop path uses the same app-server protocol and hook trust configuration as Codex. Setup discovers its own exact hook commands through `hooks/list` and matches the installed plugin’s source path and exact command, and persists only their current hashes through `config/batchWrite`. This does not bypass Codex's hook trust mechanism. Organization-managed restrictions still apply.

Known coverage limits are in the README. In particular, raw LLM request/response tracing would require a supported Codex export surface; a local adapter cannot recover data that Codex never exposes. The hook/transcript combination must not be described as a complete model-traffic recorder.

## Delivery semantics

Events are saved atomically to private files before the hook returns. One importer commits them to SQLite; if SQLite is busy, the files remain for the next pass. Completed turns are assembled into requests of at most 512 KiB, including JSON escaping and envelopes. The exact serialized batches and their individual acknowledgements are retained until delivery is confirmed. The helper marks a batch uncertain *before* issuing the network request, so a crash after acceptance cannot trigger a blind replay.

- HTTP success: acknowledge that batch. After all batches for a turn succeed, remove its queued prompt/tool data and retain a seven-day delivery receipt.
- Authentication/validation rejection: hold the turn and show an actionable error; reconnecting retries it.
- Rate limit: retry the same batch with bounded exponential backoff.
- Lost acknowledgement, server error, partial success, or crash: read the trace back and verify every span ID. Only confirmed delivery clears the batch. Never blindly resend an uncertain batch.

This favors avoiding duplicate traces over automatic recovery when the server's acceptance is unknowable. Pending content is bounded at 256 MB; reaching that limit stops capture with a visible error, rather than deleting older unsent content. The helper does not promise exactly-once delivery across an arbitrary OTLP backend.

Health reporting uses separate atomic files, so reporting a database error never needs another database write. The sender catches failures and continues; status detects a stale heartbeat. A rejected oversized request from 0.2.3 is rebuilt from its saved events with the new content and batching rules. Previously uncertain requests are reconciled unchanged.

See the [SDK comparison and design decision](export-design.md).

## Local setup server

The setup server binds only to `127.0.0.1`, validates Host and Origin, requires a per-process CSRF token for mutations, disallows framing, uses no external assets, and never returns the API key. Remote gateways require HTTPS; localhost HTTP is available for development. Redirects are not followed with credentials.

Hook/config changes are backed up. Codex discovers the hooks from the installed plugin. Setup enables hooks and trusts only this plugin’s definitions; it does not copy them into the user’s hooks file. Migration from the earlier script installer removes only its recorded legacy command. Existing model/provider/OTel settings remain untouched. Private files use owner-only permissions.
