import concurrent.futures
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error

from lens_codex import delivery, install, state, trace, transcript


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"LENS_CODEX_HOME": str(self.root / "state")})
        self.env.start()
        self.codex = self.root / "codex"
        self.codex.mkdir()
        state.save_config({"enabled": True, "gateway": "https://gateway.example", "api_key": "test-private-key",
                           "agent_name": "research_agent", "installation_id": "installation-1", "codex_home": str(self.codex)})

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def event(self, kind, session="session-1", turn="turn-1", **extra):
        return {"hook_event_name": kind, "session_id": session, "turn_id": turn, "model": "test-model", **extra}

    def record(self, session="session-1", turn="turn-1", output="answer"):
        state.capture(self.event("UserPromptSubmit", session, turn, prompt="What changed?"), 100)
        state.capture(self.event("PreToolUse", session, turn, tool_name="Bash", tool_use_id="tool-1",
                                 tool_input={"command": "echo hello"}), 101)
        state.capture(self.event("PostToolUse", session, turn, tool_name="Bash", tool_use_id="tool-1",
                                 tool_input={"command": "echo hello"}, tool_response=output), 102)
        state.capture(self.event("Stop", session, turn, last_assistant_message="Done."), 103)

    def turn(self, turn="turn-1"):
        with state.database() as db:
            return dict(db.execute("SELECT * FROM turns WHERE turn=?", (turn,)).fetchone())

    def payload(self, turn="turn-1"):
        with state.database() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM events WHERE turn=? ORDER BY id", (turn,))]
        return trace.build_trace(self.turn(turn), rows, state.config(), None)


class CaptureTests(Case):
    def test_one_session_one_trace_with_distinct_turns(self):
        self.record(turn="a")
        self.record(turn="b")
        a = self.payload("a")["resourceSpans"][0]["scopeSpans"][0]["spans"]
        b = self.payload("b")["resourceSpans"][0]["scopeSpans"][0]["spans"]
        self.assertEqual(a[0]["traceId"], b[0]["traceId"])
        self.assertNotEqual(a[0]["spanId"], b[0]["spanId"])
        self.assertEqual(a[1]["parentSpanId"], a[0]["spanId"])
        self.assertNotEqual(a[1]["spanId"], b[1]["spanId"])

    def test_distinct_sessions_do_not_mix(self):
        self.record(session="a", turn="a")
        self.record(session="b", turn="b")
        self.assertNotEqual(self.payload("a")["resourceSpans"], self.payload("b")["resourceSpans"])

    def test_full_unicode_tool_output(self):
        text = "雪🌱" * 10000 + "END"
        self.record(output=text)
        spans = self.payload()["resourceSpans"][0]["scopeSpans"][0]["spans"]
        attributes = {a["key"]: a["value"] for a in spans[1]["attributes"]}
        self.assertEqual(attributes["gen_ai.tool.call.result"]["stringValue"], text)

    def test_duplicate_hooks_are_idempotent(self):
        self.record()
        state.capture(self.event("Stop", last_assistant_message="Done."), 104)
        with state.database() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE turn='turn-1'").fetchone()[0], 4)
        self.assertEqual(self.turn()["ended"], 103)

    def test_no_backfill_without_prompt(self):
        self.assertFalse(state.capture(self.event("Stop", last_assistant_message="old chat")))
        with state.database() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM turns").fetchone()[0], 0)

    def test_pause_stops_capture(self):
        settings = state.config()
        settings["enabled"] = False
        state.save_config(settings)
        self.assertFalse(state.capture(self.event("UserPromptSubmit", prompt="private")))

    def test_concurrent_sessions(self):
        def record(index):
            self.record(session=f"session-{index}", turn=f"turn-{index}")
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(record, range(40)))
        with state.database() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM turns").fetchone()[0], 40)
            self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 160)

    def test_interrupt_marks_unfinished_tool(self):
        state.capture(self.event("UserPromptSubmit", prompt="hello"), 100)
        state.capture(self.event("PreToolUse", tool_name="Bash", tool_use_id="1", tool_input={}), 101)
        state.capture(self.event("Interrupt"), 102)
        spans = self.payload()["resourceSpans"][0]["scopeSpans"][0]["spans"]
        self.assertEqual([s["status"]["code"] for s in spans], [2, 2])

    def test_session_end_closes_recording_turn(self):
        state.capture(self.event("UserPromptSubmit", prompt="hello"), 100)
        state.capture(self.event("SessionEnd", turn=""), 110)
        self.assertEqual(self.turn()["status"], "pending")
        self.assertIn("ended", self.turn()["warning"])

    def test_rejects_missing_turn_id(self):
        with self.assertRaises(ValueError):
            state.capture(self.event("UserPromptSubmit", turn="", prompt="x"))

    def test_untrusted_transcript_path_not_read(self):
        self.assertIsNone(state.transcript_path({"transcript_path": "/etc/passwd"}, state.config()))
        target = self.codex / "sessions" / "safe.jsonl"
        self.assertEqual(state.transcript_path({"transcript_path": str(target)}, state.config()), target.resolve())

    def test_transcript_symlink_escape(self):
        (self.codex / "sessions").mkdir()
        (self.codex / "sessions" / "escape").symlink_to(self.root)
        self.assertIsNone(state.transcript_path({"transcript_path": str(self.codex / "sessions/escape/private")}, state.config()))

    def test_private_files(self):
        self.record()
        for name in ("config.json", "events.sqlite3"):
            self.assertEqual((state.state_dir() / name).stat().st_mode & 0o777, 0o600)

    def test_oversize_not_silently_truncated(self):
        with patch.object(state, "MAX_EVENT_BYTES", 50), self.assertRaisesRegex(ValueError, "not truncated"):
            state.capture(self.event("UserPromptSubmit", prompt="x" * 200))

    def test_unknown_fields_are_not_saved(self):
        state.capture(self.event("UserPromptSubmit", prompt="hello", account_id="PRIVATE", reasoning="SECRET"))
        with state.database() as db:
            data = db.execute("SELECT data FROM events").fetchone()[0]
            self.assertNotIn("PRIVATE", data)
            self.assertNotIn("SECRET", data)


