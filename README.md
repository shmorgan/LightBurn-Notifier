# LightBurn Notifier

Version 1.0 - status page and Pushover alerts for LightBurn (REST API v2.2+).


Status page for LightBurn (REST API on 10.255.201.243:19520), served at http://10.255.201.254/lightburn/

| File | Purpose |
|---|---|
| lightburn_status.py | Python service (127.0.0.1:8095): signs API requests, serves pages, watches the job and sends Pushover alerts |
| index.html | Status page (machine, job, progress, position) |
| config.html | Pushover key entry page (/lightburn/config) |
| lightburn-status.service | systemd unit |
| lightburn.conf | nginx snippet, included in the port 80 server block |
| pair.ps1 | Run on the LightBurn PC to pair and get the API secret |
| assets/lightburn-logo-128.png | 128x128 icon to upload to Pushover |
| install.sh | Installs everything on the web server |

Secrets live only on the server: /etc/lightburn/secret (LightBurn pairing) and /etc/lightburn/pushover (Pushover keys). They are not part of this folder.