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
import hashlib, hmac, ipaddress, json, os, re, socket, threading, time
import urllib.parse, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LIGHTBURN = os.environ.get("LIGHTBURN_URL", "http://127.0.0.1:19520")
HOST_FILE = os.environ.get("LIGHTBURN_HOST_FILE", "/etc/lightburn/host")
SECRET_FILE = os.environ.get("LIGHTBURN_SECRET_FILE", "/etc/lightburn/secret")
PUSHOVER_FILE = os.environ.get("LIGHTBURN_PUSHOVER_FILE", "/etc/lightburn/pushover")
HERE = os.path.dirname(os.path.abspath(__file__))
PAGES = {"/": "index.html", "/config": "config.html"}
HISTORY_FILE = os.environ.get("LIGHTBURN_HISTORY_FILE", "/var/lib/lightburn-status/history.jsonl")
HISTORY_MAX = 500    # events kept
ENDPOINTS = {"/api/status": "/api/status", "/api/project": "/api/project",
             "/api/goto/saved": "/api/goto/saved"}
WATCH_INTERVAL = 1   # seconds between checks
DOWN_AFTER = 5       # consecutive failed checks before "unreachable" alert
SNAPSHOT_MAX_AGE = 3 # seconds a cached snapshot may be served to the page
PUSHOVER = "https://api.pushover.net/1/"


