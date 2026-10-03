"""Opt-in live test. Sends only generated acceptance-test prompts and outputs.

LENS_GATEWAY, LENS_API_KEY, CODEX_AUTH_FILE must be set. Never reads normal chats.
Run with --engine /path/to/codex --output /private/test-directory.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from lens_codex import delivery, install, state

parser = argparse.ArgumentParser()
parser.add_argument("--engine", required=True)
parser.add_argument("--output", required=True)
parser.add_argument("--plugin", action="store_true", help="Install and exercise native plugin hooks")
parser.add_argument("--marketplace", default=str(Path(__file__).resolve().parent.parent))
args = parser.parse_args()
root = Path(args.output).resolve()
root.mkdir(parents=True, exist_ok=True, mode=0o700)
os.umask(0o077)
state_path, home, work = root / "integration", root / "codex", root / "work"
for folder in (home, work):
    folder.mkdir(exist_ok=True, mode=0o700)
shutil.copyfile(os.environ["CODEX_AUTH_FILE"], home / "auth.json")
(home / "auth.json").chmod(0o600)
(home / "config.toml").write_text('model="gpt-6-sol"\nmodel_reasoning_effort="low"\n[otel]\nexporter="none"\ntrace_exporter="none"\n')
os.environ["LENS_CODEX_HOME"] = str(state_path)
settings = {"enabled": True, "gateway": state.gateway_url(os.environ["LENS_GATEWAY"]),
            "api_key": os.environ["LENS_API_KEY"], "agent_name": "codex-integration-qa",
            "installation_id": str(uuid.uuid4()), "codex_home": str(home)}
state.save_config(settings)
delivery.verify(settings)
env = {**os.environ, "CODEX_HOME": str(home)}
env.pop("LENS_API_KEY", None)
env.pop("CODEX_AUTH_FILE", None)
if args.plugin:
    subprocess.run([args.engine, "plugin", "marketplace", "add", args.marketplace],
                   env=env, check=True, capture_output=True, timeout=120)
    result = subprocess.run([args.engine, "plugin", "add", "litellm-lens@berriai-lens", "--json"],
                            env=env, check=True, capture_output=True, text=True, timeout=120)
    plugin_root = Path(json.loads(result.stdout)["installedPath"])
    install.install_plugin_hooks(home, plugin_root, binary=args.engine)
    settings["capture_mode"] = "plugin"
    state.save_config(settings)
else:
    from lens_codex.codex import approve_installed_hooks
    install.copy_app()
    install.install_hooks(home)
    approve_installed_hooks(binary=args.engine)
prompt = ('This is an integration acceptance test. Run exactly this shell command: '
          'python3 -c \'print("LENS_BEGIN_" + "x" * 6000 + "_LENS_END")\'. '
          'Do not inspect files or call any other tools. Then reply exactly LENS_FIRST_TURN_DONE.')

def run(command, filename):
    with (root / (filename + ".jsonl")).open("w") as out, (root / (filename + ".stderr")).open("w") as err:
        subprocess.run(command, cwd=work, env=env, stdout=out, stderr=err, check=True, timeout=150)

run([args.engine, "exec", "--skip-git-repo-check", "--sandbox", "workspace-write",
     "--json", prompt], "first")
with state.database() as db:
    first = dict(db.execute("SELECT * FROM turns ORDER BY started LIMIT 1").fetchone())
    captured = [json.loads(r[0]) for r in db.execute("SELECT data FROM events WHERE kind='PostToolUse'")]
assert any("_LENS_END" in str(c.get("tool_response")) and len(str(c.get("tool_response"))) > 6000 for c in captured)
delivery.flush(time.time() + 20)
assert delivery.status()["counts"].get("sent") == 1, delivery.status()
run([args.engine, "exec", "resume", "--skip-git-repo-check", "--json",
     first["session"], "Without using tools, reply exactly LENS_SECOND_TURN_DONE."], "second")
delivery.flush(time.time() + 20)
summary = delivery.status()
assert summary["counts"].get("sent") == 2, summary
assert len({r["trace_id"] for r in summary["recent"]}) == 1
trace_id = summary["recent"][0]["trace_id"]
for _ in range(15):
    result = delivery.request(settings, "/v1/traces/" + trace_id)
    if len(result.get("spans", [])) >= 3:
        break
    time.sleep(1)
(root / "remote-trace.json").write_text(json.dumps(result, indent=2))
spans = result.get("spans", [])
assert len(spans) == 3, [(s.get("name"), s.get("type")) for s in spans]
assert "codex-integration-qa" in json.dumps(result.get("agents", [])), "Agent name missing"
tool = next(s for s in spans if s.get("type") == "tool")
detail = delivery.request(settings, "/v1/traces/" + trace_id + "/spans/" + tool["span_id"])
assert "_LENS_END" in str(detail.get("output")), "Tool output missing on gateway"
assert len(str(detail.get("output"))) > 6000, "Tool output truncated on gateway"
assert result["summary"]["input_tokens"] > 0, "Token totals missing on gateway"
assert all(r["warning"] is None for r in summary["recent"]), "Token usage was not captured"
# Reflushing must not append duplicate spans.
delivery.flush(time.time() + 20)
again = delivery.request(settings, "/v1/traces/" + trace_id)
assert len(again.get("spans", [])) == 3
report = {"engine": subprocess.check_output([args.engine, "--version"], text=True).strip(),
          "native_plugin": args.plugin, "hook_trust_bypassed": False,
          "session_id": first["session"], "trace_id": trace_id, "turns": 2, "spans": 3,
          "resumed_session_same_trace": True, "tool_output_over_6000_characters": True,
          "agent_name_verified": True, "token_usage_captured": True,
          "duplicate_flush_did_not_duplicate_spans": True}
(root / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
