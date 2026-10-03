"""Text-only, bounded capture. Never put transport metadata or media in the outbox."""
from __future__ import annotations

import json
import re

# A generous text excerpt, not a screenshot or an entire downloaded document.
MAX_CONTENT_BYTES = 128 * 1024
MAX_NODES = 20000
MEDIA = {"image", "image_url", "input_image", "audio", "input_audio", "video", "file"}
DATA_URI = re.compile(r"data:[\w.+/-]+(?:;[\w=.+-]+)*;base64,[A-Za-z0-9+/=\s]+", re.I)


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def excerpt(text: str, limit: int = MAX_CONTENT_BYTES) -> str:
    raw = text.encode()
    if len(raw) <= limit:
        return text
    marker = f"\n[Content shortened by Lens: {len(raw)} UTF-8 bytes; showing beginning and end.]\n"
    budget = max(0, limit - len(marker.encode()))
    return (raw[:budget * 3 // 4].decode("utf-8", errors="ignore") + marker
            + raw[-(budget // 4):].decode("utf-8", errors="ignore"))


def normalize(value: object) -> tuple[object, dict]:
    notes: dict[str, int] = {}
    visited = 0

    def note(key: str):
        notes[key] = notes.get(key, 0) + 1

    def walk(item: object, depth: int = 0) -> object:
        nonlocal visited
        visited += 1
        if depth > 32 or visited > MAX_NODES:
            note("shortened")
            return "[Nested content omitted by Lens]"
        if isinstance(item, str):
            # Some tools wrap their MCP result in a JSON string.
            if item.lstrip().startswith(("{", "[")):
                try:
                    parsed = json.loads(item)
                except (ValueError, RecursionError):
                    pass
                else:
                    if isinstance(parsed, dict) and any(k in parsed for k in ("content", "structuredContent", "_meta")):
                        return walk(parsed, depth + 1)
            clean, count = DATA_URI.subn("[Media omitted by Lens]", item)
            if count:
                notes["media_omitted"] = notes.get("media_omitted", 0) + count
            return clean
        if isinstance(item, list):
            result = [walk(child, depth + 1) for child in item[:MAX_NODES]]
            if len(item) > MAX_NODES:
                note("shortened")
                result.append("[Additional items omitted by Lens]")
            return result
        if not isinstance(item, dict):
            return item
        if (isinstance(item.get("type"), str) and item["type"] in MEDIA) or "blob" in item:
            note("media_omitted")
            return "[Media omitted by Lens]"
        if item.get("type") == "resource" and isinstance(item.get("resource"), dict):
            return walk(item["resource"], depth + 1)
        structured = item.get("structuredContent")
        content = item.get("content")
        cleaned = {}
        for key, child in list(item.items())[:MAX_NODES]:
            if key == "_meta":
                note("metadata_omitted")
                continue
            if key == "content" and structured is not None and isinstance(content, list):
                remaining = []
                for block in content:
                    duplicate = False
                    if isinstance(block, dict) and block.get("type") == "text":
                        try:
                            duplicate = json.loads(block.get("text", "")) == structured
                        except (ValueError, TypeError, RecursionError):
                            pass
                    if duplicate:
                        note("duplicates_omitted")
                    else:
                        remaining.append(block)
                if remaining:
                    cleaned[key] = walk(remaining, depth + 1)
                continue
            cleaned[key] = walk(child, depth + 1)
        return cleaned

    clean = walk(value)
    text = clean if isinstance(clean, str) else encode(clean)
    if len(text.encode()) > MAX_CONTENT_BYTES:
        note("shortened")
        notes["original_text_bytes"] = len(text.encode())
        clean = excerpt(text)
    return clean, notes


def event_content(data: dict) -> dict:
    """Also used when reading a queue written by an older plugin version."""
    result = dict(data)
    notes = dict(result.get("capture_notes", {}))
    for key in ("prompt", "last_assistant_message", "tool_input", "tool_response"):
        if key in result:
            result[key], changes = normalize(result[key])
            for change, count in changes.items():
                notes[change] = notes.get(change, 0) + count
    if notes:
        result["capture_notes"] = notes
    return result
