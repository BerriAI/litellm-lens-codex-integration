"""Durable delivery with explicit uncertainty after a lost acknowledgement."""
from __future__ import annotations

import contextlib
import fcntl
import json
import socket
import ssl
import time
import urllib.error
import urllib.request

from . import __version__
from .state import config, database, drain_inbox, health_notes, safe_health, state_dir
from .batches import spans, split
from .content import encode
from .trace import build_trace
from .transcript import usage_for_turn


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Gateway redirected the request. Check its base URL before sending credentials.")


def request(settings: dict, path: str, payload: dict | None = None) -> object:
    context = ssl.create_default_context()
    # Apple/Python distributions do not always locate macOS's system certificate bundle.
    if __import__("sys").platform == "darwin":
        context.load_verify_locations(cafile="/etc/ssl/cert.pem")
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))
    body = None if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    req = urllib.request.Request(settings["gateway"] + path, data=body, headers={
        "Authorization": "Bearer " + settings["api_key"], "Content-Type": "application/json",
        "User-Agent": "litellm-lens-codex/" + __version__})
    with opener.open(req, timeout=15) as response:
        # Readback includes the whole trace; it can be larger than an upload batch.
        limit = (32 if payload is None else 2) * 1024 * 1024
        raw = response.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("The gateway response exceeded the safe readback limit.")
        if not raw:
            return {}
        return json.loads(raw)


def verify(settings: dict) -> None:
    try:
        request(settings, "/v1/traces?limit=1")
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code in {401, 403}:
            raise ValueError("This key cannot access traces. Check the key and its tracing permissions.") from None
        if exc.code in {404, 501, 503}:
            raise ValueError("Tracing is not available at this gateway. Enable Lens tracing first.") from None
        raise ValueError(f"The gateway returned HTTP {exc.code}. Check the URL and try again.") from None
    except (OSError, ValueError) as exc:
        if isinstance(exc, ValueError) and "redirect" in str(exc):
            raise
        raise ValueError("Could not reach the gateway securely. Check its URL and your connection.") from None


def sent(db, turn: dict, trace_id: str) -> None:
    db.execute("UPDATE turns SET status='sent',sent=?,payload=NULL,error=NULL,trace_id=? WHERE session=? AND turn=?",
               (time.time(), trace_id, turn["session"], turn["turn"]))
    db.execute("DELETE FROM events WHERE session=? AND turn=?", (turn["session"], turn["turn"]))


def reconcile(settings: dict, payload: dict) -> bool:
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    result = request(settings, "/v1/traces/" + spans[0]["traceId"])
    if not isinstance(result, dict) or not isinstance(result.get("spans"), list):
        return False
    received = {s.get("span_id", s.get("spanId")) for s in result["spans"]}
    return {s["spanId"] for s in spans}.issubset(received)