class TranscriptTests(Case):
    def write_records(self, records):
        path = self.root / "usage.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        return str(path)

    def message(self, kind, **data):
        return {"type": "event_msg", "payload": {"type": kind, **data}}

    def count(self, total=20, value=10):
        return self.message("token_count", info={"total_token_usage": {"input_tokens": total},
            "last_token_usage": {"input_tokens": value, "cached_input_tokens": 3, "output_tokens": 2}})

    def test_exact_turn_and_duplicate_counts(self):
        path = self.write_records([self.message("task_started", turn_id="old"), self.count(1, 999),
            self.message("task_started", turn_id="new"), self.count(), self.count(), self.count(40, 15),
            self.message("task_complete", turn_id="new"), self.count(50, 1000)])
        usage, done = transcript.usage_for_turn(path, 0, "new")
        self.assertTrue(done)
        self.assertEqual(usage["input_tokens"], 25)
        self.assertEqual(usage["output_tokens"], 4)

    def test_unknown_usage_is_not_zero(self):
        path = self.write_records([self.message("task_started", turn_id="new"),
                                  self.message("task_complete", turn_id="new")])
        self.assertEqual(transcript.usage_for_turn(path, 0, "new"), (None, True))

    def test_partial_line_is_tolerated(self):
        path = self.write_records([self.message("task_started", turn_id="new"), self.count()])
        with open(path, "a") as f:
            f.write('{"payload":')
        self.assertEqual(transcript.usage_for_turn(path, 0, "new")[0]["input_tokens"], 10)

    def test_context_before_hook_offset_is_used_without_old_tokens(self):
        initial = [self.message("task_started", turn_id="old"), self.count(1, 900),
                   self.message("task_started", turn_id="new")]
        path = self.write_records(initial)
        offset = Path(path).stat().st_size
        with open(path, "a") as stream:
            for item in [self.count(), self.message("task_complete", turn_id="new")]:
                stream.write(json.dumps(item) + "\n")
        usage, done = transcript.usage_for_turn(path, offset, "new")
        self.assertTrue(done)
        self.assertEqual(usage["input_tokens"], 10)

    def test_missing_file_is_unknown(self):
        self.assertEqual(transcript.usage_for_turn("/missing/file", 0, "x"), (None, False))

    def test_non_current_transcript_is_ignored(self):
        path = self.write_records([self.message("task_started", turn_id="old"), self.count(),
                                  self.message("task_complete", turn_id="old")])
        self.assertEqual(transcript.usage_for_turn(path, 0, "new"), (None, False))

    def test_invalid_tokens_are_not_exported(self):
        path = self.write_records([self.message("task_started", turn_id="new"), self.count(value=-20)])
        self.assertIsNone(transcript.usage_for_turn(path, 0, "new")[0])