def token(secret=None):
    if secret is None:
        with open(SECRET_FILE) as f:
            secret = f.read().strip()
    minute = str(int(time.time()) // 60)
    return hmac.new(secret.encode(), minute.encode(), hashlib.sha256).hexdigest()


config_gen = 0   # bumped whenever the LightBurn address changes (watcher re-baselines)


def load_host():
    """(host, port) saved from the config page, or None."""
    try:
        cfg = dict(l.strip().split("=", 1) for l in open(HOST_FILE) if "=" in l)
        return cfg["HOST"], int(cfg.get("PORT", 19520))
    except Exception:
        return None


def lightburn_url():
    saved = load_host()
    return f"http://{saved[0]}:{saved[1]}" if saved else LIGHTBURN


def parse_host(text):
    """Validate 'host' or 'host:port'. Returns (host, port) or raises ValueError.

    Only private/loopback/link-local addresses are accepted, so this page cannot be
    used to point the service (and its API tokens) at a machine on the internet.
    """
    m = re.fullmatch(r"([A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?)(?::(\d{1,5}))?", text.strip())
    if not m:
        raise ValueError("Enter an IP address or hostname, optionally followed by :port.")
    host, port = m.group(1), int(m.group(3) or 19520)
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535.")
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror:
        raise ValueError(f"Cannot resolve '{host}'.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not (ip.is_private or ip.is_loopback or ip.is_link_local):
            raise ValueError("Only local network addresses are allowed.")
    return host, port


def save_host(host, port):
    global config_gen
    tmp = HOST_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(f"HOST={host}\nPORT={port}\n")
    os.replace(tmp, HOST_FILE)
    config_gen += 1


def save_secret(secret):
    global config_gen
    tmp = SECRET_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secret)
    os.replace(tmp, SECRET_FILE)
    config_gen += 1


def secret_saved():
    try:
        return bool(open(SECRET_FILE).read().strip())
    except OSError:
        return False


def host_reachable(host, port):
    try:
        urllib.request.urlopen(f"http://{host}:{port}/", timeout=4).read(1)
        return True
    except urllib.error.HTTPError:
        return True      # it answered
    except Exception:
        return False


def fetch(path, secret=None):
    """Return (http_status, dict) from LightBurn; never raises.

    Uses the saved pairing secret unless one is given (to test a candidate secret).
    HTTP 502 = unreachable or rejected (the dict says which), 500 = no secret saved.
    """
    try:
        req = urllib.request.Request(lightburn_url() + path,
                                     headers={"Authorization": "Bearer " + token(secret)})
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


CAMERA_FILE = os.environ.get("LIGHTBURN_CAMERA_FILE", "/etc/lightburn/camera")
CAMERA_MAX_AGE = 3    # seconds between checks of LightBurn's camera image revision
_cam = {"t": 0, "code": 0, "meta": {}, "rev": None, "png": None}
_cam_lock = threading.Lock()


def camera_enabled():
    """The camera view is OFF unless explicitly enabled on the config page."""
    try:
        return open(CAMERA_FILE).read().strip() == "1"
    except OSError:
        return False


def save_camera(on):
    tmp = CAMERA_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write("1\n" if on else "0\n")
    os.replace(tmp, CAMERA_FILE)
    with _cam_lock:
        _cam.update(t=0, rev=None, png=None)     # drop any cached image


def fetch_png(path):
    """(status, bytes) for a binary image response from LightBurn; never raises."""
    try:
        req = urllib.request.Request(lightburn_url() + path,
                                     headers={"Authorization": "Bearer " + token()})
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200, r.read(25_000_000)
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:
        return 502, b""


def camera_state():
    """(code, metadata, png) of LightBurn's camera overlay, cached briefly.

    The image is re-downloaded only when LightBurn's overlay `revision` changes.
    code 200 = image available, 404 = LightBurn has no overlay loaded, else an error.
    """
    with _cam_lock:
        if time.time() - _cam["t"] > CAMERA_MAX_AGE:
            code, meta = fetch("/api/overlay/metadata")
            if code == 502 and "HTTP 404" in meta.get("error", ""):
                code, meta = 404, {}
            elif code == 200 and meta.get("revision") != _cam["rev"]:
                pcode, png = fetch_png("/api/overlay")
                if pcode == 200:
                    _cam.update(rev=meta.get("revision"), png=png)
                else:
                    code, meta = pcode, {"error": f"image request failed (HTTP {pcode})"}
            if code != 200:
                _cam.update(rev=None, png=None)
            _cam.update(t=time.time(), code=code, meta=meta)
        return _cam["code"], dict(_cam["meta"]), _cam["png"]


_summary = {"t": 0, "code": 0, "data": {}}
SUMMARY_MAX_AGE = 5   # seconds; /api/cuts is large, so it is fetched at most this often


def _cut_row(c, layer_colors):
    p = c.get("params") or {}
    row = {"index": c.get("index"), "name": c.get("name") or "", "kind": c.get("kind"),
           "mode": c.get("mode") or c.get("kind"), "enabled": c.get("enabled", True),
           "priority": c.get("priority", 0), "shape_count": c.get("shape_count", 0),
           "color": layer_colors.get(c.get("layer_index")),
           "speed": p.get("speed"), "min_power": p.get("min_power"), "max_power": p.get("max_power"),
           "passes": p.get("num_passes"), "interval": p.get("interval"), "dpi": p.get("dpi"),
           "z_offset": p.get("z_offset")}
    subs = []
    for s in c.get("sub_layers") or []:
        sp = s.get("params") or {}
        subs.append({"name": s.get("name") or "", "mode": s.get("mode"), "enabled": s.get("enabled", True),
                     "speed": sp.get("speed"), "min_power": sp.get("min_power"),
                     "max_power": sp.get("max_power"), "passes": sp.get("num_passes")})
    if subs:
        row["sub_layers"] = subs
    return row


def project_summary():
    """Compact project + cut-settings summary for the page (needs the 'project' capability)."""
    if time.time() - _summary["t"] <= SUMMARY_MAX_AGE:
        return _summary["code"], dict(_summary["data"])
    code, proj = fetch("/api/project")
    if code != 200:
        result = (code, proj)
    else:
        out = {k: proj.get(k) for k in ("filename", "modified", "shape_count", "units", "workspace")}
        laser = (proj.get("device") or {}).get("laser") or {}
        out["device"] = {"name": (proj.get("device") or {}).get("name"),
                         "controller": laser.get("controller"), "sources": laser.get("sources")}
        lcode, ldata = fetch("/api/layers")
        colors = {l.get("index"): l.get("color") for l in ldata.get("layers", [])} if lcode == 200 else {}
        ccode, cdata = fetch("/api/cuts")
        if ccode == 200 and cdata.get("product") == "lightburn":
            rows = [_cut_row(c, colors) for c in cdata.get("cuts", []) if c.get("in_use")]
            rows.sort(key=lambda r: (r["priority"], r["index"]))
            out["cuts"] = rows
            out["unused_cuts"] = sum(1 for c in cdata.get("cuts", []) if not c.get("in_use"))
        result = (200, out)
    _summary.update(t=time.time(), code=result[0], data=result[1])
    return result[0], dict(result[1])


snapshot = {"t": 0, "code": 0, "data": {}}   # latest /api/events/poll result


def live_snapshot():
    """Latest LightBurn poll data for the page: the watcher's cache, or a live fetch."""
    if time.time() - snapshot["t"] <= SNAPSHOT_MAX_AGE:
        return snapshot["code"], dict(snapshot["data"])
    return fetch("/api/events/poll")


def watch():
    """Background loop: record history, alert on job-state changes and on outages."""
    last = None        # last confirmed job state (None = needs a fresh baseline)
    pending = None     # candidate state, must be seen twice in a row
    pending_since = 0
    fails, fail_since = 0, 0
    down = False
    machine = None     # last seen "machine connected" flag
    job_start = job_file = None
    seen_gen = config_gen
    while True:
        if seen_gen != config_gen:      # LightBurn address changed: start fresh
            seen_gen = config_gen
            last = pending = machine = None
            fails = 0
            job_start = job_file = None
        now = time.time()
        code, data = fetch("/api/events/poll")
        snapshot.update(t=time.time(), code=code, data=data)
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
    def _send(self, code, body, ctype, cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/api/camera/meta":
            if not camera_enabled():
                return self._json(200, {"enabled": False})
            code, meta, _ = camera_state()
            out = {"enabled": True, "available": code == 200}
            if code == 200:
                out.update(meta)
            elif code != 404:
                out["error"] = meta.get("error", "LightBurn did not answer")
            self._json(200, out)
        elif path == "/api/camera/image":
            png = camera_state()[2] if camera_enabled() else None
            if png:     # the URL carries the revision, so a given URL never changes
                self._send(200, png, "image/png", cache="private, max-age=86400")
            else:
                self._send(404, b"no camera image", "text/plain")
        elif path == "/api/project/summary":
            code, data = project_summary()
            self._json(code, data)
        elif path == "/api/poll":
            code, data = live_snapshot()
            data["checked_at"] = int(time.time())
            self._json(code, data)
        elif path in ENDPOINTS:
            code, data = fetch(ENDPOINTS[path])
            data["checked_at"] = int(time.time())
            self._json(code, data)
        elif path == "/api/history":
            with history_lock:
                self._json(200, {"events": history[::-1][:100]})
        elif path == "/api/config":      # never returns the stored keys
            saved = load_host()
            if saved:
                host, port = saved
            else:
                host, port = urllib.parse.urlparse(LIGHTBURN).netloc.rsplit(":", 1)
            self._json(200, {"configured": load_pushover() is not None,
                             "secret_configured": secret_saved(),
                             "camera": camera_enabled(),
                             "host": host, "port": int(port),
                             "host_source": "config page" if saved else "service default"})
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
        elif path == "/api/config/host":
            try:
                host, port = parse_host(str(body.get("host", "")))
                save_host(host, port)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            except OSError as e:
                return self._json(500, {"error": f"could not save: {e}"})
            self._json(200, {"ok": True, "host": host, "port": port,
                             "reachable": host_reachable(host, port)})
        elif path == "/api/config/camera":
            on = body.get("enabled") is True
            try:
                save_camera(on)
            except OSError as e:
                return self._json(500, {"error": f"could not save: {e}"})
            self._json(200, {"ok": True, "camera": on})
        elif path == "/api/config/secret":
            secret = str(body.get("secret", "")).strip()
            if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{8,128}", secret):
                return self._json(400, {"error": "That does not look like a pairing secret "
                                                 "(8-128 letters, numbers or - _ . characters, no spaces)."})
            code, data = fetch("/api/status", secret)
            if code == 502 and "401" in data.get("error", ""):
                return self._json(400, {"error": "LightBurn rejected this secret. Check that you copied "
                                                 "the newest one, and that both computers' clocks are correct."})
            verified = code == 200
            try:
                save_secret(secret)
            except OSError as e:
                return self._json(500, {"error": f"could not save: {e}"})
            project = verified and fetch("/api/project", secret)[0] == 200
            self._json(200, {"ok": True, "verified": verified, "project": project,
                             "note": "" if verified else data.get("error", "")})
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
