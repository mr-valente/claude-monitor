#!/usr/bin/env python3
import hmac
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

PORT = int(os.environ.get("PORT", "8787"))
TOKEN = os.environ.get("QUOTA_TOKEN", "")
DATA_FILE = Path(os.environ.get("DATA_FILE", "/data/quota.json"))
MAX_BODY = 64 * 1024
LOCK = threading.Lock()

if not TOKEN:
    raise SystemExit("QUOTA_TOKEN must be set")


def iso_utc(epoch):
    if epoch is None:
        return None
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def load_state():
    if not DATA_FILE.exists():
        return {}
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="quota-", suffix=".json", dir=str(DATA_FILE.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, DATA_FILE)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass


def normalize_incoming_window(value):
    if not isinstance(value, dict):
        return None
    try:
        used = float(value["used_percentage"])
        reset = int(float(value["resets_at"]))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not (0 <= used <= 1000):
        return None
    if reset <= 0:
        return None
    return {
        "used_percentage": round(used, 2),
        "resets_at_epoch": reset,
    }


def human_until(epoch, now):
    seconds = int(epoch - now)
    if seconds <= 0:
        return "reset passed"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = max(1, rem // 60)
    if days:
        return f"in {days}d {hours}h"
    if hours:
        return f"in {hours}h {minutes}m"
    return f"in {minutes}m"


def render_window(stored, now):
    if not isinstance(stored, dict):
        return None
    try:
        observed_used = float(stored["used_percentage"])
        reset_epoch = int(stored["resets_at_epoch"])
    except (KeyError, TypeError, ValueError):
        return None

    expired = now >= reset_epoch
    effective_used = 0.0 if expired else observed_used
    remaining = max(0.0, 100.0 - effective_used)
    used_text = f"{effective_used:.0f}" if abs(effective_used - round(effective_used)) < 0.05 else f"{effective_used:.1f}"

    return {
        "used_percentage": round(effective_used, 2),
        "observed_used_percentage": round(observed_used, 2),
        "remaining_percentage": round(remaining, 2),
        "resets_at": iso_utc(reset_epoch),
        "resets_at_epoch": reset_epoch,
        "expired": expired,
        "summary": f"{used_text}% used · {human_until(reset_epoch, now)}",
    }


def public_state(state):
    now = time.time()
    result = {
        "five_hour": render_window(state.get("five_hour"), now),
        "seven_day": render_window(state.get("seven_day"), now),
        "fable": render_window(state.get("fable"), now),
        "updated_at": state.get("updated_at"),
        "updated_at_epoch": state.get("updated_at_epoch"),
        "source": state.get("source"),
        "session_id": state.get("session_id"),
    }
    if state.get("updated_at_epoch"):
        result["age_seconds"] = max(0, int(now - float(state["updated_at_epoch"])))
    else:
        result["age_seconds"] = None
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "ClaudeQuotaBridge/1.0"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")

    def send_json(self, status, payload):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        supplied = self.headers.get("X-API-Token", "")
        return bool(supplied) and hmac.compare_digest(supplied, TOKEN)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            self.send_json(200, {"ok": True})
            return
        if path != "/usage":
            self.send_json(404, {"error": "not found"})
            return
        if not self.authorized():
            self.send_json(401, {"error": "unauthorized"})
            return
        with LOCK:
            state = load_state()
        if not state:
            self.send_json(404, {"error": "no quota data received yet"})
            return
        self.send_json(200, public_state(state))

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/usage":
            self.send_json(404, {"error": "not found"})
            return
        if not self.authorized():
            self.send_json(401, {"error": "unauthorized"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"error": "invalid content length"})
            return
        if length <= 0 or length > MAX_BODY:
            self.send_json(413 if length > MAX_BODY else 400, {"error": "invalid request body size"})
            return

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(400, {"error": "invalid JSON"})
            return
        if not isinstance(payload, dict):
            self.send_json(400, {"error": "JSON body must be an object"})
            return

        incoming = {}
        for key in ("five_hour", "seven_day", "fable"):
            if key in payload:
                normalized = normalize_incoming_window(payload.get(key))
                if normalized is not None:
                    incoming[key] = normalized

        if not incoming:
            self.send_json(400, {"error": "no valid rate-limit windows in request"})
            return

        now = time.time()
        with LOCK:
            state = load_state()
            # Deliberately preserve a previously known window if Claude temporarily omits it.
            state.update(incoming)
            state["updated_at_epoch"] = now
            state["updated_at"] = iso_utc(now)
            if payload.get("source"):
                state["source"] = str(payload["source"])[:200]
            if payload.get("session_id"):
                state["session_id"] = str(payload["session_id"])[:200]
            save_state(state)

        self.send_json(200, public_state(state))


if __name__ == "__main__":
    print(f"Claude quota bridge listening on 0.0.0.0:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
