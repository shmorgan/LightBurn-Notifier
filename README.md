# LightBurn Notifier

**Version 1.4.0** - a small self-hosted status page and [Pushover](https://pushover.net) alert service for [LightBurn](https://lightburnsoftware.com) (v2.2 and later, which includes a local REST API).

It gives you:

- A **status page** (`/lightburn/`) showing whether LightBurn is reachable, the selected machine, whether the machine is connected, the job state (idle / running / paused), job progress, machine and workpiece position, the speed and power overrides, spindle / coolant / vacuum state, jog settings, and your saved positions. It refreshes every second.
- **Pushover alerts** to your phone when a job starts, pauses, resumes, finishes or stops, and when LightBurn becomes unreachable or comes back. Alerts are sent by a background watcher, so they work even if nobody has the page open.
- A **history** table on the status page: job started / paused / resumed / finished (with total job time), machine connected / disconnected, and LightBurn unreachable / reachable again, with timestamps. The last 500 events are kept across restarts.
- Optionally, the **project filename** of the job (see [Showing the project filename](#showing-the-project-filename)).
- A **settings page** (`/lightburn/config`) where you set the address of the LightBurn computer to monitor, enter the pairing secret, enter your Pushover keys, verify them, and send a test alert.

It is read-only: it can look at LightBurn's state but cannot start, stop or change anything.

---

## Contents

1. [How it works](#how-it-works)
2. [Requirements](#requirements)
3. [Installation](#installation)
4. [Configuring Pushover](#configuring-pushover)
5. [Alerts reference](#alerts-reference)
6. [Configuration reference](#configuration-reference)
7. [Security notes](#security-notes)
8. [Troubleshooting](#troubleshooting)
9. [Updating, re-pairing and uninstalling](#updating-re-pairing-and-uninstalling)
10. [Repository layout](#repository-layout)

---

## How it works

```
 Browser ──HTTP──> nginx ──/lightburn/──> lightburn_status.py ──HTTP + Bearer token──> LightBurn PC
 (status page)     :80     (reverse proxy)   127.0.0.1:8095        (REST API :19520)
                                                │
                                                └──HTTPS──> api.pushover.net ──> your phone
```

LightBurn's REST API requires a Bearer token that **changes every minute**: `hex(HMAC-SHA256(secret, floor(unix_time / 60)))`. The secret is obtained once by *pairing* (see below). Because a web browser cannot safely hold that secret or compute the token, a small Python service (`lightburn_status.py`, standard library only, no pip packages) runs on a server on your network:

1. It holds the secret and signs every request to LightBurn.
2. It serves the status page and a small JSON API to the browser. The browser never talks to LightBurn directly.
3. It runs a **watcher thread** that checks LightBurn once a second, keeps the latest reading for the status page (so any number of open browsers puts no extra load on LightBurn), and sends Pushover alerts.

nginx simply proxies `/lightburn/` to the service, which listens on `127.0.0.1:8095` only (it is not reachable from the network except through nginx).

### The pairing secret

LightBurn only allows pairing (`POST /api/connect`) **from the LightBurn computer itself**, and shows a consent dialog the user must click. Pairing returns a secret. This project only asks for the `state` capability (read-only machine and job state). The secret is stored on the server in `/etc/lightburn/secret`.

### How the watcher decides to alert

- The first successful check after the service starts sets the baseline. Restarting the service never sends an alert.
- A job state change (for example `idle` to `running`) must be seen on **two consecutive checks** (about 1 to 2 seconds) before an alert is sent. This avoids alerts from a single odd reading.
- If LightBurn fails **5 checks in a row** (unreachable, connection refused, token rejected), an "UNREACHABLE" alert is sent once. When it answers again, a "reachable again" alert is sent and the job state is re-baselined so you do not get a false "job started".
- Having no secret file yet is not treated as an outage.

### History

Every confirmed change is appended to `/var/lib/lightburn-status/history.jsonl` (one JSON object per line; the folder is created by systemd and survives restarts and reboots). The file is trimmed to the last 500 events. The status page shows the newest 100. Job events record the time the new state was first seen, and job-finished events include the total time since the job started. If LightBurn was already running a job when the service started, that job's total time is not known.

To clear the history, stop the service, delete the file, and start it again:

```bash
sudo systemctl stop lightburn-status && sudo rm /var/lib/lightburn-status/history.jsonl && sudo systemctl start lightburn-status
```

### What LightBurn does not tell us

LightBurn's REST API reports only the job state (`idle`, `running`, `paused`) and, on some controllers, a progress percentage. It does **not** report a job name, elapsed time, estimated time or remaining time. This project measures job durations itself from the state changes it sees.

### Showing the project filename

The one thing the API offers about *what* is being run is the loaded project's filename (`GET /api/project`). It needs the `project` capability, which is chosen at pairing time and cannot be changed afterwards, so pair again with:

```powershell
powershell -ExecutionPolicy Bypass -File .\pair.ps1 -IncludeProject
```

and paste the new secret into the **Pairing secret** box on the config page (see step 4 below). The filename then appears on the status page, in the history, and in the Pushover message. Without that capability everything still works; the filename is simply left out. Note that it is the file *loaded* in LightBurn, which is normally the job being run but is not guaranteed to be. Also note that LightBurn's `project` capability additionally covers camera imagery and project details; this project only ever reads the filename.

---

## Requirements

**LightBurn computer (Windows, macOS or Linux)**

- LightBurn **2.2 or later** with the REST API enabled. In LightBurn, open **Settings > Extensions** and enable the API. Enable **Allow API Access From Network** so the server can reach it (the API listens on port **19520**; by default it only listens on loopback).
- The computer's firewall must allow inbound TCP **19520** from the server.
- PowerShell (Windows) or any HTTP client for the one-time pairing step. `pair.ps1` is provided for Windows.

**Server (Linux)**

- A Linux machine on the same network as the LightBurn computer. Tested on Ubuntu 20.04 (Python 3.8, nginx 1.18, systemd). Any distribution with these should work.
- `python3` (3.6 or newer, standard library only), `nginx` and `systemd`.
- Root or `sudo` access.
- Outbound HTTPS access to `api.pushover.net`.
- **Accurate clocks on both machines** (use NTP). The token is only valid for the current and previous minute, so a clock difference of more than about a minute causes 401 errors.

**Pushover**

- A Pushover account and its **user key** (shown on the dashboard after logging in at pushover.net).
- A Pushover **application** (create one at pushover.net/apps/build). It gives you an **application API token**. You can upload `assets/lightburn-logo-128.png` as the app icon.

---

## Installation

### 1. Get the code

On the server:

```bash
git clone https://github.com/shmorgan/LightBurn-Notifier.git
cd LightBurn-Notifier
```

(The repository is private, so use your GitHub credentials, or download the ZIP from GitHub and copy it to the server.)

### 2. Tell it where LightBurn is

You can do this after installing, on the config page (`/lightburn/config`, field **LightBurn computer**), and change it at any time without restarting anything.

To have the right address from the first start instead, edit `lightburn-status.service` and set the address of the LightBurn computer:

```ini
Environment=LIGHTBURN_URL=http://192.168.1.50:19520
```

Use the LightBurn computer's IP address (or hostname) and keep port `19520`. An address saved on the config page takes priority over this setting.

### 3. Pair with LightBurn (one time)

Pairing must be run **on the LightBurn computer**.

1. Copy `pair.ps1` to the LightBurn computer.
2. Make sure LightBurn is running with the API enabled.
3. Open PowerShell and run it. If PowerShell blocks scripts, use a one-time bypass:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\pair.ps1
   ```

4. **Switch to LightBurn and click Allow** in the consent dialog. You have 30 seconds. If it times out, run the script again.
5. The script prints `Secret: xxxxxxxxxxxxxxxx`. Copy the secret. Treat it like a password.

(On macOS or Linux, the equivalent request is
`curl -X POST http://localhost:19520/api/connect -H 'Content-Type: application/json' -d '{"application_name":"Status Page","capabilities":["state"]}'`.)

### 4. Install on the server

```bash
sudo ./install.sh 'PASTE_SECRET_HERE'
```

This script:

- copies the service and pages to `/opt/lightburn-status/`
- saves the secret to `/etc/lightburn/secret` (readable only by the service user)
- installs and starts the `lightburn-status` systemd service
- installs the nginx snippet at `/etc/nginx/snippets/lightburn.conf`

Without the argument it installs everything and you set the secret afterwards. The easiest way is the config page: open `/lightburn/config`, paste the secret into **Pairing secret**, and click **Verify & save**. It is checked against LightBurn before saving, and the page tells you whether the pairing includes project access. (If the config page is not reachable yet, write it by hand instead:)

```bash
sudo sh -c 'printf %s "PASTE_SECRET_HERE" > /etc/lightburn/secret && chown www-data /etc/lightburn/secret && chmod 600 /etc/lightburn/secret'
```

### 5. Add the nginx route

The script does not edit your nginx site files. Add this line inside the `server { ... }` block that serves the site (for example `/etc/nginx/sites-enabled/default`):

```nginx
include snippets/lightburn.conf;
```

Then test and reload:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

The snippet contains:

```nginx
location = /lightburn { return 301 /lightburn/; }
location /lightburn/ {
    proxy_pass http://127.0.0.1:8095/;
}
```

The trailing slashes matter. If you do not use nginx, any reverse proxy that maps `/lightburn/` to `http://127.0.0.1:8095/` works, or you can change the listening address in `lightburn_status.py`.

### 6. Check it

Open `http://YOUR-SERVER/lightburn/`. Within a couple of seconds the page should say **LightBurn is running** and show your machine name. If not, see [Troubleshooting](#troubleshooting).

---

## Configuring Pushover

1. Open `http://YOUR-SERVER/lightburn/config` (there is also an **Alert settings** link at the top of the status page).
2. Enter your Pushover **application API token** and **user key**.
3. Click **Verify & save**. The keys are checked with Pushover before they are saved, so a typo gives an error instead of silently failing.
4. Click **Send test alert**. A notification should arrive on your phone.

The keys are stored in `/etc/lightburn/pushover` (mode 600). The page never displays saved keys again; to change them, enter new ones.

Alternatively, create the file by hand (no restart needed; it is read on each alert):

```bash
sudo sh -c 'printf "APP_TOKEN=YOUR_APP_TOKEN\nUSER_KEY=YOUR_USER_KEY\n" > /etc/lightburn/pushover && chown www-data /etc/lightburn/pushover && chmod 600 /etc/lightburn/pushover'
```

---

## Alerts reference

| Event | Pushover title | When |
|---|---|---|
| Job starts | `LightBurn: Job started` | state goes `idle` to `running` |
| Job resumes | `LightBurn: Job resumed` | state goes `paused` to `running` |
| Job pauses | `LightBurn: Job paused` | state goes to `paused` |
| Job ends | `LightBurn: Job finished or stopped` | state goes from `running` or `paused` to `idle` (message includes total time, and the filename if available) |
| Outage | `LightBurn: UNREACHABLE` | 5 consecutive failed checks |
| Recovery | `LightBurn: reachable again` | first success after an outage |

LightBurn does not report whether a job completed or was cancelled, so "finished or stopped" covers both.

---

## Configuration reference

| Setting | Where | Default |
|---|---|---|
| LightBurn address | **config page** (saved in `/etc/lightburn/host`), or `Environment=LIGHTBURN_URL=` in `lightburn-status.service` as the fallback | `http://127.0.0.1:19520` (the unit file sets an example address; change it) |
| LightBurn address file | `LIGHTBURN_HOST_FILE` env var | `/etc/lightburn/host` |
| Pairing secret file | `LIGHTBURN_SECRET_FILE` env var | `/etc/lightburn/secret` |
| Pushover keys file | `LIGHTBURN_PUSHOVER_FILE` env var | `/etc/lightburn/pushover` |
| History file | `LIGHTBURN_HISTORY_FILE` env var | `/var/lib/lightburn-status/history.jsonl` |
| Events kept | `HISTORY_MAX` in `lightburn_status.py` | 500 |
| Check interval | `WATCH_INTERVAL` in `lightburn_status.py` | 1 second |
| Failed checks before outage alert | `DOWN_AFTER` in `lightburn_status.py` | 5 |
| Listening address and port | last line of `lightburn_status.py` | `127.0.0.1:8095` |
| Page refresh | the `loop()` timer at the end of `index.html` | 1 second (history every 5 seconds) |

After changing the service file or the Python script: `sudo systemctl daemon-reload && sudo systemctl restart lightburn-status`.

### Endpoints served by the service

| Method and path | Purpose |
|---|---|
| `GET /` | status page |
| `GET /config` | Pushover configuration page |
| `GET /api/status` | LightBurn `/api/status` (device name, connected, capabilities) |
| `GET /api/poll` | latest LightBurn `/api/events/poll` snapshot (job, position, overrides, aux, settings, connection), cached by the watcher |
| `GET /api/goto/saved` | LightBurn saved positions |
| `GET /api/project` | LightBurn `/api/project` (only works if paired with `project`) |
| `GET /api/history` | newest 100 history events, newest first |
| `GET /api/config` | Pushover configured or not, and the LightBurn host and port being monitored (never returns the keys) |
| `POST /api/config/secret` | verify a pairing secret against LightBurn and save it (`{"secret": "..."}`) |
| `POST /api/config/host` | set the LightBurn host (`{"host": "192.168.1.50"}` or `"host:port"`) |
| `POST /api/config` | verify and save Pushover keys |
| `POST /api/config/test` | send a test alert |

---

## Security notes

- The LightBurn address set on the config page must be a **local network address** (private, loopback or link-local); public addresses are refused. This stops the page from being used to send the service's API tokens to a machine on the internet.
- The service holds a **read-only** (`state`) LightBurn credential and cannot change anything in LightBurn.
- The status page and the config page have **no login**. Anyone who can reach the server can see machine state and can **replace** the Pushover keys, the pairing secret and the LightBurn address (they cannot read the existing keys or secret). This is fine on a trusted home or workshop network; do not expose the server to the internet as-is.
- To require a password on the config page, add basic auth in nginx:

  ```bash
  sudo apt install apache2-utils
  sudo htpasswd -c /etc/nginx/.htpasswd yourname
  ```

  and in the nginx snippet, before the `location /lightburn/` block:

  ```nginx
  location /lightburn/config {
      auth_basic "LightBurn";
      auth_basic_user_file /etc/nginx/.htpasswd;
      proxy_pass http://127.0.0.1:8095/config;
  }
  location /lightburn/api/config {
      auth_basic "LightBurn";
      auth_basic_user_file /etc/nginx/.htpasswd;
      proxy_pass http://127.0.0.1:8095/api/config;
  }
  ```
- The secret and Pushover keys live only in `/etc/lightburn/` on the server and are excluded from this repository by `.gitignore`. Never commit them.
- Traffic between the server and LightBurn is plain HTTP on your LAN (that is how LightBurn's API works).

---

## Troubleshooting

First, look at the service log:

```bash
sudo journalctl -u lightburn-status -n 50 --no-pager
systemctl status lightburn-status
```

| Symptom | Likely cause and fix |
|---|---|
| Page says "secret file ... missing - pair first" | Pairing secret not saved. Do steps 3 and 4. |
| "LightBurn unreachable" | LightBurn is not running, API network access is off, the address in `LIGHTBURN_URL` is wrong, or the LightBurn computer's firewall blocks port 19520. Test from the server: `curl -i http://LIGHTBURN-IP:19520/` should return a placeholder page. |
| "LightBurn returned HTTP 401 (token rejected - re-pair?)" | Clocks differ by more than about a minute (enable NTP on both machines), or the secret is wrong or was replaced by a newer pairing. Re-pair. |
| Browser shows 502 Bad Gateway | The service is not running: `sudo systemctl restart lightburn-status`. |
| Browser shows 404 at `/lightburn/` | The nginx `include` line is missing or nginx was not reloaded. |
| Pairing script times out or is declined | You must click Allow in LightBurn within 30 seconds. Run it again. |
| `pair.ps1` blocked by PowerShell | Use `powershell -ExecutionPolicy Bypass -File .\pair.ps1`. |
| "Pushover rejected these keys" on the config page | Re-check the application token and the user key (they are different). Both are letters and numbers only. |
| Test alert fails | Server cannot reach `api.pushover.net` over HTTPS, or the keys are wrong. Check the journal. |
| No alerts but status page works | Pushover keys not configured, or the job state did not change for 2 consecutive checks. |

---

## Updating, re-pairing and uninstalling

**Update** - pull the new code and run `sudo ./install.sh` again (no argument, so the existing secret is kept). Re-check `LIGHTBURN_URL` in the service file first, because the installer overwrites the unit file.

**Re-pair** - run `pair.ps1` again and paste the new secret into the **Pairing secret** box on the config page. Each pairing creates a new secret and a new consent prompt, so always save the newest one.

**Uninstall**

```bash
sudo systemctl disable --now lightburn-status
sudo rm /etc/systemd/system/lightburn-status.service /etc/nginx/snippets/lightburn.conf
sudo rm -r /opt/lightburn-status /etc/lightburn
# remove the include line from your nginx site, then:
sudo systemctl daemon-reload && sudo nginx -t && sudo systemctl reload nginx
```

---

## Repository layout

| File | Purpose |
|---|---|
| `lightburn_status.py` | The service: signs LightBurn API requests, serves the pages, runs the alert watcher |
| `index.html` | Status page |
| `config.html` | Pushover configuration page |
| `lightburn-status.service` | systemd unit (set `LIGHTBURN_URL` here) |
| `lightburn.conf` | nginx snippet |
| `pair.ps1` | Pairing script, run on the LightBurn computer (`-IncludeProject` to also allow reading the project filename) |
| `install.sh` | Installer for the server |
| `assets/lightburn-logo-128.png` | 128x128 icon to use for the Pushover application |
| `VERSION` | Current version |

LightBurn API documentation: <https://github.com/LightBurnSoftware/LightBurn-REST>

---

## License

Copyright (C) 2026 Stephen Morgan

LightBurn Notifier is free software: you can redistribute it and/or modify it under the terms of the **GNU General Public License, version 3 or (at your option) any later version**, as published by the Free Software Foundation. It is distributed in the hope that it will be useful, but **without any warranty**; without even the implied warranty of merchantability or fitness for a particular purpose. See the [LICENSE](LICENSE) file for the full text.

This project is not affiliated with or endorsed by LightBurn Software. "LightBurn" and its logo are trademarks of their respective owner; the logo in `assets/` is included only as an icon for use with your own notifications.