class DeliveryTests(Case):
    def test_success_clears_private_content(self):
        self.record()
        with patch.object(delivery, "request", return_value={}) as req:
            delivery.flush(200)
        self.assertEqual(req.call_count, 1)
        self.assertEqual(self.turn()["status"], "sent")
        self.assertIsNone(self.turn()["payload"])
        with state.database() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE turn='turn-1'").fetchone()[0], 0)

    def test_repeated_flush_does_not_repeat_upload(self):
        self.record()
        with patch.object(delivery, "request", return_value={}) as req:
            delivery.flush(200)
            delivery.flush(201)
            self.assertEqual(req.call_count, 1)

    def test_auth_error_visible_and_retryable(self):
        self.record()
        error = urllib.error.HTTPError("https://example", 401, "secret raw error", {}, io.BytesIO())
        with patch.object(delivery, "request", side_effect=error):
            delivery.flush(200)
        self.assertEqual(self.turn()["status"], "blocked")
        self.assertNotIn("secret", self.turn()["error"])
        delivery.retry_blocked()
        with patch.object(delivery, "request", return_value={}):
            delivery.flush(201)
        self.assertEqual(self.turn()["status"], "sent")

    def test_rate_limit_retries_same_payload(self):
        self.record()
        error = urllib.error.HTTPError("https://example", 429, "rate limited", {}, io.BytesIO())
        with patch.object(delivery, "request", side_effect=error):
            delivery.flush(200)
        saved = json.loads(self.turn()["payload"])
        with patch.object(delivery, "request", return_value={}) as req:
            delivery.flush(201)
            self.assertFalse(req.called)
            delivery.flush(210)
            self.assertEqual(req.call_args.args[2], saved)

    def test_lost_acknowledgement_never_blindly_reposts(self):
        self.record()
        with patch.object(delivery, "request", side_effect=TimeoutError):
            delivery.flush(200)
        self.assertEqual(self.turn()["status"], "uncertain")
        with patch.object(delivery, "request", return_value={"spans": []}) as req:
            delivery.flush(240)
            self.assertEqual(len(req.call_args.args), 2)  # read-only reconciliation
        self.assertEqual(self.turn()["status"], "uncertain")

    def test_reconcile_proves_all_spans_exist(self):
        self.record()
        expected = self.payload()["resourceSpans"][0]["scopeSpans"][0]["spans"]
        with patch.object(delivery, "request", side_effect=TimeoutError):
            delivery.flush(200)
        with patch.object(delivery, "request", return_value={"spans": [{"span_id": s["spanId"]} for s in expected]}):
            delivery.flush(240)
        self.assertEqual(self.turn()["status"], "sent")

    def test_partial_success_needs_review(self):
        self.record()
        with patch.object(delivery, "request", return_value={"partialSuccess": {"rejectedSpans": "1"}}):
            delivery.flush(200)
        self.assertEqual(self.turn()["status"], "uncertain")

    def test_pause_stops_delivery(self):
        self.record()
        settings = state.config()
        settings["enabled"] = False
        state.save_config(settings)
        with patch.object(delivery, "request") as req:
            delivery.flush(200)
            self.assertFalse(req.called)

    def test_status_never_contains_key_or_content(self):
        self.record(output="private code")
        output = json.dumps(delivery.status())
        self.assertNotIn("test-private-key", output)
        self.assertNotIn("private code", output)


class InstallTests(Case):
    def test_preserves_other_hooks_and_is_idempotent(self):
        existing = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "existing-command"}]}]}}
        (self.codex / "hooks.json").write_text(json.dumps(existing))
        (self.codex / "config.toml").write_text('model="my-model"\n[otel]\nexporter="none"\n')
        install.install_hooks(self.codex)
        first = (self.codex / "hooks.json").read_text()
        install.install_hooks(self.codex)
        self.assertEqual(first, (self.codex / "hooks.json").read_text())
        self.assertIn("existing-command", first)
        self.assertIn('model="my-model"', (self.codex / "config.toml").read_text())
        self.assertIn('exporter="none"', (self.codex / "config.toml").read_text())

    def test_uninstall_only_removes_owned_hooks(self):
        existing = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "existing-command"}]}]}}
        (self.codex / "hooks.json").write_text(json.dumps(existing))
        install.install_hooks(self.codex)
        with patch.object(install.sys, "platform", "linux"):
            install.uninstall()
        self.assertEqual(json.loads((self.codex / "hooks.json").read_text()), existing)
        self.assertNotIn("api_key", state.config())

    def test_inline_config_is_not_clobbered(self):
        source = 'features = {hooks = false, another = true}\n'
        with self.assertRaises(ValueError):
            install.enable_hooks(source)

    def test_updates_only_hooks_feature(self):
        source = '[features]\nanother=true\nhooks=false\n[otel]\nexporter="none"\n'
        changed = install.enable_hooks(source)
        self.assertIn("hooks = true", changed)
        self.assertIn("another=true", changed)
        self.assertIn('[otel]\nexporter="none"', changed)

    def test_invalid_json_is_untouched(self):
        path = self.codex / "hooks.json"
        path.write_text("invalid")
        with self.assertRaises(ValueError):
            install.install_hooks(self.codex)
        self.assertEqual(path.read_text(), "invalid")

    def test_invalid_toml_is_untouched(self):
        path = self.codex / "config.toml"
        path.write_text("invalid = ")
        with self.assertRaises(ValueError):
            install.install_hooks(self.codex)
        self.assertEqual(path.read_text(), "invalid = ")


class UrlTests(unittest.TestCase):
    def test_allowed_urls(self):
        for url in ("https://example.com", "https://example.com/ui", "https://example.com/v1/traces"):
            self.assertEqual(state.gateway_url(url), "https://example.com")
        self.assertEqual(state.gateway_url("http://127.0.0.1:5000"), "http://127.0.0.1:5000")

    def test_rejects_unsafe_or_ambiguous_urls(self):
        for url in ("http://example.com", "https://user:pass@example.com", "https://example.com?key=x",
                    "file:///etc/passwd", "https://example.com/ui/lens", "https://example.com/#a"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                state.gateway_url(url)


if __name__ == "__main__":
    unittest.main()
