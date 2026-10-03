"""Split OTLP by actual serialized bytes, preserving every span's identity."""
from __future__ import annotations

import copy

from .content import encode, excerpt

MAX_BATCH_BYTES = 512 * 1024
CONTENT_FIELDS = {"gen_ai.input.messages", "gen_ai.output.messages",
                  "gen_ai.tool.call.arguments", "gen_ai.tool.call.result"}


def spans(payload: dict) -> list[dict]:
    return payload["resourceSpans"][0]["scopeSpans"][0]["spans"]


def envelope(payload: dict, items: list[dict]) -> dict:
    resource = payload["resourceSpans"][0]
    scope = resource["scopeSpans"][0]
    return {"resourceSpans": [{**resource, "scopeSpans": [{**scope, "spans": items}]}]}


def split(payload: dict, limit: int = MAX_BATCH_BYTES) -> list[dict]:
    result, current = [], []
    for original in spans(payload):
        item = copy.deepcopy(original)
        while len(encode(envelope(payload, [item])).encode()) > limit:
            candidates = [a for a in item["attributes"] if a["key"] in CONTENT_FIELDS
                          and len(a["value"].get("stringValue", "").encode()) > 1024]
            if not candidates:
                raise ValueError("A trace span exceeds the upload limit even with shortened content.")
            attr = max(candidates, key=lambda a: len(a["value"]["stringValue"].encode()))
            text = attr["value"]["stringValue"]
            if attr["key"] in {"gen_ai.input.messages", "gen_ai.output.messages"}:
                # Keep the message attribute valid JSON even at the wire limit.
                import json
                messages = json.loads(text)
                for message in messages:
                    content = message.get("content", "")
                    content = content if isinstance(content, str) else encode(content)
                    message["content"] = excerpt(content, max(512, len(content.encode()) // 2))
                attr["value"]["stringValue"] = encode(messages)
            else:
                attr["value"]["stringValue"] = excerpt(text, len(text.encode()) // 2)
            if not any(a["key"] == "codex.capture.wire_shortened" for a in item["attributes"]):
                item["attributes"].append({"key": "codex.capture.wire_shortened", "value": {"boolValue": True}})
        candidate = envelope(payload, [*current, item])
        if current and len(encode(candidate).encode()) > limit:
            result.append(envelope(payload, current))
            current = []
        current.append(item)
    if current:
        result.append(envelope(payload, current))
    return result
