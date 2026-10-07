"""Test the desktop's app-server transport, trusted hooks, and cancellation.

Run after live_smoke.py; pass its private output directory. Uses only generated test content.
"""
import argparse
import json
import os
from pathlib import Path
import time

from lens_codex.codex import Client, approve_installed_hooks, engine
from lens_codex.delivery import flush, request, status
from lens_codex.install import copy_app
from lens_codex.state import config, state_dir

p = argparse.ArgumentParser()
p.add_argument("output")
args = p.parse_args()
root = Path(args.output).resolve()
os.environ["LENS_CODEX_HOME"] = str(root / "integration")
home = root / "codex"
copy_app()
approve_installed_hooks()
with Client(engine(), home) as client:
    thread = client.call("thread/start", {"cwd": str(root / "work"), "model": "gpt-6.1-sol",
        "approvalPolicy": "never", "sandbox": "workspace-write", "threadSource": "desktop"})["thread"]["id"]
    for index, prompt in enumerate((
        "This is a telemetry acceptance test. Run the shell command python3 -c 'print(\"DESKTOP_TOOL_OK\")', then reply DESKTOP_FIRST_DONE.",
        "Without tools, reply DESKTOP_SECOND_DONE.",
        "This is a cancellation test. Run python3 -c 'import time; time.sleep(30)' and then reply DONE.",
    )):
        started = client.call("turn/start", {"threadId": thread, "input": [{"type": "text", "text": prompt}], "effort": "low"})
        turn_id = started["turn"]["id"]
        interrupted = False
        while True:
            message = client.read(timeout=120)
            method, params = message.get("method"), message.get("params", {})
            if index == 2 and method == "item/started" and params.get("item", {}).get("type") == "commandExecution":
                client.call("turn/interrupt", {"threadId": thread, "turnId": turn_id})
                interrupted = True
                # A fast completion may arrive while waiting for the interrupt acknowledgement.
                if any(m.get("method") == "turn/completed" and m.get("params", {}).get("turn", {}).get("id") == turn_id
                       for m in client.notifications):
                    break
            if method == "turn/completed" and params.get("turn", {}).get("id") == turn_id:
                break
        if index == 2:
            assert interrupted, "The cancellation test did not interrupt a running command"
        flush(time.time() + 20)
results = [r for r in status()["recent"] if r["session"] == thread]
assert len(results) == 3, results
assert all(r["status"] == "sent" for r in results), results
assert len({r["trace_id"] for r in results}) == 1
trace_id = results[0]["trace_id"]
reader = {"gateway": os.environ["LENS_READ_GATEWAY"], "api_key": os.environ["LENS_READ_API_KEY"]}
remote = request(reader, "/v1/traces/" + trace_id)
assert remote["summary"]["error_count"] > 0, "Cancelled turn was not marked interrupted"
report = {"transport": "desktop app-server", "session_id": thread, "trace_id": trace_id,
          "turns": 3, "trusted_hooks_without_bypass": True, "cancelled_turn_marked": True}
(root / "appserver-report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
