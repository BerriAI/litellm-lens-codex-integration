import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Final

from lens_codex.codex import approve_installed_hooks
from lens_codex.state import EVENTS


class HookApprovalTests(unittest.TestCase):
    def test_approves_the_installed_event_set_and_rejects_incomplete_installs(self) -> None:
        for missing in (False, True):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                home: Final = Path(directory)
                events: Final = tuple(EVENTS)[:-1] if missing else tuple(EVENTS)
                hooks: Final = [
                    {
                        "key": event,
                        "currentHash": "hash-" + event,
                        "handlerType": "command",
                        "command": "capture",
                        "source": "user",
                        "sourcePath": str(home / "hooks.json"),
                    }
                    for event in events
                ]
                (home / "listed.json").write_text(json.dumps({"data": [{"hooks": hooks}]}))
                binary: Final = home / "codex-test-server"
                binary.write_text(
                    f"#!{sys.executable}\n"
                    "import json, os, sys\n"
                    "from pathlib import Path\n"
                    "home = Path(os.environ['CODEX_HOME'])\n"
                    "for line in sys.stdin:\n"
                    "    request = json.loads(line)\n"
                    "    if 'id' not in request: continue\n"
                    "    result = {}\n"
                    "    if request['method'] == 'hooks/list':\n"
                    "        result = json.loads((home / 'listed.json').read_text())\n"
                    "    if request['method'] == 'config/batchWrite':\n"
                    "        (home / 'written.json').write_text(json.dumps(request['params']))\n"
                    "    print(json.dumps({'id': request['id'], 'result': result}), flush=True)\n"
                )
                binary.chmod(0o700)
                manifest: Final = {"codex_home": str(home), "command": "capture"}
                if missing:
                    with self.assertRaisesRegex(ValueError, "all Lens hooks"):
                        approve_installed_hooks(str(binary), manifest)
                    self.assertFalse((home / "written.json").exists())
                else:
                    approve_installed_hooks(str(binary), manifest)
                    written: Final = json.loads((home / "written.json").read_text())
                    self.assertEqual(written["filePath"], str(home / "config.toml"))
                    self.assertEqual(
                        {edit["keyPath"] for edit in written["edits"]},
                        {"hooks.state." + json.dumps(event) for event in EVENTS},
                    )
                    self.assertTrue(all(edit["value"]["enabled"] for edit in written["edits"]))
