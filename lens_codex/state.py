"""Private local configuration and durable, transactional capture."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import sqlite3
import tempfile
import time
from urllib.parse import urlsplit

MAX_EVENT_BYTES = 16 * 1024 * 1024
MAX_QUEUE_BYTES = 256 * 1024 * 1024
EVENTS = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "Interrupt", "SessionEnd"}


def state_dir() -> Path:
    path = Path(os.environ.get("LENS_CODEX_HOME", "~/.local/share/litellm-lens-codex")).expanduser()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".lens-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def config() -> dict:
    try:
        return json.loads((state_dir() / "config.json").read_text())
    except FileNotFoundError:
        return {}


def save_config(value: dict) -> None:
    atomic_json(state_dir() / "config.json", value)


def gateway_url(value: str) -> str:
    parsed = urlsplit(value.strip().rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Enter your LiteLLM gateway URL, starting with https://.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Use the gateway URL without credentials, a query, or a fragment.")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Remote gateways must use HTTPS.")
    if parsed.path not in {"", "/v1", "/v1/traces", "/ui"}:
        raise ValueError("Use the gateway's base URL, without a page path.")
    return f"{parsed.scheme}://{parsed.netloc}"


@contextlib.contextmanager
def database():
    path = state_dir() / "events.sqlite3"
    db = sqlite3.connect(path, timeout=2)
    path.chmod(0o600)
    db.row_factory = sqlite3.Row
    try:
        with (state_dir() / "schema.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if db.execute("PRAGMA user_version").fetchone()[0] == 0:
                db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, fingerprint TEXT UNIQUE, session TEXT NOT NULL,
                turn TEXT NOT NULL, kind TEXT NOT NULL, received REAL NOT NULL, data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS event_turn ON events(session, turn);
            CREATE TABLE IF NOT EXISTS turns (
                session TEXT NOT NULL, turn TEXT NOT NULL, started REAL NOT NULL,
                ended REAL, status TEXT NOT NULL DEFAULT 'recording', path TEXT,
                offset INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, payload TEXT, error TEXT,
                trace_id TEXT, sent REAL, warning TEXT,
                PRIMARY KEY(session, turn)
            );
            CREATE TABLE IF NOT EXISTS health (key TEXT PRIMARY KEY, value TEXT);
            PRAGMA user_version=1;
                """)
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def health(key: str, value: str) -> None:
    with database() as db:
        db.execute("INSERT OR REPLACE INTO health VALUES (?, ?)", (key, value))


def transcript_path(event: dict, settings: dict) -> Path | None:
    candidate = Path(str(event.get("transcript_path", ""))).expanduser().resolve()
    base = Path(settings.get("codex_home", "~/.codex")).expanduser().resolve()
    if any(candidate.is_relative_to(base / folder) for folder in ("sessions", "archived_sessions")):
        return candidate
    return None


def redact(value: object, secret: str) -> object:
    if isinstance(value, str):
        text = value.replace(secret, "[REDACTED]") if secret else value
        text = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]{12,}", "Bearer [REDACTED]", text)
        return re.sub(r"\bsk-[A-Za-z0-9_-]{16,}", "[REDACTED]", text)
    if isinstance(value, list):
        return [redact(item, secret) for item in value]
    if isinstance(value, dict):
        return {key: redact(item, secret) for key, item in value.items()}
    return value


def set_enabled(enabled: bool) -> None:
    settings = config()
    settings["enabled"] = enabled
    save_config(settings)
    if not enabled:
        with database() as db:
            db.execute("DELETE FROM events WHERE EXISTS (SELECT 1 FROM turns WHERE turns.session=events.session "
                       "AND turns.turn=events.turn AND turns.status='recording')")
            db.execute("UPDATE turns SET status='discarded',warning=? WHERE status='recording'",
                       ("Recording paused before this turn finished; incomplete content discarded.",))


def capture(event: dict, now: float | None = None) -> bool:
    settings = config()
    if not settings.get("enabled"):
        return False
    now = time.time() if now is None else now
    kind, session, turn = event.get("hook_event_name"), event.get("session_id"), event.get("turn_id", "")
    if kind not in EVENTS or not isinstance(session, str) or not session or len(session) > 200:
        raise ValueError("Unsupported hook or missing session ID. Nothing was exported.")
    if not isinstance(turn, str) or len(turn) > 200:
        raise ValueError("Invalid turn ID.")
    # Only fields needed for a trace; never store account IDs, settings, or arbitrary future hook fields.
    fields = {"hook_event_name", "session_id", "turn_id", "model", "prompt", "tool_name", "tool_input",
              "tool_response", "tool_use_id", "last_assistant_message", "reason"}
    data = {key: value for key, value in event.items() if key in fields}
    data = redact(data, settings.get("api_key", ""))
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True)
    if len(encoded.encode()) > MAX_EVENT_BYTES:
        raise ValueError("A hook exceeded the 16 MB safety limit. Its content was not truncated or exported.")
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    with database() as db:
        queued_bytes = db.execute("SELECT COALESCE(SUM(LENGTH(data)), 0) FROM events").fetchone()[0]
        if queued_bytes + len(encoded.encode()) > MAX_QUEUE_BYTES:
            raise ValueError("Local capture queue is full. Open Lens Codex to resolve the delivery problem.")
        if kind == "UserPromptSubmit":
            if not turn:
                raise ValueError("This Codex hook is missing its turn ID. Update Codex before recording.")
            path = transcript_path(event, settings)
            offset = path.stat().st_size if path and path.exists() else 0
            db.execute("INSERT OR IGNORE INTO turns(session,turn,started,path,offset) VALUES (?,?,?,?,?)",
                       (session, turn, now, str(path) if path else None, offset))
        existing = db.execute("SELECT status FROM turns WHERE session=? AND turn=?", (session, turn)).fetchone()
        if turn and (not existing or existing[0] != "recording"):
            # No historical backfill; finalized turns are immutable.
            return False
        db.execute("INSERT OR IGNORE INTO events(fingerprint,session,turn,kind,received,data) VALUES (?,?,?,?,?,?)",
                   (fingerprint, session, turn, kind, now, encoded))
        if kind in {"Stop", "Interrupt"} and turn:
            db.execute("UPDATE turns SET ended=?,status='pending',next_attempt=? WHERE session=? AND turn=?",
                       (now, now + 2, session, turn))
        elif kind == "SessionEnd":
            db.execute("UPDATE turns SET ended=?,status='pending',next_attempt=?,warning=? "
                       "WHERE session=? AND status='recording'", (now, now + 2,
                       "Session ended before a completed response was captured.", session))
        db.execute("INSERT OR REPLACE INTO health VALUES ('last_capture',?)", (str(now),))
    return True
