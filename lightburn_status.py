#!/usr/bin/env python3
"""Tiny proxy for the LightBurn REST API (stdlib only).

Serves the status page and /api/status, signing requests with the paired secret.
Also watches the job state and sends Pushover alerts.
Listens on 127.0.0.1:8095; nginx exposes it at /lightburn/.

Copyright (C) 2026 Stephen Morgan

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with
this program. If not, see <https://www.gnu.org/licenses/>.
"""
import hashlib, hmac, json, os, threading, time, urllib.parse, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LIGHTBURN = os.environ.get("LIGHTBURN_URL", "http://127.0.0.1:19520")
SECRET_FILE = os.environ.get("LIGHTBURN_SECRET_FILE", "/etc/lightburn/secret")
PUSHOVER_FILE = os.environ.get("LIGHTBURN_PUSHOVER_FILE", "/etc/lightburn/pushover")
HERE = os.path.dirname(os.path.abspath(__file__))
PAGES = {"/": "index.html", "/config": "config.html"}
HISTORY_FILE = os.environ.get("LIGHTBURN_HISTORY_FILE", "/var/lib/lightburn-status/history.jsonl")
HISTORY_MAX = 500    # events kept
ENDPOINTS = {"/api/status": "/api/status", "/api/poll": "/api/events/poll",
             "/api/project": "/api/project"}
WATCH_INTERVAL = 2   # seconds between checks
DOWN_AFTER = 3       # consecutive failed checks before "unreachable" alert
PUSHOVER = "https://api.pushover.net/1/"


def token():
    with open(SECRET_FILE) as f:
        secret = f.read().strip()
    minute = str(int(time.time()) // 60)
    return hmac.new(secret.encode(), minute.encode(), hashlib.sha256).hexdigest()


def fetch(path):
    """Return (http_status, dict) from LightBurn; never raises."""
    try:
        req = urllib.request.Request(LIGHTBURN + path,
                                     headers={"Authorization": "Bearer " + token()})
        with urllib.request.urlopen(req, timeout=5) as r:
            return 200, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return 502, {"error": f"LightBurn returned HTTP {e.code}" +
                     (" (token rejected - re-pair?)" if e.code == 401 else "")}
    except FileNotFoundError:
        return 500, {"error": f"secret file {SECRET_FILE} missing - pair first"}
    except Exception as e:
        return 502, {"error": f"LightBurn unreachable: {e}"}


# ---- Pushover ---------------------------------------------------------------

def load_pushover():
    try:
        cfg = dict(l.strip().split("=", 1) for l in open(PUSHOVER_FILE) if "=" in l)
        return cfg["APP_TOKEN"].strip(), cfg["USER_KEY"].strip()
    except Exception:
        return None


def pushover_call(endpoint, fields):
    """POST to Pushover. Returns (ok, error_text)."""
    try:
        data = urllib.parse.urlencode(fields).encode()
        urllib.request.urlopen(PUSHOVER + endpoint, data, timeout=10).read()
        return True, ""
    except urllib.error.HTTPError as e:
        try:
            errs = json.loads(e.read()).get("errors", [])
        except Exception:
            errs = []
        return False, "; ".join(errs) or f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)


def pushover(title, message):
    creds = load_pushover()
    if not creds:
        return False, "Pushover is not configured"
    ok, err = pushover_call("messages.json", {"token": creds[0], "user": creds[1],
                                              "title": title, "message": message})
    if not ok:
        print("pushover failed:", err, flush=True)
    return ok, err


def save_pushover(app_token, user_key):
    tmp = PUSHOVER_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"APP_TOKEN={app_token}\nUSER_KEY={user_key}\n")
    os.replace(tmp, PUSHOVER_FILE)


# ---- History ----------------------------------------------------------------

history = []                 # oldest first, capped at HISTORY_MAX
history_lock = threading.Lock()
_since_trim = 0


def load_history():
    try:
        with open(HISTORY_FILE) as f:
            for line in f.readlines()[-HISTORY_MAX:]:
                try:
                    history.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass


def record(event):
    """Add an event to the history (memory + file). Never raises."""
    global _since_trim
    event.setdefault("t", time.time())
    event["t"] = int(event["t"])
    with history_lock:
        history.append(event)
        del history[:-HISTORY_MAX]
        try:
            os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
            _since_trim += 1
            if _since_trim >= HISTORY_MAX:       # keep the file from growing forever
                _since_trim = 0
                with open(HISTORY_FILE, "w") as f:
                    f.writelines(json.dumps(e) + "\n" for e in history)
            else:
                with open(HISTORY_FILE, "a") as f:
                    f.write(json.dumps(event) + "\n")
        except OSError as e:
            print("history write failed:", e, flush=True)


