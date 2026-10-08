import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler
import urllib.request
import urllib.error

from lens_codex import codex, delivery, state, trace
from lens_codex.web import LoopbackHTTPServer
from test_integration import Case


class PrivacyTests(Case):
    def test_redacts_key_before_local_storage(self):
        state.capture(self.event("UserPromptSubmit", prompt="key test-private-key and Bearer abcdefghijklmnop"))
        with state.database() as db:
            saved = db.execute("SELECT data FROM events").fetchone()[0]
        self.assertNotIn("test-private-key", saved)
        self.assertNotIn("abcdefghijklmnop", saved)
        self.assertIn("[REDACTED]", saved)

    def test_redacts_nested_tool_fields(self):
        value = state.redact({"headers": ["Bearer abcdefghijklmnop", {"key": "sk-abcdefghijklmnopqrst"}]}, "")
        self.assertNotIn("abcdefghijklmnop", json.dumps(value))

    def test_pause_discards_incomplete_turn_but_keeps_queued_turn(self):
        self.record(turn="finished")
        state.capture(self.event("UserPromptSubmit", turn="active", prompt="partial private content"))
        state.set_enabled(False)
        self.assertEqual(self.turn("active")["status"], "discarded")
        self.assertEqual(self.turn("finished")["status"], "pending")
        state.set_enabled(True)
        self.assertFalse(state.capture(self.event("Stop", turn="active", last_assistant_message="late reply")))

    def test_preserves_steered_messages_in_same_turn(self):
        state.capture(self.event("UserPromptSubmit", prompt="First request"), 100)
        state.capture(self.event("UserPromptSubmit", prompt="Clarification"), 101)
        state.capture(self.event("Stop", last_assistant_message="Done"), 102)
        root = self.payload()["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        values = {a["key"]: a["value"] for a in root["attributes"]}
        messages = json.loads(values["gen_ai.input.messages"]["stringValue"])
        self.assertEqual([m["content"] for m in messages], ["First request", "Clarification"])

    def test_queue_limit_retains_existing_content(self):
        self.record()
        with patch.object(state, "MAX_QUEUE_BYTES", 1), self.assertRaisesRegex(ValueError, "queue is full"):
            state.capture(self.event("UserPromptSubmit", turn="another", prompt="new"))
        self.assertEqual(self.turn()["status"], "pending")


class TrustTests(Case):
    def test_selects_only_our_exact_user_hooks(self):
        ours = {"key": "ours", "handlerType": "command", "command": "ours-command",
                "sourcePath": str(self.codex / "hooks.json"), "source": "user", "currentHash": "hash"}
        candidates = [ours, {**ours, "key": "unrelated", "command": "another"},
                      {**ours, "key": "project", "source": "project"},
                      {**ours, "key": "wrongpath", "sourcePath": str(self.root / "hooks.json")}]
        self.assertEqual(codex.own_hooks({"data": [{"hooks": candidates}]}, "ours-command", self.codex), [ours])


class HttpTests(Case):
    def setUp(self):
        super().setUp()
        self.requests = []
        self.answer = 200
        self.response_body = {}
        parent = self
        class Mock(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                parent.requests.append((self.path, self.headers.get("Authorization")))
                self.send_response(parent.answer)
                if parent.answer == 302:
                    self.send_header("Location", "http://127.0.0.1:1/stolen")
                self.end_headers()
                self.wfile.write(json.dumps(parent.response_body).encode())
            def do_POST(self):
                parent.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(parent.answer)
                self.end_headers()
                self.wfile.write(json.dumps(parent.response_body).encode())
        self.server = LoopbackHTTPServer(("127.0.0.1", 0), Mock)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        settings = state.config()
        settings["gateway"] = f"http://127.0.0.1:{self.server.server_port}"
        state.save_config(settings)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        super().tearDown()

    def test_real_http_upload(self):
        self.record()
        delivery.flush(200)
        self.assertEqual(self.turn()["status"], "sent")
        self.assertEqual(len(self.requests), 1)
        self.assertIn("resourceSpans", self.requests[0])

    def test_real_http_auth_error_does_not_echo_body(self):
        self.answer = 401
        self.response_body = {"error": "test-private-key"}
        with self.assertRaisesRegex(ValueError, "cannot send traces"):
            delivery.verify(state.config())

    def test_redirect_does_not_forward_key(self):
        self.answer = 302
        with self.assertRaisesRegex(ValueError, "redirect"):
            delivery.request(state.config(), "/v1/traces")
        self.assertEqual(len(self.requests), 1)


class WebTests(unittest.TestCase):
    def test_loopback_startup_never_uses_dns(self):
        with patch("socket.getfqdn", side_effect=AssertionError("No DNS for localhost")):
            with LoopbackHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler) as server:
                self.assertEqual(server.server_name, "localhost")
                self.assertGreater(server.server_port, 0)

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.directory = Path(cls.temp.name)
        cls.log = (cls.directory / "test-server.log").open("w+")
        cls.addClassCleanup(cls.log.close)
        cls.process = subprocess.Popen([sys.executable, "-m", "lens_codex", "--state", str(cls.directory),
                                        "serve", "--port", "0"], stdout=cls.log, stderr=cls.log)
        def stop():
            cls.process.terminate()
            try:
                cls.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.process.kill()
                cls.process.wait()
        cls.addClassCleanup(stop)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if (cls.directory / "service.json").exists():
                break
            if cls.process.poll() is not None:
                break
            time.sleep(.05)
        if not (cls.directory / "service.json").exists():
            cls.log.seek(0)
            raise RuntimeError(f"Test server did not start (exit={cls.process.poll()}): {cls.log.read()}")
        port = json.loads((cls.directory / "service.json").read_text())["port"]
        cls.url = f"http://127.0.0.1:{port}"

    def test_status_has_no_credential_fields(self):
        with urllib.request.urlopen(self.url + "/status") as response:
            value = json.load(response)
        self.assertFalse(value["configured"])
        self.assertNotIn("api_key", value)

    def test_cross_site_request_is_rejected(self):
        req = urllib.request.Request(self.url + "/configure", data=b"{}", headers={"Origin": "https://evil.example"})
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(req)
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()

    def test_rebinding_host_is_rejected(self):
        req = urllib.request.Request(self.url + "/", headers={"Host": "evil.example"})
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(req)
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()

    def test_security_headers_and_setup_page(self):
        with urllib.request.urlopen(self.url + "/") as response:
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
            self.assertIn("Connect to Lens", response.read().decode())
