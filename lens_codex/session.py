"""Read visible session items within one hook-authorized turn."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Iterator

from .content import encode

MAX_RECORD_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class Entry:
    identity: str
    start: float
    end: float
    role: str = "assistant"
    text: str = ""
    model: str | None = None
    tool: str | None = None
    arguments: object = None
    result: object = None
    error: str | None = None


@dataclass(frozen=True)
class Recording:
    entries: tuple[Entry, ...] = ()
    complete: bool = False
    warning: str | None = None
    ended: float | None = None
    error: str | None = None


def records(path: str, offset: int = 0) -> Iterator[dict]:
    with Path(path).open("rb") as stream:
        beginning = max(0, offset - MAX_RECORD_BYTES - 256 * 1024)
        stream.seek(beginning)
        if beginning:
            stream.readline(MAX_RECORD_BYTES)
        while raw := stream.readline(MAX_RECORD_BYTES + 1):
            after_offset = stream.tell() > offset
            if len(raw) > MAX_RECORD_BYTES:
                raise ValueError("A transcript record exceeds the supported size.")
            try:
                record = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(record, dict):
                yield {**record, "_lens_after_offset": after_offset}


def timestamp(value: object, fallback: float = 0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return fallback


def visible_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type", "")).lower()
        if kind in {"text", "input_text", "output_text"} and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif kind in {"image", "input_image", "image_url", "localimage", "local_image", "file", "document"}:
            parts.append("[Attachment omitted by Lens]")
    return "\n".join(parts)


def codex_item(item: dict, start: float, end: float, model: str | None) -> Entry | None:
    kind, identity = item.get("type"), str(item.get("id", ""))
    common = dict(identity=identity, start=start, end=end, model=model)
    if kind in {"UserMessage", "AgentMessage"}:
        return Entry(**common, role="user" if kind == "UserMessage" else "assistant",
                     text=visible_text(item.get("content")))
    if kind == "Reasoning":
        return None
    if kind == "SubAgentActivity":
        return Entry(**common, role="system", text=f"Agent {item.get('agent_path', 'subagent')} {item.get('kind', 'updated')}")
    if kind == "CommandExecution":
        command = item.get("command", [])
        command = command[-1] if isinstance(command, list) and len(command) >= 3 and command[-2] in {"-lc", "-c"} else command
        code = item.get("exit_code")
        error = f"Command exited with code {code}." if isinstance(code, int) and code != 0 else None
        if not error and item.get("status") in {"failed", "declined"}:
            error = "Command " + item["status"] + "."
        return Entry(**common, tool="Bash", arguments={"command": command},
                     result={"output": item.get("aggregated_output", ""), "exit_code": code}, error=error)
    if kind == "FileChange":
        return Entry(**common, tool="apply_patch", arguments={"changes": item.get("changes", [])},
                     result=item.get("status", "unknown"),
                     error="File change failed." if item.get("status") in {"failed", "declined"} else None)
    if kind == "McpToolCall":
        result, error = item.get("result"), item.get("error")
        failed = item.get("status") == "failed" or isinstance(result, dict) and result.get("isError") is True
        return Entry(**common, tool=str(item.get("tool", "MCP tool")), arguments=item.get("arguments"),
                     result=result, error=encode(error) if error else ("Tool returned an error." if failed else None))
    if kind == "CollabAgentToolCall":
        return Entry(**common, tool=str(item.get("tool", "Agent")),
                     arguments={k: item[k] for k in ("prompt", "receiver_thread_ids") if k in item},
                     result=item.get("agents_states", {}),
                     error="Agent operation failed." if item.get("status") == "failed" else None)
    if kind == "WebSearch":
        return Entry(**common, tool="Web search", arguments=item.get("action", {"query": item.get("query")}),
                     result=item.get("status", "completed"))
    if kind == "ContextCompaction":
        return Entry(**common, role="system", text="Context compacted")
    if kind in {"ImageView", "ImageGeneration"}:
        return Entry(**common, text="[Image omitted by Lens]")
    return Entry(**common, role="system", text=f"[Session item not supported by Lens: {kind}]")


def codex_recording(path: str, offset: int, turn_id: str) -> Recording:
    current, model, complete = None, None, False
    ended, error = None, None
    entries: dict[str, Entry] = {}
    fallback: dict[str, Entry] = {}
    for index, record in enumerate(records(path, offset)):
        payload = record.get("payload", {})
        if not isinstance(payload, dict):
            continue
        kind, record_type = payload.get("type"), record.get("type")
        if record_type == "turn_context":
            current, model = payload.get("turn_id"), payload.get("model")
        if record_type == "event_msg" and kind == "task_started":
            current = payload.get("turn_id")
        if current != turn_id:
            continue
        at = timestamp(record.get("timestamp"))
        if record_type == "event_msg" and kind == "item_completed" and payload.get("turn_id") == turn_id:
            item = payload.get("item")
            if not isinstance(item, dict):
                continue
            start = timestamp(payload.get("started_at_ms"), at * 1000) / 1000
            end = timestamp(payload.get("completed_at_ms"), at * 1000) / 1000
            entry = codex_item(item, start, end, model)
            if entry:
                entries[entry.identity or str(index)] = entry
        if record_type == "response_item" and kind == "message" and payload.get("role") == "assistant" and payload.get("channel") != "analysis":
            identity = str(payload.get("id") or index)
            fallback[identity] = Entry(identity, at, at, text=visible_text(payload.get("content")), model=model)
        if record_type == "event_msg" and kind == "error":
            error = str(payload.get("message") or "Turn failed.")
        if record_type == "event_msg" and kind in {"task_complete", "turn_aborted"} and payload.get("turn_id") == turn_id:
            complete, ended = True, at
            if kind == "turn_aborted":
                error = "Turn interrupted before a final reply."
            current = None
    for identity, entry in fallback.items():
        entries.setdefault(identity, entry)
    return Recording(tuple(sorted(entries.values(), key=lambda entry: entry.start)), complete, ended=ended, error=error)


def read_recording(path: str | None, offset: int, turn_id: str) -> Recording:
    if not path:
        return Recording(warning="Transcript unavailable; only lifecycle hook content was captured.")
    try:
        result = codex_recording(path, offset, turn_id)
        return result if result.entries else Recording(complete=result.complete, ended=result.ended, error=result.error, warning="No supported transcript items were found.")
    except (OSError, ValueError, TypeError, RecursionError):
        return Recording(warning="Transcript could not be read; only lifecycle hook content was captured.")


def codex_parent(path: Path, turn_id: str, offset: int = 0) -> tuple[str | None, str | None, str | None]:
    first = next(records(str(path)), {})
    payload = first.get("payload", {})
    source = payload.get("source", {}) if isinstance(payload, dict) else {}
    subagent = source.get("subagent", {}) if isinstance(source, dict) else {}
    spawn = subagent.get("thread_spawn", {}) if isinstance(subagent, dict) else {}
    spawn = spawn if isinstance(spawn, dict) else {}
    parent, name = spawn.get("parent_thread_id"), spawn.get("agent_path")
    for record in records(str(path), offset):
        payload = record.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if record.get("type") == "event_msg" and payload.get("type") == "task_started" and payload.get("turn_id") == turn_id:
            return parent, payload.get("root_turn_id"), name
    return parent, None, name
