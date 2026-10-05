"""Immutable completed-turn batches sharing a stable session trace ID."""
from __future__ import annotations

import hashlib
import json

from . import __version__
from .content import encode, event_content, excerpt
from .session import Recording
from .state import redact


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


def build_trace(turn: dict, rows: list[dict], settings: dict, usage: dict | None,
                recording: Recording | None = None) -> dict:
    session_id, turn_id = turn["session"], turn["turn"]
    client = "codex"
    trace_id = stable_id(client + "-session", settings["installation_id"], session_id)
    root_id = stable_id(client + "-turn", session_id, turn_id, size=8)
    events = [(row["received"], event_content(json.loads(row["data"]))) for row in rows]
    prompts = [d.get("prompt", "") for _, d in events if d["hook_event_name"] == "UserPromptSubmit"]
    stop = next((d for _, d in reversed(events) if d["hook_event_name"] in {"Stop", "SubagentStop"}), {})
    interrupted = any(d["hook_event_name"] == "Interrupt" for _, d in events) or (not stop and not (recording and recording.complete))
    name = turn.get("agent_name") or settings.get("agent_name", client)
    values = {
        "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": name,
        "gen_ai.agent.id": turn.get("agent_id") or session_id,
        "gen_ai.conversation.id": session_id, "codex.session.id": session_id,
        "codex.turn.id": turn_id, "codex.capture.source": "lifecycle_hooks",
        "codex.capture.scope": "user prompts, final replies, local tool calls",
        "codex.usage.known": usage is not None,
        "gen_ai.input.messages": messages("user", excerpt("\n\n".join(
            p if isinstance(p, str) else encode(p) for p in prompts))) if len(encode(prompts).encode()) > 128 * 1024
            else json.dumps([{ "role": "user", "content": prompt} for prompt in prompts], ensure_ascii=False),
        "gen_ai.output.messages": messages("assistant", stop.get("last_assistant_message", "")),
    }
    model = next((d["model"] for _, d in events if d.get("model")), None)
    model = next((entry.model for entry in recording.entries if entry.model), model) if recording else model
    if model:
        values["gen_ai.request.model"] = model
    if usage is not None:
        values.update({"gen_ai.usage.input_tokens": usage["input_tokens"],
                       "gen_ai.usage.output_tokens": usage["output_tokens"],
                       "gen_ai.usage.cache_read.input_tokens": usage["cached_input_tokens"],
                       "codex.usage.reasoning_output_tokens": usage["reasoning_output_tokens"],
                       "codex.usage.source": "codex_transcript_turn_totals"})
    notes = {key: sum(d.get("capture_notes", {}).get(key, 0) for _, d in events)
             for key in {key for _, d in events for key in d.get("capture_notes", {})}}
    values.update({"codex.capture." + key: value for key, value in notes.items()})
    if recording and (recording.warning or not recording.complete):
        values["lens.capture.warning"] = recording.warning or "The session ended without a recorded completion. Content may be incomplete."
    if recording and recording.entries:
        if any(entry.role == "user" and not entry.tool for entry in recording.entries):
            values["lens.capture.messages_separate"] = True
        if any(entry.role == "assistant" and not entry.tool for entry in recording.entries):
            values["gen_ai.output.messages"] = "[]"
        values["lens.capture.source"] = "session_transcript"
        values["lens.capture.complete"] = recording.complete
        values["codex.capture.scope"] = "visible messages and tools; media and hidden reasoning excluded"
    parent_id = stable_id(client + "-turn", session_id, turn["parent_turn"], size=8) if turn.get("parent_turn") else None
    failure = recording.error if recording and recording.error else ("Turn interrupted before a final reply." if interrupted else None)
    result = [span(trace_id, root_id, name, turn["started"], turn["ended"], values,
                   parent=parent_id,
                   error=excerpt(str(redact(failure, settings.get("api_key", "")))) if failure else None)]
    calls: dict[str, dict] = {}
    for timestamp, data in events:
        kind = data["hook_event_name"]
        if kind not in {"PreToolUse", "PostToolUse", "PostToolUseFailure"}:
            continue
        identity = data.get("tool_use_id")
        if not identity:
            continue
        call = calls.setdefault(identity, {"start": timestamp, "input": data.get("tool_input"),
                                           "name": data.get("tool_name", "Tool")})
        if kind in {"PostToolUse", "PostToolUseFailure"}:
            call.update(end=timestamp, output=data.get("tool_response", data.get("error")), finished=True,
                        error=data.get("error") if kind == "PostToolUseFailure" else None)
    recorded_tools = {entry.identity for entry in recording.entries if entry.tool} if recording else set()
    for identity, call in calls.items():
        if identity in recorded_tools:
            continue
        tool_values = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": call["name"],
                       "gen_ai.tool.call.id": identity, "gen_ai.agent.name": name,
                       "gen_ai.tool.call.arguments": json.dumps(call["input"], ensure_ascii=False),
                       "gen_ai.tool.call.result": call.get("output") if isinstance(call.get("output"), str)
                       else json.dumps(call.get("output"), ensure_ascii=False),
                       "codex.tool.completed": bool(call.get("finished"))}
        result.append(span(trace_id, stable_id("codex-tool", session_id, turn_id, identity, size=8),
                           call["name"], call["start"], call.get("end", turn["ended"]), tool_values, root_id,
                           call.get("error") if call.get("finished") else "Tool did not complete before the turn ended."))
    for entry in recording.entries if recording else ():
        content = event_content(redact({"prompt": entry.text, "tool_input": entry.arguments,
                                        "tool_response": entry.result}, settings.get("api_key", "")))
        entry_values = {"gen_ai.agent.name": name, "gen_ai.agent.id": turn.get("agent_id") or session_id,
                        "gen_ai.request.model": entry.model, "lens.capture.source": "session_transcript"}
        if entry.tool:
            entry_values.update({"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": entry.tool,
                                 "gen_ai.tool.call.id": entry.identity,
                                 "gen_ai.tool.call.arguments": encode(content["tool_input"]),
                                 "gen_ai.tool.call.result": content["tool_response"] if isinstance(content["tool_response"], str)
                                 else encode(content["tool_response"])})
        else:
            entry_values.update({"openinference.span.kind": "CHAIN", "lens.message.role": entry.role,
                                 "agent.name": name, "llm.model_name": entry.model,
                                 "input.value" if entry.role == "user" else "output.value":
                                     messages(entry.role, content["prompt"])})
        result.append(span(trace_id, stable_id(client + "-item", session_id, turn_id, entry.identity, size=8),
                           entry.tool or entry.role.capitalize(), entry.start, entry.end, entry_values, root_id,
                           excerpt(str(redact(entry.error, settings.get("api_key", "")))) if entry.error else None))
    return {"resourceSpans": [{"resource": {"attributes": attrs({"service.name": settings.get("agent_name", client),
             "telemetry.sdk.name": "litellm-lens-" + client, "telemetry.sdk.version": __version__,
             **settings.get("resource_attributes", {})})},
             "scopeSpans": [{"scope": {"name": "berriai.lens." + client, "version": __version__}, "spans": result}]}]}
