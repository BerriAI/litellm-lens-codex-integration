"""Durable delivery with explicit uncertainty after a lost acknowledgement."""
from __future__ import annotations

import contextlib
import fcntl
import json
import ssl
import time
import urllib.error
import urllib.request

from .state import config, database, health, state_dir
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
        "User-Agent": "litellm-lens-codex/0.1.0"})
    with opener.open(req, timeout=15) as response:
        raw = response.read(2 * 1024 * 1024)
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


def flush(now: float | None = None) -> None:
    settings = config()
    if not settings.get("enabled"):
        return
    now = time.time() if now is None else now
    with delivery_lock() as locked:
        if not locked:
            return
        with database() as db:
            turns = [dict(row) for row in db.execute(
                "SELECT * FROM turns WHERE status IN ('pending','retry','uncertain') AND next_attempt<=? "
                "ORDER BY started LIMIT 10", (now,))]
        for turn in turns:
            if not config().get("enabled"):
                return
            with database() as db:
                rows = [dict(row) for row in db.execute(
                    "SELECT * FROM events WHERE session=? AND turn=? ORDER BY id", (turn["session"], turn["turn"]))]
            if turn["payload"]:
                payload = json.loads(turn["payload"])
            else:
                usage, complete = usage_for_turn(turn["path"], turn["offset"], turn["turn"])
                if not complete and now < turn["ended"] + 15:
                    continue  # Allow Codex's buffered transcript to finish writing.
                payload = build_trace(turn, rows, settings, usage)
                warning = turn["warning"] or (None if usage is not None else
                    "Token usage was not available. This trace does not include a token or cost estimate.")
                with database() as db:
                    db.execute("UPDATE turns SET payload=?,warning=? WHERE session=? AND turn=?",
                               (json.dumps(payload, ensure_ascii=False), warning, turn["session"], turn["turn"]))
            trace_id = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"]
            if turn["status"] == "uncertain":
                try:
                    confirmed = reconcile(settings, payload)
                except (OSError, ValueError):
                    confirmed = False
                with database() as db:
                    if confirmed:
                        sent(db, turn, trace_id)
                    else:
                        db.execute("UPDATE turns SET next_attempt=? WHERE session=? AND turn=?",
                                   (now + 30, turn["session"], turn["turn"]))
                continue
            if not config().get("enabled"):
                return
            # Mark uncertain BEFORE network I/O. A process crash can never silently replay an accepted batch.
            with database() as db:
                db.execute("UPDATE turns SET status='uncertain',attempts=attempts+1,next_attempt=?,error=? "
                           "WHERE session=? AND turn=?", (now + 30,
                           "Delivery was not confirmed. Checking Lens before sending anything again.",
                           turn["session"], turn["turn"]))
            try:
                response = request(settings, "/v1/traces", payload)
                partial = response.get("partialSuccess", response.get("partial_success", {})) if isinstance(response, dict) else {}
                if int(partial.get("rejectedSpans", partial.get("rejected_spans", 0))) > 0:
                    raise ValueError("The gateway rejected part of this trace. Delivery needs review.")
            except urllib.error.HTTPError as exc:
                code = exc.code
                exc.close()
                status = "blocked" if code in {400, 401, 403, 404, 413, 422, 501} else "uncertain"
                message = f"Gateway returned HTTP {code}. Open settings to check the gateway and key."
                if code == 429:
                    status, message = "retry", "Gateway rate limit reached. Delivery will retry automatically."
                with database() as db:
                    db.execute("UPDATE turns SET status=?,error=?,next_attempt=? WHERE session=? AND turn=?",
                               (status, message, now + min(300, 5 * 2 ** min(turn["attempts"], 6)),
                                turn["session"], turn["turn"]))
            except (OSError, ValueError):
                # Do not include gateway bodies or exception URLs: they can echo credentials and user content.
                health("delivery_error", "Delivery was not confirmed. Checking Lens; the saved trace will not be discarded.")
            else:
                with database() as db:
                    sent(db, turn, trace_id)
                    db.execute("DELETE FROM health WHERE key='delivery_error'")
        with database() as db:
            db.execute("DELETE FROM turns WHERE status='sent' AND sent<?", (now - 7 * 86400,))
            db.execute("DELETE FROM events WHERE turn='' AND received<?", (now - 86400,))


def retry_blocked() -> None:
    with database() as db:
        db.execute("UPDATE turns SET status='retry',next_attempt=0 WHERE status='blocked'")


def status() -> dict:
    settings = config()
    with database() as db:
        counts = {row[0]: row[1] for row in db.execute("SELECT status,count(*) FROM turns GROUP BY status")}
        recent = [dict(row) for row in db.execute(
            "SELECT session,turn,status,started,ended,error,warning,trace_id,sent FROM turns ORDER BY started DESC LIMIT 8")]
        notes = dict(db.execute("SELECT key,value FROM health"))
    return {"configured": bool(settings.get("gateway")), "enabled": settings.get("enabled", False),
            "gateway": settings.get("gateway", ""), "agent_name": settings.get("agent_name", "codex"),
            "counts": counts, "recent": recent, "health": notes}
