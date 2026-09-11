#!/usr/bin/env python3
"""Claude Code quota sender for claude-quota-bridge.

Normal mode is invoked as Claude Code's statusLine command. It immediately reads
5-hour / 7-day quota data from stdin, prints a compact status line, and pushes
those values to the bridge.

Because Claude Code does not currently expose model-scoped weekly limits such as
Fable in statusLine stdin, this script can also launch a low-frequency,
background `get_usage` control request through the local Claude Code CLI. That
request uses Claude Code's own authentication, does not invoke a model, and can
return model-scoped weekly limits. The interface is experimental and may change.
"""

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
CONFIG_FILE = CLAUDE_DIR / "quota-bridge.json"
PUSH_CACHE_FILE = CLAUDE_DIR / "quota-bridge-push-cache.json"
PROBE_CACHE_FILE = CLAUDE_DIR / "quota-bridge-usage-cache.json"
PROBE_LOCK_FILE = CLAUDE_DIR / "quota-bridge-usage.lock"


def read_json_file(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json_file(path, value):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def compact_pct(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if abs(number - round(number)) < 0.05:
        return str(int(round(number)))
    return f"{number:.1f}"


def parse_reset_epoch(value):
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)) or str(value).replace(".", "", 1).isdigit():
            epoch = int(float(value))
            return epoch if epoch > 0 else None
    except (TypeError, ValueError, OverflowError):
        pass

    if not isinstance(value, str):
        return None
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (TypeError, ValueError, OverflowError):
        return None


def normalize_window(used, reset):
    try:
        pct = float(used)
    except (TypeError, ValueError, OverflowError):
        return None
    epoch = parse_reset_epoch(reset)
    if epoch is None or not (0 <= pct <= 1000):
        return None
    return {"used_percentage": pct, "resets_at": epoch}


def extract_statusline_window(rate_limits, key):
    value = rate_limits.get(key)
    if not isinstance(value, dict):
        return None
    return normalize_window(value.get("used_percentage"), value.get("resets_at"))


def extract_probe_windows(stdout_text):
    """Parse Claude Code get_usage stream-json output.

    Supports both the current `rate_limits.model_scoped[]` shape and the
    `rate_limits.limits[]` weekly_scoped shape seen in recent clients.
    """
    response = None
    for raw_line in stdout_text.splitlines():
        try:
            message = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if message.get("type") != "control_response":
            continue
        body = message.get("response") or {}
        if body.get("request_id") != "quota-bridge":
            continue
        if body.get("subtype") != "success":
            return {}
        response = body.get("response") or {}
        break

    if not isinstance(response, dict):
        return {}
    rate_limits = response.get("rate_limits") or {}
    if not isinstance(rate_limits, dict):
        return {}

    result = {}
    for key in ("five_hour", "seven_day"):
        value = rate_limits.get(key)
        if isinstance(value, dict):
            window = normalize_window(value.get("utilization"), value.get("resets_at"))
            if window:
                result[key] = window

    # Preferred current control-protocol shape.
    scoped = rate_limits.get("model_scoped")
    if isinstance(scoped, list):
        for item in scoped:
            if not isinstance(item, dict):
                continue
            name = str(item.get("display_name") or "").strip()
            if name.lower() != "fable":
                continue
            window = normalize_window(item.get("utilization"), item.get("resets_at"))
            if window:
                result["fable"] = window
                break

    # Tolerate the raw usage-endpoint style if Claude Code exposes it instead.
    if "fable" not in result:
        limits = rate_limits.get("limits")
        if isinstance(limits, list):
            for item in limits:
                if not isinstance(item, dict) or item.get("kind") != "weekly_scoped":
                    continue
                scope = item.get("scope") or {}
                model = scope.get("model") if isinstance(scope, dict) else None
                name = model.get("display_name") if isinstance(model, dict) else None
                if str(name or "").strip().lower() != "fable":
                    continue
                window = normalize_window(item.get("percent"), item.get("resets_at"))
                if window:
                    result["fable"] = window
                    break

    return result


def print_status(five, seven, fable):
    parts = []
    for label, window in (("5h", five), ("7d", seven), ("Fable", fable)):
        if not window:
            continue
        pct = compact_pct(window.get("used_percentage"))
        if pct is not None:
            parts.append(f"{label} {pct}%")
    print("Claude · " + " · ".join(parts) if parts else "Claude")


def send_payload(config, payload, timeout=None):
    url = config.get("url")
    token = config.get("token")
    if not url or not token:
        return False

    if timeout is None:
        try:
            timeout = max(0.1, float(config.get("timeout_seconds", 0.5)))
        except (TypeError, ValueError):
            timeout = 0.5

    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url=url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-API-Token": token,
            "User-Agent": "claude-code-quota-statusline/2.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def post_if_needed(config, payload, fingerprint):
    try:
        interval = max(1, int(config.get("push_interval_seconds", 60)))
    except (TypeError, ValueError):
        interval = 60

    now = time.time()
    cache = read_json_file(PUSH_CACHE_FILE, {}) or {}
    same = cache.get("fingerprint") == fingerprint
    try:
        recent = now - float(cache.get("last_sent_at", 0)) < interval
    except (TypeError, ValueError):
        recent = False
    if same and recent:
        return

    if send_payload(config, payload):
        write_json_file(PUSH_CACHE_FILE, {"fingerprint": fingerprint, "last_sent_at": now})


