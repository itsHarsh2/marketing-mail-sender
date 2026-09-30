#!/usr/bin/env bash
# deploy-cpanel.sh — install AI Sales Machine on a shared/cPanel server
# WITHOUT root, WITHOUT touching Apache, and WITHOUT touching the firewall.
#
#     bash deploy/deploy-cpanel.sh USER@184.168.122.203 [ssh-port]
#
# WHY THIS IS DIFFERENT FROM provision.sh
#   That script assumed a blank Ubuntu VPS. This target is a live GoDaddy
#   cPanel server already running Apache and serving asapbeauty.in with a
#   valid certificate. Installing nginx there would fight Apache for ports
#   80/443, and enabling ufw would cut off cPanel, WHM, mail and every
#   existing site. So this script:
#     - installs entirely inside the user's home directory
#     - never installs system packages
#     - binds Streamlit to 127.0.0.1 only, reachable via an SSH tunnel
#     - leaves Apache, the firewall, and all existing sites untouched
#
# The Scraper Campaign needs Google Chrome, which cannot be installed without
# root. List Campaign, Replies, follow-ups and scheduling all work.

set -euo pipefail

TARGET="${1:-}"
PORT="${2:-22}"
if [ -z "$TARGET" ]; then
    echo "usage: bash deploy/deploy-cpanel.sh user@host [ssh-port]" >&2
    exit 1
fi

SRC="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_DIR="~/aisales"
SSH="ssh -p $PORT -o StrictHostKeyChecking=accept-new"

echo "==> 1/5  Checking the server"
$SSH "$TARGET" bash -s <<'PROBE'
set -e
echo "    user      : $(whoami)"
echo "    home      : $HOME"
echo "    os        : $(cat /etc/redhat-release 2>/dev/null || lsb_release -ds 2>/dev/null || uname -s)"
echo "    ram       : $(free -m 2>/dev/null | awk '/Mem:/{print $2" MB total, "$7" MB available"}' || echo unknown)"
echo "    disk free : $(df -h "$HOME" | awk 'NR==2{print $4}')"
for p in python3.12 python3.11 python3.10 python3.9 python3; do
    command -v $p >/dev/null 2>&1 && { echo "    python    : $p -> $($p -V 2>&1)"; break; }
done
command -v google-chrome >/dev/null 2>&1 \
    && echo "    chrome    : $(google-chrome --version)" \
    || echo "    chrome    : NOT INSTALLED (Scraper Campaign will be unavailable)"
echo "    root      : $([ "$(id -u)" -eq 0 ] && echo yes || echo 'no (user-level install)')"
PROBE

echo
echo "==> 2/5  Copying application files"
rsync -az --delete -e "$SSH" \
    --exclude '.venv/' --exclude '__pycache__/' --exclude 'logs/' \
    --exclude '.wdm/' --exclude '*.db' --exclude '*.db-wal' --exclude '*.db-shm' \
    --exclude '.streamlit/secrets.toml' --exclude '.sender_key' \
    --exclude 'backup_*' --exclude 'sales_campaign.backup_*' \
    "$SRC/" "$TARGET:aisales/"

echo "==> 3/5  Building the Python environment (this takes a few minutes)"
$SSH "$TARGET" bash -s <<'BUILD'
set -e
cd ~/aisales
PY=""
for p in python3.12 python3.11 python3.10 python3.9 python3; do
    command -v $p >/dev/null 2>&1 && { PY=$p; break; }
done
[ -z "$PY" ] && { echo "    !! no python3 found on this server"; exit 1; }
[ -d venv ] || $PY -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt
echo "    installed: $(./venv/bin/pip list 2>/dev/null | wc -l) packages"
BUILD

echo "==> 4/5  Installing the start/stop helpers"
$SSH "$TARGET" bash -s <<'HELPERS'
set -e
mkdir -p ~/aisales/logs
cat > ~/aisales/start.sh <<'EOF'
#!/usr/bin/env bash
# Start the app bound to localhost only. Reach it through an SSH tunnel.
cd ~/aisales
if pgrep -f "streamlit run app.py" >/dev/null 2>&1; then
    echo "already running (pid $(pgrep -f 'streamlit run app.py' | head -1))"; exit 0
fi
nohup ./venv/bin/streamlit run app.py \
    --server.address 127.0.0.1 \
    --server.port 8501 \
    --server.headless true \
    --browser.gatherUsageStats false \
    >> ~/aisales/logs/server.log 2>&1 &
sleep 4
pgrep -f "streamlit run app.py" >/dev/null && echo "started on 127.0.0.1:8501" \
    || { echo "failed to start — see ~/aisales/logs/server.log"; tail -20 ~/aisales/logs/server.log; }
EOF
cat > ~/aisales/stop.sh <<'EOF'
#!/usr/bin/env bash
pkill -f "streamlit run app.py" && echo "stopped" || echo "not running"
EOF
chmod +x ~/aisales/start.sh ~/aisales/stop.sh

# Survive a reboot without root/systemd.
CRON="@reboot ~/aisales/start.sh >/dev/null 2>&1"
( crontab -l 2>/dev/null | grep -vF "aisales/start.sh"; echo "$CRON" ) | crontab -
echo "    start.sh / stop.sh installed, @reboot cron set"
HELPERS

echo "==> 5/5  Remaining setup (must be done once, on the server)"
cat <<EOF

    ssh $TARGET
    cd ~/aisales

    # a) encryption key for saved SMTP passwords
    ./venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

    # b) login password hash
    ./venv/bin/python set_password.py

    # c) write both, plus your OpenAI key, into .streamlit/secrets.toml
    mkdir -p .streamlit && nano .streamlit/secrets.toml
    chmod 600 .streamlit/secrets.toml

    # d) start it
    ./start.sh

Then, FROM YOUR OWN MACHINE, open a tunnel and browse to it:

    ssh -N -L 8501:127.0.0.1:8501 $TARGET
    # leave that running, then open  http://localhost:8501

EOF
