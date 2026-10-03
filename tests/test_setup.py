import contextlib
import getpass
import io
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time
from unittest.mock import patch

from lens_codex import setup, state
from test_integration import Case


class SetupTests(Case):
    def test_connect_requires_explicit_consent_and_never_prints_key(self):
        output = io.StringIO()
        with patch.object(setup.getpass, "getpass", return_value="private-test-key"), \
                patch.object(setup, "configure") as configure:
            self.assertTrue(setup.connect(io.StringIO("https://gateway.example/ui\n\nyes\n"), output))
        configure.assert_called_once_with({"gateway": "https://gateway.example", "api_key": "private-test-key",
                                           "agent_name": "codex"}, enabled=False)
        self.assertNotIn("private-test-key", output.getvalue())
        self.assertIn("prompts, replies, and tool output", output.getvalue())

    def test_declining_consent_never_configures(self):
        with patch.object(setup.getpass, "getpass", return_value="private-test-key"), \
                patch.object(setup, "configure") as configure:
            self.assertFalse(setup.connect(io.StringIO("https://gateway.example\nmy-agent\n\n"), io.StringIO()))
        configure.assert_not_called()

    def test_bad_gateway_can_be_corrected_before_key_prompt(self):
        with patch.object(setup.getpass, "getpass", return_value="private-test-key") as secret, \
                patch.object(setup, "configure") as configure:
            self.assertTrue(setup.connect(io.StringIO("not-a-url\ny\nhttps://gateway.example\nagent\ny\n"), io.StringIO()))
        secret.assert_called_once()
        self.assertEqual(configure.call_args.args[0]["agent_name"], "agent")

    def test_failed_verification_can_retry_without_exposing_key(self):
        output = io.StringIO()
        answers = "https://gateway.example\nagent\ny\ny\nhttps://gateway.example\nagent\ny\n"
        with patch.object(setup.getpass, "getpass", side_effect=["old-private-key", "new-private-key"]), \
                patch.object(setup, "configure", side_effect=[ValueError("Key cannot access traces."), None]) as configure:
            self.assertTrue(setup.connect(io.StringIO(answers), output))
        self.assertEqual(configure.call_count, 2)
        self.assertEqual(configure.call_args.args[0]["api_key"], "new-private-key")
        self.assertNotIn("private-key", output.getvalue())

    def test_hidden_input_failure_does_not_fall_back_to_visible_input(self):
        with patch.object(setup.getpass, "getpass", side_effect=getpass.GetPassWarning("cannot hide")), \
                patch.object(setup, "configure") as configure:
            with self.assertRaisesRegex(ValueError, "cannot hide"):
                setup.connect(io.StringIO("https://gateway.example\n"), io.StringIO())
        configure.assert_not_called()

    def test_eof_does_not_configure(self):
        with patch.object(setup, "configure") as configure:
            with self.assertRaises(EOFError):
                setup.connect(io.StringIO(""), io.StringIO())
        configure.assert_not_called()

    def test_recording_only_starts_after_helper_is_ready(self):
        state.save_config({})
        def connect(*_):
            state.save_config({"gateway": "https://gateway.example", "enabled": False})
            return True
        def helper():
            self.assertFalse(state.config()["enabled"])
            return 18734
        with patch.object(setup, "open", create=True, side_effect=[io.StringIO(), io.StringIO()]), \
                patch.object(setup, "connect", side_effect=connect), \
                patch.object(setup, "start_helper", side_effect=helper), \
                patch.object(setup.webbrowser, "open") as browser, contextlib.redirect_stdout(io.StringIO()):
            setup.terminal_setup()
        self.assertTrue(state.config()["enabled"])
        browser.assert_not_called()

    def test_helper_failure_leaves_fresh_connection_paused(self):
        state.save_config({})
        def connect(*_):
            state.save_config({"gateway": "https://gateway.example", "enabled": False})
            return True
        with patch.object(setup, "open", create=True, side_effect=[io.StringIO(), io.StringIO()]), \
                patch.object(setup, "connect", side_effect=connect), \
                patch.object(setup, "start_helper", side_effect=ValueError("failed to start")):
            with self.assertRaisesRegex(ValueError, "failed to start"):
                setup.terminal_setup()
        self.assertFalse(state.config()["enabled"])

    def test_cancel_does_not_start_helper(self):
        state.save_config({})
        with patch.object(setup, "open", create=True, side_effect=[io.StringIO(), io.StringIO()]), \
                patch.object(setup, "connect", return_value=False), \
                patch.object(setup, "start_helper") as helper:
            setup.terminal_setup()
        helper.assert_not_called()
        self.assertFalse(state.config().get("enabled"))

    def test_reinstall_after_key_removal_prompts_for_a_connection(self):
        settings = state.config()
        settings.pop("api_key")
        state.save_config(settings)
        with patch.object(setup, "open", create=True, side_effect=[io.StringIO(), io.StringIO()]), \
                patch.object(setup, "connect", return_value=False) as connect, \
                patch.object(setup, "start_helper") as helper:
            setup.terminal_setup()
        connect.assert_called_once()
        helper.assert_not_called()

    def test_missing_terminal_has_actionable_error_without_reading_stdin(self):
        state.save_config({})
        with patch.object(setup, "open", create=True, side_effect=OSError("no tty")), \
                patch.object(setup, "connect") as connect:
            with self.assertRaisesRegex(ValueError, "own Terminal"):
                setup.terminal_setup()
        connect.assert_not_called()

    def test_updates_preserve_recording_state_and_skip_prompts_and_browser(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                state.save_config({**state.config(), "enabled": enabled})
                with patch.dict(os.environ, {"LENS_CODEX_PLUGIN_ROOT": str(self.root)}), \
                        patch.object(setup, "install_plugin_hooks") as hooks, \
                        patch.object(setup, "start_helper", return_value=18734), \
                        patch.object(setup, "open", create=True) as prompt, \
                        patch.object(setup.webbrowser, "open") as browser, \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    setup.terminal_setup()
                hooks.assert_called_once()
                prompt.assert_not_called()
                browser.assert_not_called()
                self.assertEqual(state.config()["enabled"], enabled)
                self.assertNotIn(state.config()["api_key"], output.getvalue())
                if not enabled:
                    self.assertIn("still paused", output.getvalue())

    def test_browser_is_explicit_and_preserves_pause(self):
        state.set_enabled(False)
        with patch.object(setup, "refresh_plugin"), patch.object(setup, "start_helper", return_value=18734), \
                patch.object(setup.webbrowser, "open") as browser, contextlib.redirect_stdout(io.StringIO()):
            setup.browser_setup()
        browser.assert_called_once_with("http://127.0.0.1:18734/")
        self.assertFalse(state.config()["enabled"])

    def test_real_terminal_hides_key_even_when_stdin_is_not_interactive(self):
        state.save_config({})
        code = '''
import fcntl, os, termios
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
fd = os.open(os.devnull, os.O_RDONLY)
os.dup2(fd, 0)
os.close(fd)
from lens_codex import setup, state
setup.configure = lambda value, **kwargs: state.save_config({**value, **kwargs})
setup.start_helper = lambda: 18734
setup.terminal_setup()
'''
        master, slave = os.openpty()
        process = subprocess.Popen([sys.executable, "-c", code], stdin=slave, stdout=slave, stderr=slave,
                                   start_new_session=True, cwd=Path(__file__).resolve().parent.parent)
        os.close(slave)
        output = b""
        try:
            for prompt, answer in [(b"Gateway URL: ", b"https://gateway.example\n"),
                                   (b"(hidden): ", b"secret-should-not-echo\n"),
                                   (b"Agent name [codex]: ", b"desktop-demo\n"),
                                   (b"[y/N]: ", b"yes\n")]:
                deadline = time.monotonic() + 10
                while prompt not in output and time.monotonic() < deadline:
                    if select.select([master], [], [], 0.1)[0]:
                        output += os.read(master, 65536)
                self.assertIn(prompt, output)
                os.write(master, answer)
            # Drain output while the child exits; macOS can wait for the terminal to drain.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if not select.select([master], [], [], 0.1)[0]:
                    if process.poll() is not None:
                        break
                    continue
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break  # Linux PTYs report EIO at EOF.
                if not chunk:
                    break
                output += chunk
            process.wait(timeout=3)
            self.assertEqual(process.returncode, 0, output.decode(errors="replace"))
            self.assertNotIn(b"secret-should-not-echo", output)
            self.assertEqual(state.config()["api_key"], "secret-should-not-echo")
            self.assertEqual(state.config()["agent_name"], "desktop-demo")
            self.assertTrue(state.config()["enabled"])
        finally:
            os.close(master)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
