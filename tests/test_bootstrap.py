import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from lens_codex import bootstrap

ROOT = Path(__file__).resolve().parent.parent


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lens installer ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.entry = self.root / "plugin with spaces/scripts/plugin_entry.py"
        self.entry.parent.mkdir(parents=True)
        self.entry.write_text("# fixture")
        self.installed = Mock(stdout=json.dumps({"installedPath": str(self.entry.parents[1])}))

    def test_desktop_without_cli(self):
        expected = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
        with patch.object(Path, "is_file", lambda path: str(path) == expected), \
                patch.object(bootstrap.shutil, "which", return_value=None):
            self.assertEqual(bootstrap.engine(), expected)

    def test_user_applications_install(self):
        expected = self.root / "Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
        with patch.object(Path, "is_file", lambda path: path == expected), \
                patch.object(Path, "home", return_value=self.root):
            self.assertEqual(bootstrap.engine(), str(expected))

    def test_cli_without_desktop(self):
        with patch.object(Path, "is_file", return_value=False), \
                patch.object(bootstrap.shutil, "which", return_value="/bin/codex"):
            self.assertEqual(bootstrap.engine(), "/bin/codex")

    def test_missing_codex(self):
        with patch.object(Path, "is_file", return_value=False), \
                patch.object(bootstrap.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValueError, "Install Codex"):
                bootstrap.engine()

    def test_install_refreshes_and_opens_cached_plugin_using_checked_python(self):
        with patch.object(bootstrap.sys, "platform", "darwin"), \
                patch.object(bootstrap, "engine", return_value="/Codex With Spaces/codex"), \
                patch.object(bootstrap.subprocess, "run", side_effect=[Mock(), Mock(), self.installed, Mock()]) as run:
            bootstrap.main()
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands, [
            ["/Codex With Spaces/codex", "plugin", "marketplace", "add",
             "BerriAI/litellm-lens-codex-integration", "--json"],
            ["/Codex With Spaces/codex", "plugin", "marketplace", "upgrade", "berriai-lens"],
            ["/Codex With Spaces/codex", "plugin", "add", "litellm-lens@berriai-lens", "--json"],
            [sys.executable, str(self.entry), "setup"],
        ])

    def test_failed_install_never_launches_setup(self):
        for index in range(3):
            with self.subTest(index=index), patch.object(bootstrap.sys, "platform", "darwin"), \
                    patch.object(bootstrap, "engine", return_value="codex"), \
                    patch.object(bootstrap.subprocess, "run", side_effect=[Mock()] * index + [
                        subprocess.CalledProcessError(1, "codex", stderr="private diagnostic")]) as run:
                with self.assertRaisesRegex(SystemExit, "could not install") as error:
                    bootstrap.main()
                self.assertNotIn("private diagnostic", str(error.exception))
                self.assertEqual(run.call_count, index + 1)

    def test_invalid_install_result_never_launches_setup(self):
        for value in ["invalid json", "{}", "null", '{"installedPath": null}', '{"installedPath": "relative"}']:
            with self.subTest(value=value), patch.object(bootstrap.sys, "platform", "darwin"), \
                    patch.object(bootstrap, "engine", return_value="codex"), \
                    patch.object(bootstrap.subprocess, "run", side_effect=[Mock(), Mock(), Mock(stdout=value)]) as run:
                with self.assertRaisesRegex(SystemExit, "could not install"):
                    bootstrap.main()
                self.assertEqual(run.call_count, 3)

    def test_missing_plugin_entry_never_launches_setup(self):
        self.entry.unlink()
        with patch.object(bootstrap.sys, "platform", "darwin"), \
                patch.object(bootstrap, "engine", return_value="codex"), \
                patch.object(bootstrap.subprocess, "run", side_effect=[Mock(), Mock(), self.installed]) as run:
            with self.assertRaisesRegex(SystemExit, "incomplete"):
                bootstrap.main()
            self.assertEqual(run.call_count, 3)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lens download ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        self.command("uname", "printf 'Darwin\\n'")
        self.marker = self.root / "executed"
        self.fixture = self.root / "fixture.py"
        self.fixture.write_text('import os\nfrom pathlib import Path\n'
                                'Path(os.environ["LENS_TEST_MARKER"]).write_text("executed")\n')
        self.env = {**os.environ, "PATH": f"{self.bin}:/usr/bin:/bin", "TMPDIR": str(self.root),
                    "LENS_TEST_MARKER": str(self.marker), "LENS_TEST_FIXTURE": str(self.fixture)}

    def command(self, name, script):
        path = self.bin / name
        path.write_text("#!/bin/bash\n" + script + "\n")
        path.chmod(0o755)

    def run_installer(self, source=None):
        return subprocess.run(["/bin/bash"], input=source or (ROOT / "install.sh").read_text(),
                              cwd=self.root, env=self.env, capture_output=True, text=True, timeout=10)

    def test_piped_install_runs_outside_repo_and_cleans_up(self):
        self.command("curl", '/bin/cp "$LENS_TEST_FIXTURE" "${@: -1}"')
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.marker.exists())
        self.assertEqual(list(self.root.glob("lens-install.*")), [])

    def test_failed_download_does_not_execute_partial_file(self):
        self.command("curl", '/bin/cp "$LENS_TEST_FIXTURE" "${@: -1}"\nexit 22')
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not download", result.stderr)
        self.assertFalse(self.marker.exists())
        self.assertEqual(list(self.root.glob("lens-install.*")), [])

    def test_interrupted_initial_download_cannot_start_install(self):
        self.command("curl", 'touch "$LENS_TEST_MARKER"')
        source = (ROOT / "install.sh").read_text().split('  "$lens_python"')[0]
        self.assertNotEqual(self.run_installer(source).returncode, 0)
        self.assertFalse(self.marker.exists())

    def test_unsupported_platform_stops_before_download(self):
        self.command("uname", "printf 'Linux\\n'")
        self.command("curl", 'touch "$LENS_TEST_MARKER"')
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("macOS", result.stderr)
        self.assertFalse(self.marker.exists())
