#!/usr/bin/env bash
# push.sh — copy the app to the VPS and (re)start it.
#
# Run FROM THIS MACHINE:
#     bash deploy/push.sh user@1.2.3.4
#
# Deliberately excluded:
#   .venv/              rebuilt on the server (628 MB, platform-specific)
#   sales_campaign.db   fresh start on the server, per your choice
#   .streamlit/secrets.toml, .sender_key
#                       secrets are pushed separately and never bundled
#   logs/, __pycache__/, backups

set -euo pipefail

TARGET="${1:-}"
if [ -z "$TARGET" ]; then
    echo "usage: bash deploy/push.sh user@host [ssh-port]" >&2
    exit 1
fi
PORT="${2:-22}"
APP_ROOT=/opt/aisales
APP_DIR="$APP_ROOT/app"
SRC="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Syncing code to $TARGET:$APP_DIR"
rsync -az --delete -e "ssh -p $PORT" \
    --exclude '.venv/' \
    --exclude '__pycache__/' \
    --exclude 'logs/' \
    --exclude '.wdm/' \
    --exclude '*.db' \
    --exclude '*.db-wal' \
    --exclude '*.db-shm' \
    --exclude '.streamlit/secrets.toml' \
    --exclude '.sender_key' \
    --exclude 'backup_*' \
    "$SRC/" "$TARGET:/tmp/aisales-src/"

ssh -p "$PORT" "$TARGET" "sudo rsync -a --delete /tmp/aisales-src/ $APP_DIR/ && sudo chown -R aisales:aisales $APP_ROOT && rm -rf /tmp/aisales-src"

echo "==> Building the virtualenv on the server"
ssh -p "$PORT" "$TARGET" "sudo -u aisales python3 -m venv $APP_ROOT/venv 2>/dev/null || true; \
    sudo -u aisales $APP_ROOT/venv/bin/pip install -q --upgrade pip; \
    sudo -u aisales $APP_ROOT/venv/bin/pip install -q -r $APP_DIR/requirements.txt"

echo "==> Installing service + proxy config"
ssh -p "$PORT" "$TARGET" "\
    sudo cp $APP_DIR/deploy/aisales.service /etc/systemd/system/ && \
    sudo cp $APP_DIR/deploy/nginx-aisales.conf /etc/nginx/sites-available/aisales && \
    sudo ln -sf /etc/nginx/sites-available/aisales /etc/nginx/sites-enabled/aisales && \
    sudo rm -f /etc/nginx/sites-enabled/default && \
    sudo nginx -t && sudo systemctl reload nginx && \
    sudo systemctl daemon-reload && sudo systemctl enable aisales >/dev/null && \
    sudo systemctl restart aisales"

echo "==> Status"
ssh -p "$PORT" "$TARGET" "sudo systemctl is-active aisales && sudo systemctl --no-pager -l status aisales | head -12"

echo
echo "Done. Open  https://<server-ip>/   (accept the self-signed warning once)"
echo "If this is the first deploy, set the login password and secrets first —"
echo "see deploy/README.md."
