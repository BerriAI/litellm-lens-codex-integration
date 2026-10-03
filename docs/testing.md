# Preview verification

Verified on **October 2, 2026**, against a LiteLLM test gateway.

## Automated checks

Run the suite without credentials or network access to a real gateway:

```bash
python3 -m unittest discover -s tests -v
```

73 tests cover:

- Stable session/turn identity, resumed conversations, duplicate hooks, multiple prompts in a turn, and isolation between sessions.
- 40 concurrent sessions writing to the same local queue.
- Full Unicode tool output above the old 2 KB limit, explicit oversized-event rejection, and queue capacity limits.
- Interruptions and sessions ending before a final response.
- Pause during a turn, no historical backfill, private file permissions, key redaction, and restricted transcript paths.
- Exact-turn token totals, repeated usage events, partial transcript writes, and missing/changed usage data.
- Real local HTTP transport, credential rejection, rate limiting, lost acknowledgements, partial acceptance, deduplication, and delivery reconciliation.
- Preserving other hooks/settings during install and uninstall; refusing malformed configuration; trusting only the exact installed hooks.
- Native plugin manifests, opt-in capture, paused-state migration, rejecting unrelated plugin hooks, and avoiding duplicate legacy capture.
- The Terminal installer: desktop and CLI detection, paths with spaces, interrupted downloads, install failures, and cleanup outside the repository.
- Loopback setup security: Host, Origin, CSRF, credential-free status responses, and browser security headers.

The test workflow runs the suite on macOS and Linux with Python 3.11 and 3.14. Its live status is available under the repository's Actions tab; local success is not a claim that every hosted run has finished.

## Live gateway checks

| Runtime | Check | Result |
| --- | --- | --- |
| Standalone Codex CLI 0.160.0 | Two turns, reopening the same session | One trace, two agent turns, one child tool span |
| Desktop-bundled Codex 0.159.0-alpha.12.1 | Same two-turn test | Same grouping and content |
| Desktop app-server protocol | Three turns, including a running command cancelled by `turn/interrupt` | Same trace; cancellation recorded as an error |
| Desktop app-server protocol | Trust only this integration's seven hooks through Codex's config API | All seven trusted; no hook-trust bypass flag |
| Both engines | Native Codex plugin installation and trusted plugin hooks | Same two-turn grouping; no hook-trust bypass |
| Both engines | 6,000-character command output | End marker and complete output verified by reading the stored span from Lens |
| Both engines | Token usage | Nonzero totals verified in the gateway trace summary |
| Both engines | Repeat delivery processing | No additional spans |
| Gateway | Configured agent name | Verified in trace data and `/lens/agents`, the Investigations selector's source |

The first live test exposed a token-reader boundary bug. Codex writes the turn context before firing the prompt hook. A bounded lookbehind now establishes the turn identity without importing earlier usage; the fix has a regression test. Live verification also checks the gateway's stored tool output, not just the local payload.

## Browser checks

The real setup page was exercised locally: invalid HTTP gateway rejection, connect against a local test endpoint, successful hook registration, pause, settings/cancel, and reload persistence. Layout was checked at the normal desktop viewport and at 390 px wide, without horizontal overflow. The UI test uses an isolated Codex profile, not the user's configuration.

## What is still a human QA step

The desktop's engine and app-server transport are tested. A click-through conversation in the running desktop app remains the final user QA step; we do not equate a backend test with every desktop UI lifecycle working.

1. Open the local setup window and resume recording.
2. Start a new desktop chat. Ask Codex to run a harmless command and answer a question.
3. In Lens, select `codex` and open the trace. Check the prompt, tool output, and reply.
4. Continue that chat. Confirm the second turn appears in the same trace.
5. Open a different chat. Confirm it produces a different trace.
6. Pause recording. A new turn should not be uploaded.

Not yet certified: nested subagent reconstruction, hosted-tool internals, remote/cloud execution, Windows installation, managed-enterprise hook policies, or compatibility with future transcript schemas. These are explicit preview limits, not passing tests.

## Reproduce live tests

These commands create isolated Codex profiles and send only generated test content. They require an already signed-in Codex auth file and a test gateway; they do not read normal conversation history. Both scripts use normal, exact-hook trust registration; neither bypasses hook trust. Add `--plugin` to exercise installation and capture through the native plugin.

```bash
export LENS_GATEWAY="https://your-test-gateway"
export LENS_API_KEY="<test-key>"
export CODEX_AUTH_FILE="$HOME/.codex/auth.json"
PYTHONPATH=. python3 scripts/live_smoke.py --plugin --engine "$(command -v codex)" --output /tmp/lens-qa
PYTHONPATH=. python3 scripts/live_appserver.py /tmp/lens-qa
```

The private output directory contains an auth-file copy and test payloads. Treat it as sensitive and remove it when finished. Never commit it.