def probe_config(config):
    enabled = config.get("scoped_usage_enabled", True)
    try:
        refresh = max(60, int(config.get("scoped_usage_refresh_seconds", 300)))
    except (TypeError, ValueError):
        refresh = 300
    try:
        timeout = max(5, int(config.get("scoped_usage_timeout_seconds", 60)))
    except (TypeError, ValueError):
        timeout = 60
    command = config.get("claude_command", "claude")
    if not isinstance(command, str) or not command.strip():
        command = "claude"
    return bool(enabled), refresh, timeout, command


def cache_is_fresh(cache, refresh_seconds):
    try:
        return time.time() - float(cache.get("fetched_at", 0)) < refresh_seconds
    except (TypeError, ValueError):
        return False


def maybe_spawn_probe(config):
    enabled, refresh_seconds, _, _ = probe_config(config)
    if not enabled:
        return
    cache = read_json_file(PROBE_CACHE_FILE, {}) or {}
    if cache_is_fresh(cache, refresh_seconds):
        return

    # Avoid a thundering herd while an existing detached probe is still starting.
    try:
        if PROBE_LOCK_FILE.exists() and time.time() - PROBE_LOCK_FILE.stat().st_mtime < 120:
            return
    except OSError:
        pass

    cmd = [sys.executable, str(Path(__file__).resolve()), "--refresh-usage"]
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(cmd, **kwargs)
    except OSError:
        pass


def acquire_probe_lock():
    try:
        PROBE_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(PROBE_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        return True
    except FileExistsError:
        try:
            if time.time() - PROBE_LOCK_FILE.stat().st_mtime > 120:
                PROBE_LOCK_FILE.unlink()
                return acquire_probe_lock()
        except OSError:
            pass
        return False
    except OSError:
        return False


def release_probe_lock():
    try:
        PROBE_LOCK_FILE.unlink()
    except OSError:
        pass


def run_usage_probe(config):
    enabled, _, timeout, claude_command = probe_config(config)
    if not enabled:
        return {}

    args = [
        claude_command,
        "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--mcp-config", '{"mcpServers":{}}',
        "--settings", '{"hooks":{}}',
    ]
    request_line = json.dumps({
        "type": "control_request",
        "request_id": "quota-bridge",
        "request": {"subtype": "get_usage"},
    }, separators=(",", ":")) + "\n"

    env = os.environ.copy()
    # A statusLine command inherits markers that normally block nested Claude
    # processes. This is a non-interactive read-only control request, so clear
    # only the nesting markers while preserving auth/config selection.
    for key in (
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_CHILD_SESSION",
        "CLAUDE_CODE_SESSION_ID",
    ):
        env.pop(key, None)
    env["CLAUDE_CODE_SKIP_PROMPT_HISTORY"] = "1"

    try:
        proc = subprocess.run(
            args,
            input=request_line,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return extract_probe_windows(proc.stdout or "")


def refresh_usage_worker():
    config = read_json_file(CONFIG_FILE, {}) or {}
    enabled, refresh_seconds, _, _ = probe_config(config)
    if not enabled:
        return 0

    existing = read_json_file(PROBE_CACHE_FILE, {}) or {}
    if cache_is_fresh(existing, refresh_seconds):
        return 0
    if not acquire_probe_lock():
        return 0

    try:
        existing = read_json_file(PROBE_CACHE_FILE, {}) or {}
        if cache_is_fresh(existing, refresh_seconds):
            return 0

        windows = run_usage_probe(config)
        if not windows:
            return 0

        now = time.time()
        cache = {"fetched_at": now}
        cache.update(windows)
        write_json_file(PROBE_CACHE_FILE, cache)

        payload = {
            "source": socket.gethostname(),
            "observed_at": now,
            "probe_source": "claude_get_usage",
        }
        payload.update(windows)
        send_payload(config, payload, timeout=max(1.0, float(config.get("timeout_seconds", 0.5))))
        return 0
    finally:
        release_probe_lock()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--refresh-usage":
        return refresh_usage_worker()

    try:
        incoming = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        print("Claude")
        return 0

    config = read_json_file(CONFIG_FILE, {}) or {}
    probe_cache = read_json_file(PROBE_CACHE_FILE, {}) or {}
    rate_limits = incoming.get("rate_limits") or {}

    five = extract_statusline_window(rate_limits, "five_hour") or probe_cache.get("five_hour")
    seven = extract_statusline_window(rate_limits, "seven_day") or probe_cache.get("seven_day")
    fable = probe_cache.get("fable") if isinstance(probe_cache.get("fable"), dict) else None

    print_status(five, seven, fable)
    maybe_spawn_probe(config)

    # Before the first API response the official statusLine windows can be
    # absent. Cached get_usage values are still useful, so push whichever valid
    # windows we currently know.
    payload = {
        "source": socket.gethostname(),
        "session_id": incoming.get("session_id"),
        "observed_at": time.time(),
    }
    for key, value in (("five_hour", five), ("seven_day", seven), ("fable", fable)):
        if isinstance(value, dict):
            payload[key] = value

    fingerprint_data = {k: payload[k] for k in ("five_hour", "seven_day", "fable") if k in payload}
    if not fingerprint_data:
        return 0
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    post_if_needed(config, payload, fingerprint)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
