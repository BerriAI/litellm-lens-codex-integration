"""Immutable completed-turn batches sharing a stable session trace ID."""
from __future__ import annotations

import hashlib
import json


def stable_id(*parts: str, size: int = 16) -> str:
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:size * 2]


def attribute(key: str, value: object) -> dict:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": str(value)}}


def attrs(values: dict) -> list[dict]:
    return [attribute(key, value) for key, value in values.items() if value is not None]


def span(trace_id: str, span_id: str, name: str, start: float, end: float, values: dict,
         parent: str | None = None, error: str | None = None) -> dict:
    result = {"traceId": trace_id, "spanId": span_id, "name": name, "kind": 1,
              "startTimeUnixNano": str(int(start * 1e9)), "endTimeUnixNano": str(int(max(start, end) * 1e9)),
              "attributes": attrs(values), "status": {"code": 2 if error else 1}}
    if parent:
        result["parentSpanId"] = parent
    if error:
        result["status"]["message"] = error
    return result


def messages(role: str, value: object) -> str:
    return json.dumps([{"role": role, "content": value}], ensure_ascii=False)


def build_trace(turn: dict, rows: list[dict], settings: dict, usage: dict | None) -> dict:
    session_id, turn_id = turn["session"], turn["turn"]
    trace_id = stable_id("codex-session", settings["installation_id"], session_id)
    root_id = stable_id("codex-turn", session_id, turn_id, size=8)
    events = [(row["received"], json.loads(row["data"])) for row in rows]
    prompts = [d.get("prompt", "") for _, d in events if d["hook_event_name"] == "UserPromptSubmit"]
    stop = next((d for _, d in reversed(events) if d["hook_event_name"] == "Stop"), {})
    interrupted = any(d["hook_event_name"] == "Interrupt" for _, d in events) or not stop
    name = settings.get("agent_name", "codex")
    values = {
        "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": name,
        "gen_ai.conversation.id": session_id, "codex.session.id": session_id,
        "codex.turn.id": turn_id, "codex.capture.source": "lifecycle_hooks",
        "codex.capture.scope": "user prompts, final replies, local tool calls",
        "codex.usage.known": usage is not None,
        "gen_ai.input.messages": json.dumps([{ "role": "user", "content": prompt} for prompt in prompts], ensure_ascii=False),
        "gen_ai.output.messages": messages("assistant", stop.get("last_assistant_message", "")),
    }
    model = next((d["model"] for _, d in events if d.get("model")), None)
    if model:
        values["gen_ai.request.model"] = model
    if usage is not None:
        values.update({"gen_ai.usage.input_tokens": usage["input_tokens"],
                       "gen_ai.usage.output_tokens": usage["output_tokens"],
                       "gen_ai.usage.cache_read.input_tokens": usage["cached_input_tokens"],
                       "codex.usage.reasoning_output_tokens": usage["reasoning_output_tokens"],
                       "codex.usage.source": "codex_transcript_turn_totals"})
    result = [span(trace_id, root_id, name, turn["started"], turn["ended"], values,
                   error="Turn interrupted before a final reply." if interrupted else None)]
    calls: dict[str, dict] = {}
    for timestamp, data in events:
        kind = data["hook_event_name"]
        if kind not in {"PreToolUse", "PostToolUse"}:
            continue
        identity = data.get("tool_use_id")
        if not identity:
            continue
        call = calls.setdefault(identity, {"start": timestamp, "input": data.get("tool_input"),
                                           "name": data.get("tool_name", "Tool")})
        if kind == "PostToolUse":
            call.update(end=timestamp, output=data.get("tool_response"), finished=True)
    for identity, call in calls.items():
        tool_values = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": call["name"],
                       "gen_ai.tool.call.id": identity, "gen_ai.agent.name": name,
                       "gen_ai.tool.call.arguments": json.dumps(call["input"], ensure_ascii=False),
                       "gen_ai.tool.call.result": call.get("output") if isinstance(call.get("output"), str)
                       else json.dumps(call.get("output"), ensure_ascii=False),
                       "codex.tool.completed": bool(call.get("finished"))}
        result.append(span(trace_id, stable_id("codex-tool", session_id, turn_id, identity, size=8),
                           call["name"], call["start"], call.get("end", turn["ended"]), tool_values, root_id,
                           None if call.get("finished") else "Tool did not complete before the turn ended."))
    return {"resourceSpans": [{"resource": {"attributes": attrs({"service.name": name,
             "telemetry.sdk.name": "litellm-lens-codex", "telemetry.sdk.version": "0.1.0"})},
             "scopeSpans": [{"scope": {"name": "berriai.lens.codex", "version": "0.1.0"}, "spans": result}]}]}
