#!/bin/sh
# Install the LightBurn status page on the web server (Ubuntu + nginx). Run from this folder:
#   sudo ./install.sh [secret-from-pair.ps1]
# The secret can also be written later to /etc/lightburn/secret.
set -e
install -d /opt/lightburn-status /etc/lightburn
install -m 644 lightburn_status.py index.html config.html /opt/lightburn-status/
chown www-data /etc/lightburn
if [ -n "$1" ]; then
  umask 077; printf '%s' "$1" > /etc/lightburn/secret; chown www-data /etc/lightburn/secret
fi
install -m 644 lightburn-status.service /etc/systemd/system/
mkdir -p /etc/nginx/snippets
install -m 644 lightburn.conf /etc/nginx/snippets/lightburn.conf
systemctl daemon-reload
systemctl enable lightburn-status
systemctl restart lightburn-status
echo "Add 'include snippets/lightburn.conf;' to the port-80 server { } block if it is not there,"
echo "then: nginx -t && systemctl reload nginx"
echo "Pushover keys are entered on the page at /lightburn/config"