@contextlib.contextmanager
def delivery_lock():
    with (state_dir() / "delivery.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def prepare(turn: dict, settings: dict, now: float) -> list[dict]:
    with database(write=False) as db:
        saved = [dict(row) for row in db.execute(
            "SELECT * FROM batches WHERE session=? AND turn=? ORDER BY position",
            (turn["session"], turn["turn"]))]
        rows = [dict(row) for row in db.execute(
            "SELECT * FROM events WHERE session=? AND turn=? ORDER BY id", (turn["session"], turn["turn"]))]
    if saved:
        return saved
    if turn["status"] == "uncertain" and turn["payload"]:
        # An old in-flight request must be reconciled exactly, never rebuilt/reposted.
        payload = json.loads(turn["payload"])
        parts = [payload]
        warning = turn["warning"]
    else:
        usage, complete = usage_for_turn(turn["path"], turn["offset"], turn["turn"])
        if not complete and now < turn["ended"] + 15:
            return []
        payload = build_trace(turn, rows, settings, usage)
        parts = split(payload)
        warning = turn["warning"] or (None if usage is not None else
            "Token usage was not available. This trace does not include a token or cost estimate.")
    encoded = [(turn["session"], turn["turn"], i, encode(part),
                "uncertain" if turn["status"] == "uncertain" else "pending") for i, part in enumerate(parts)]
    with database() as db:
        db.executemany("INSERT INTO batches(session,turn,position,payload,status) VALUES (?,?,?,?,?)", encoded)
        db.execute("UPDATE turns SET payload=?,warning=? WHERE session=? AND turn=?",
                   (encode(payload), warning, turn["session"], turn["turn"]))
    return [dict(session=a, turn=b, position=c, payload=d, status=e) for a,b,c,d,e in encoded]


def batch_state(turn: dict, batch: dict, status: str, error: str | None, next_attempt: float) -> None:
    with database() as db:
        db.execute("UPDATE batches SET status=? WHERE session=? AND turn=? AND position=?",
                   (status, turn["session"], turn["turn"], batch["position"]))
        db.execute("UPDATE turns SET status=?,error=?,next_attempt=? WHERE session=? AND turn=?",
                   (status, error, next_attempt, turn["session"], turn["turn"]))


def deliver_turn(turn: dict, settings: dict, now: float) -> None:
    parts = prepare(turn, settings, now)
    if not parts:
        return
    trace_id = spans(json.loads(parts[0]["payload"]))[0]["traceId"]
    for batch in parts:
        if batch["status"] == "sent":
            continue
        if not config().get("enabled"):
            return
        payload = json.loads(batch["payload"])
        if batch["status"] == "uncertain":
            try:
                confirmed = reconcile(settings, payload)
            except (OSError, ValueError):
                confirmed = False
            if not confirmed:
                batch_state(turn, batch, "uncertain",
                    "Delivery is not confirmed. The saved batch is being checked in Lens; it has not been sent twice.",
                    now + 30)
                return
        else:
            # Commit intent BEFORE sending. A restart can reconcile just this batch.
            batch_state(turn, batch, "uncertain", "Checking whether this batch reached Lens.", now + 30)
            with database() as db:
                db.execute("UPDATE turns SET attempts=attempts+1 WHERE session=? AND turn=?",
                           (turn["session"], turn["turn"]))
            safe_health("helper_heartbeat", str(time.time()))
            try:
                response = request(settings, "/v1/traces", payload)
                partial = response.get("partialSuccess", response.get("partial_success", {})) if isinstance(response, dict) else {}
                if int(partial.get("rejectedSpans", partial.get("rejected_spans", 0))) > 0:
                    batch_state(turn, batch, "uncertain",
                        "Lens accepted only part of a batch. Checking the saved spans; no automatic replay.", now + 30)
                    return
            except urllib.error.HTTPError as exc:
                code = exc.code
                retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                exc.close()
                status = "blocked" if code in {400, 401, 403, 404, 413, 422, 501} else "uncertain"
                message = f"Gateway returned HTTP {code}. Check the gateway and key in Settings, then retry."
                if code == 413:
                    message = "The gateway rejected an upload as too large (HTTP 413). The batch is saved locally."
                if code == 429:
                    status, message = "retry", "Gateway rate limit reached. Delivery will retry automatically."
                delay = min(300, 5 * 2 ** min(turn["attempts"], 6))
                if code == 429 and retry_after.isdigit():
                    delay = max(delay, min(3600, int(retry_after)))
                batch_state(turn, batch, status, message, now + delay)
                return
            except urllib.error.URLError as exc:
                if isinstance(exc.reason, (ConnectionRefusedError, socket.gaierror)):
                    # No connection was established, so no spans could be accepted.
                    batch_state(turn, batch, "retry",
                        "Cannot reach the gateway. Saved turns will retry automatically.", now + 30)
                else:
                    batch_state(turn, batch, "uncertain",
                        "Connection interrupted. Checking Lens before sending this saved batch again.", now + 30)
                return
            except (OSError, ValueError):
                # No remote body/URL in errors: it may contain credentials or content.
                batch_state(turn, batch, "uncertain",
                    "Connection interrupted. Checking Lens before sending this saved batch again.", now + 30)
                return
        with database() as db:
            db.execute("UPDATE batches SET status='sent' WHERE session=? AND turn=? AND position=?",
                       (turn["session"], turn["turn"], batch["position"]))
            db.execute("UPDATE turns SET status='pending',error=NULL,next_attempt=0 WHERE session=? AND turn=?",
                       (turn["session"], turn["turn"]))
    with database() as db:
        sent(db, turn, trace_id)
        db.execute("DELETE FROM batches WHERE session=? AND turn=?", (turn["session"], turn["turn"]))
        db.execute("DELETE FROM health WHERE key='delivery_error'")


def flush(now: float | None = None) -> None:
    settings = config()
    now = time.time() if now is None else now
    with delivery_lock() as locked:
        if not locked:
            return
        drain_inbox()
        if not settings.get("enabled"):
            return
        with database() as db:
            # Version 0.2.3's rejected oversized requests were definitely not
            # accepted. Rebuild those from retained events using text-only batches.
            db.execute("UPDATE turns SET status='pending',payload=NULL,error=NULL,next_attempt=0 "
                       "WHERE status='blocked' AND error LIKE 'Gateway returned HTTP 413.%' "
                       "AND NOT EXISTS (SELECT 1 FROM batches b WHERE b.session=turns.session AND b.turn=turns.turn)")
        with database(write=False) as db:
            turns = [dict(row) for row in db.execute(
                "SELECT * FROM turns WHERE status IN ('pending','retry','uncertain') AND next_attempt<=? "
                "ORDER BY started LIMIT 10", (now,))]
        for turn in turns:
            if not config().get("enabled"):
                return
            try:
                deliver_turn(turn, settings, now)
            except (ValueError, KeyError, TypeError):
                # One malformed saved turn must not starve unrelated sessions.
                with database() as db:
                    db.execute("UPDATE turns SET status='blocked',error=? WHERE session=? AND turn=?",
                               ("This saved turn could not be prepared. Its data is retained locally for recovery.",
                                turn["session"], turn["turn"]))
        with database() as db:
            db.execute("DELETE FROM turns WHERE status='sent' AND sent<?", (now - 7 * 86400,))
            db.execute("DELETE FROM events WHERE turn='' AND received<?", (now - 86400,))


def retry_blocked() -> None:
    with database() as db:
        db.execute("UPDATE batches SET status='pending' WHERE status='blocked'")
        db.execute("UPDATE turns SET status='retry',next_attempt=0 WHERE status='blocked'")


def status() -> dict:
    settings = config()
    with database(write=False) as db:
        counts = {row[0]: row[1] for row in db.execute("SELECT status,count(*) FROM turns GROUP BY status")}
        recent = [dict(row) for row in db.execute(
            "SELECT session,turn,status,started,ended,error,warning,trace_id,sent FROM turns ORDER BY started DESC LIMIT 8")]
        notes = dict(db.execute("SELECT key,value FROM health"))
        failure = db.execute("SELECT error FROM turns WHERE status IN ('blocked','uncertain') ORDER BY started LIMIT 1").fetchone()
    notes.update(health_notes())
    if failure:
        notes["delivery_error"] = failure[0]
    inbox = list((state_dir() / "inbox").glob("*.json"))
    counts["queued_events"] = len(inbox)
    heartbeat = float(notes.get("helper_heartbeat") or 0)
    if heartbeat and settings.get("enabled") and time.time() - heartbeat > 90:
        notes["helper_error"] = "The background sender stopped responding. Your turns are saved locally; restart the helper."
    return {"configured": bool(settings.get("gateway")), "enabled": settings.get("enabled", False),
            "gateway": settings.get("gateway", ""), "agent_name": settings.get("agent_name", "codex"),
            "counts": counts, "recent": recent, "health": notes}
