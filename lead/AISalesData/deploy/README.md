# Deploying AI Sales Machine

Written for: whoever is running the deployment (you, on your own VPS).

Target setup — chosen for this deployment:

- **Access:** password login inside the app, behind nginx with a self-signed cert
- **Address:** IP only, no domain
- **Data:** fresh database on the server; local data stays on your machine

---

## What you need before starting

| Item | Why |
|---|---|
| VPS IP, SSH user, port | to connect |
| sudo on the server | installs packages, systemd unit, nginx |
| **2 GB RAM minimum** | Chrome + Selenium needs ~1 GB alone during a scrape |
| Ubuntu/Debian | `provision.sh` uses `apt` |
| Your OpenAI key | AI-written emails |
| Your SMTP account passwords | re-entered on the server (fresh start) |

Under 2 GB of RAM, List Campaigns and Replies work fine but **the Scraper
Campaign will be killed mid-run by the OOM killer**.

---

## Steps

### 1. Provision the server

```bash
scp deploy/provision.sh user@SERVER:/tmp/
ssh user@SERVER "sudo bash /tmp/provision.sh"
```

Installs Python, Chrome, nginx, a self-signed cert for the server's IP, a
firewall, and the `aisales` service account. Port 8501 is **not** opened —
Streamlit binds to localhost and is reachable only through the TLS proxy.

### 2. Push the code

```bash
bash deploy/push.sh user@SERVER          # add a port as the 2nd argument
```

Syncs the source, builds the venv on the server, installs the systemd unit
and nginx config, and starts the service. Secrets and the database are
deliberately excluded.

### 3. Create the secrets file on the server

```bash
ssh user@SERVER
sudo -u aisales mkdir -p /opt/aisales/app/.streamlit
sudo -u aisales nano /opt/aisales/app/.streamlit/secrets.toml
```

```toml
openai_api_key        = "sk-proj-..."
sender_encryption_key = "<generate below>"
app_password_hash     = "<generate below>"
```

Generate the encryption key (a **new** one — a fresh database means nothing
is encrypted with the old key yet):

```bash
sudo -u aisales /opt/aisales/venv/bin/python -c \
  "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Generate the login password hash:

```bash
cd /opt/aisales/app
sudo -u aisales /opt/aisales/venv/bin/python set_password.py
```

Lock the file down, then restart:

```bash
sudo chmod 600 /opt/aisales/app/.streamlit/secrets.toml
sudo systemctl restart aisales
```

The app **refuses to serve** until `app_password_hash` is set — it fails
closed rather than starting wide open.

### 4. Open it

`https://SERVER-IP/` — accept the certificate warning once (expected with a
self-signed cert; the connection is still encrypted).

Then, inside the app:

1. **⚙️ Manage Senders** — add your SMTP accounts and **💾 Save accounts**
   so scheduled sends can authenticate later
2. **🔌 Test All Connections** to confirm SMTP works from the server's IP
3. **📥 Replies** — check the IMAP host per account is right

---

## Day-to-day

```bash
sudo systemctl status aisales        # is it up
sudo journalctl -u aisales -f        # live logs
sudo systemctl restart aisales       # after editing secrets
bash deploy/push.sh user@SERVER      # deploy code changes
```

Application logs also land in `/opt/aisales/app/logs/app.log`.

---

## Things worth knowing

**Scheduled follow-ups now actually work.** The scheduler runs inside the app
process, so it only fires while the app is up. Under systemd it always is —
which was not true running it locally.

**Chrome memory.** Watch `free -h` during your first scrape. If the service
dies mid-scrape, that's the OOM killer; add swap or a smaller
`Max Scrolls`.

**Back up the database.** Everything lives in
`/opt/aisales/app/sales_campaign.db`:

```bash
sudo -u aisales cp /opt/aisales/app/sales_campaign.db \
  /opt/aisales/backup-$(date +%F).db
```

**Never lose `sender_encryption_key`.** Saved SMTP passwords are encrypted
with it. Lose it and every account must be re-entered. Keep a copy somewhere
outside the server.

**The login is one shared password**, not user accounts. Anyone who has it
can send mail from your accounts and read every lead. Treat it accordingly,
and switch to Let's Encrypt as soon as a domain points at the box.
