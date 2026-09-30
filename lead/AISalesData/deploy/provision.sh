#!/usr/bin/env bash
# provision.sh — prepare a fresh Ubuntu/Debian VPS for AI Sales Machine.
#
# Run ON THE SERVER as a user with sudo:
#     sudo bash provision.sh
#
# Installs Python, Google Chrome (for the scraper), nginx, a self-signed
# certificate, a dedicated service account, and the systemd unit. Idempotent:
# safe to re-run.

set -euo pipefail

APP_USER=aisales
APP_ROOT=/opt/aisales
APP_DIR="$APP_ROOT/app"
VENV_DIR="$APP_ROOT/venv"
CERT_DIR=/etc/ssl/aisales

echo "==> Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3 python3-venv python3-pip \
    nginx openssl curl ca-certificates ufw

echo "==> Installing Google Chrome (required by the Maps scraper)"
if ! command -v google-chrome >/dev/null 2>&1; then
    curl -fsSL https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
        -o /tmp/chrome.deb
    apt-get install -y -qq /tmp/chrome.deb
    rm -f /tmp/chrome.deb
fi
google-chrome --version || echo "!! Chrome not available — Scraper Campaign will not work"

echo "==> Creating service account $APP_USER"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir "$APP_ROOT" --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_ROOT"

echo "==> Self-signed certificate"
if [ ! -f "$CERT_DIR/selfsigned.crt" ]; then
    mkdir -p "$CERT_DIR"
    IP=$(curl -fsS --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')
    openssl req -x509 -nodes -days 825 -newkey rsa:2048 \
        -keyout "$CERT_DIR/selfsigned.key" \
        -out    "$CERT_DIR/selfsigned.crt" \
        -subj   "/CN=$IP" \
        -addext "subjectAltName=IP:$IP" 2>/dev/null
    chmod 600 "$CERT_DIR/selfsigned.key"
    echo "    issued for $IP"
fi

echo "==> Firewall"
ufw allow OpenSSH >/dev/null 2>&1 || true
ufw allow 'Nginx Full' >/dev/null 2>&1 || true
# Port 8501 is deliberately NOT opened: Streamlit binds to localhost and is
# reachable only through the TLS proxy.
yes | ufw enable >/dev/null 2>&1 || true

echo
echo "==> System ready."
echo "    Next: push the code with deploy/push.sh from your machine,"
echo "    then set the login password on the server:"
echo "      sudo -u $APP_USER $VENV_DIR/bin/python $APP_DIR/set_password.py"
