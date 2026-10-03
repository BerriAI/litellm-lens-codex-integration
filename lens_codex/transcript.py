"""Narrow, version-sensitive usage reader. Never export reasoning or raw transcripts."""
from __future__ import annotations

import json
from pathlib import Path

MAX_READ = 32 * 1024 * 1024
USAGE_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


def usage_for_turn(path: str | None, offset: int, turn_id: str) -> tuple[dict | None, bool]:
    """Read only the current turn's recorded token totals. No historical scan/backfill.

    The desktop does not expose a passive app-server event subscription. This optional
    reader uses the hook-provided transcript and exact turn IDs; missing/changed schema
    yields unknown usage, never an invented zero. Tests cover the supported versions.
    """
    if not path:
        return None, False
    try:
        with Path(path).open("rb") as stream:
            beginning = max(0, offset - 256 * 1024)
            stream.seek(beginning)
            raw = stream.read(MAX_READ + 1)
    except OSError:
        return None, False
    if len(raw) > MAX_READ:
        return None, False
    current = None
    totals = dict.fromkeys(USAGE_FIELDS, 0)
    seen = set()
    complete = False
    position = beginning
    for line in raw.splitlines(keepends=True):
        after_prompt = position >= offset
        position += len(line)
        try:
            record = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        payload = record.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if record.get("type") == "turn_context":
            current = payload.get("turn_id")
        if record.get("type") != "event_msg":
            continue
        kind = payload.get("type")
        if kind == "task_started":
            current = payload.get("turn_id")
        if kind == "token_count" and current == turn_id and after_prompt:
            info = payload.get("info") or {}
            last, total = info.get("last_token_usage"), info.get("total_token_usage")
            if not isinstance(last, dict) or not isinstance(total, dict):
                continue
            fingerprint = json.dumps(total, sort_keys=True)
            if fingerprint in seen:
                continue
            if not all(isinstance(last.get(key, 0), int) and last.get(key, 0) >= 0 for key in USAGE_FIELDS):
                continue
            seen.add(fingerprint)
            for key in USAGE_FIELDS:
                totals[key] += last.get(key, 0)
        if kind in {"task_complete", "turn_aborted"} and payload.get("turn_id") == turn_id and after_prompt:
            complete = True
            current = None
    return (totals if seen else None), complete