# ---- Watcher ----------------------------------------------------------------

def describe(old, new):
    if new == "idle" and old in ("running", "paused"):
        return "Job finished or stopped"
    return {"running": "Job started" if old == "idle" else "Job resumed",
            "paused": "Job paused"}.get(new, f"Job {new}")


def duration(sec):
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s" if m else f"{s}s"


def project_filename():
    """Loaded project's filename, or None (needs the 'project' capability)."""
    code, data = fetch("/api/project")
    return data.get("filename") or None if code == 200 else None


def watch():
    """Background loop: record history, alert on job-state changes and on outages."""
    last = None        # last confirmed job state (None = needs a fresh baseline)
    pending = None     # candidate state, must be seen twice in a row
    pending_since = 0
    fails, fail_since = 0, 0
    down = False
    machine = None     # last seen "machine connected" flag
    job_start = job_file = None
    while True:
        now = time.time()
        code, data = fetch("/api/events/poll")
        if code == 200:
            if down:
                record({"type": "outage", "to": "reachable", "t": now,
                        "down_for": int(now - fail_since)})
                pushover("LightBurn: reachable again",
                         f"LightBurn is responding again after {duration(now - fail_since)}.")
                down, last, pending, machine = False, None, None, None   # rebaseline
            fails = 0
            state = data.get("job", {}).get("state")
            conn = data.get("connection", {})
            device = conn.get("device_name", "LightBurn")
            connected = conn.get("connected")
            if machine is not None and connected is not None and connected != machine:
                record({"type": "machine", "t": now, "device": device,
                        "to": "connected" if connected else "disconnected"})
            if connected is not None:
                machine = connected
            if state and last is None:
                last = state
                if state != "idle":
                    job_file = project_filename()
            elif state and state != last:
                if pending == state:
                    ev = {"type": "job", "t": pending_since, "from": last, "to": state}
                    if state == "running" and last == "idle":
                        job_start, job_file = pending_since, project_filename()
                    if job_file:
                        ev["file"] = job_file
                    if state == "idle" and job_start:
                        ev["total"] = int(pending_since - job_start)
                    record(ev)
                    lines = [f"{device}: {last} -> {state}"]
                    if job_file:
                        lines.append("File: " + job_file)
                    if "total" in ev:
                        lines.append("Total time: " + duration(ev["total"]))
                    pushover("LightBurn: " + describe(last, state), "\n".join(lines))
                    if state == "idle":
                        job_start = job_file = None
                    last, pending = state, None
                else:
                    pending, pending_since = state, now
            else:
                pending = None
        elif code == 502:   # unreachable / rejected; 500 (no secret yet) is not an outage
            if fails == 0:
                fail_since = now
            fails += 1
            if fails >= DOWN_AFTER and not down:
                down = True
                err = data.get("error", "No response")
                record({"type": "outage", "to": "unreachable", "t": fail_since, "error": err})
                pushover("LightBurn: UNREACHABLE", err)
        time.sleep(WATCH_INTERVAL)


# ---- HTTP -------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path in ENDPOINTS:
            code, data = fetch(ENDPOINTS[path])
            data["checked_at"] = int(time.time())
            self._json(code, data)
        elif path == "/api/history":
            with history_lock:
                self._json(200, {"events": history[::-1][:100]})
        elif path == "/api/config":      # never returns the stored keys
            self._json(200, {"configured": load_pushover() is not None})
        elif path in PAGES:
            with open(os.path.join(HERE, PAGES[path]), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(min(n, 4096)) or b"{}")
        except Exception:
            return self._json(400, {"error": "bad request"})
        if path == "/api/config":
            app_token = str(body.get("app_token", "")).strip()
            user_key = str(body.get("user_key", "")).strip()
            if not (app_token.isalnum() and user_key.isalnum()):
                return self._json(400, {"error": "Tokens must be letters and numbers only."})
            ok, err = pushover_call("users/validate.json", {"token": app_token, "user": user_key})
            if not ok:
                return self._json(400, {"error": "Pushover rejected these keys: " + err})
            try:
                save_pushover(app_token, user_key)
            except OSError as e:
                return self._json(500, {"error": f"could not save: {e}"})
            self._json(200, {"ok": True})
        elif path == "/api/config/test":
            ok, err = pushover("LightBurn test", "Pushover alerts are working.")
            self._json(200 if ok else 400, {"ok": ok, "error": err})
        else:
            self._json(404, {"error": "not found"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    load_history()
    threading.Thread(target=watch, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", 8095), Handler).serve_forever()
