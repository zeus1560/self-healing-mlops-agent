Oracle Cloud Free Tier — Self-Healing MLOps Agent deployment guide

Overview

This guide shows step-by-step how to provision an Oracle Cloud (OCI) ARM Ubuntu 22.04 instance, prepare the host, deploy the Self-Healing MLOps Agent environment with Docker Compose and the host-managed agent service, and configure basic firewall (iptables) rules suitable for a closed production environment.

Security note: Do NOT hardcode API keys or bot tokens into repo files. Use a .env file on the host (only readable by the deployment user) and pass secrets via env_file in docker-compose. The agent reads environment variables from /root/agent/.env when running under systemd (EnvironmentFile).

Prerequisites

- Oracle Cloud Free Tier account
- SSH key pair for instance access
- Basic familiarity with OCI console

0) Account signup & known gotchas (read this first)

- Sign up at https://www.oracle.com/cloud/free/ — requires a phone number and a credit card for identity verification, but the Always Free resources used here (Ampere A1 4 OCPU/24GB, block storage) are never billed as long as you stay within Always Free limits.
- **"Out of host capacity" error**: the Always Free Ampere A1 shape is extremely popular and many regions run out of capacity constantly. If instance creation fails with this error:
  - Try a different region at signup time (avoid the busiest ones; ap-chuncheon-1 (Korea) is frequently full — ap-osaka-1, ap-tokyo-1, or US/EU regions often have more headroom).
  - Retry creation periodically (capacity frees up as other free-tier users release instances), or split into two smaller shapes (e.g. 2 OCPU/12GB each) instead of one 4/24 instance — smaller requests succeed more often.
  - You cannot switch region after account creation without opening a new tenancy, so pick carefully.
- SSH key pair: OCI's instance-creation wizard can auto-generate one and hand you a `.pem` to download — take that if you don't already have a key. Otherwise generate your own on your laptop (`ssh-keygen -t ed25519 -f ~/.ssh/oracle_gx10 -N ""`) and paste the **public** key (`.pub`) into the "SSH keys" field during instance creation. Only the public key ever goes to Oracle; keep the private key file safe, `chmod 600` it.

1) Create an instance

- Image: Canonical Ubuntu 22.04 (ARM)
- Shape: Ampere A1 (4 OCPU, 24 GB RAM) or equivalent available in free tier
- Boot volume: choose at least 50GB if you will store logs / WAL files
- Networking: place instance in a VCN with a public subnet and an ephemeral public IP (or a NAT gateway + private subnet if you prefer)
- Add the SSH public key to the instance for access

1.5) Open ports at the cloud network level (VCN Security List) — easy to miss

`iptables` on the host (step 6 below) is only the **second** firewall layer. Oracle's Virtual Cloud Network has its own **Security List**, which by default only allows inbound SSH (22). Any port you open with `iptables` still gets dropped upstream unless you also add it here:

- OCI Console → Networking → Virtual Cloud Networks → (your VCN) → Security Lists → (the list attached to your subnet) → Ingress Rules → Add Ingress Rules
- Add one rule per port you need reachable from your admin IP (or `0.0.0.0/0` if you accept the risk), e.g. TCP 8501 (dashboard), TCP 8000 (approval-server), TCP 9000 (target-app). Leave the rest closed — you don't need to expose the dashboard/approval-server publicly if Telegram is your only interface.
- Only open what you actually need remote access to. SSH (22) restricted to your own IP is enough for day-to-day operation since Telegram handles alerts/approvals.

2) Initial host setup (run as root / sudo)

# update, install packages, enable swap if desired
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-venv python3-pip docker.io docker-compose sqlite3 git
sudo systemctl enable --now docker

# create user & workspace (if not using root)
sudo mkdir -p /root/agent && sudo chown $(whoami):$(whoami) /root/agent
cd /root/agent

# clone repo
git clone https://github.com/zeus1560/self-healing-mlops-agent.git .

# create python venv and install requirements
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

3) Prepare .env (secrets injection)

# copy example and edit
cp .env.example .env
# Edit .env and fill these values (do NOT commit .env):
# GROQ_API_KEY=...        # (optional) Groq API key for L2 inference
# TELEGRAM_BOT_TOKEN=...  # Bot token for Telegram alerts/approvals
# TELEGRAM_CHAT_ID=...    # Chat id (or ALLOWED_USER_ID alias)
# other values: SLACK_WEBHOOK_URL (fallback), AUTO_APPROVE, etc.

# secure the .env file
chmod 600 .env

4) Docker Compose (infrastructure)

# bring up infra containers
docker compose up -d

Notes:
- docker-compose loads environment from env_file: .env
- The approval-server (FastAPI) listens on port 8000. The dashboard runs on 8501. The simulated target app runs on 9000.
- The SQLite database (./data/agent_metrics.db) is mounted into containers that need it via the host filesystem (./data:/app/data). This keeps metrics persistent across container restarts.

5) Systemd host agent service

# install systemd unit
sudo cp deploy/self-healing-agent.service /etc/systemd/system/self-healing-agent.service
sudo systemctl daemon-reload
sudo systemctl enable --now self-healing-agent

# check logs
sudo journalctl -u self-healing-agent -f

6) Firewall (iptables) for OCI instance

The following is a minimal iptables rule set to allow only required inbound ports (SSH for admin, and optionally your dashboard/approval endpoints if you need remote access). Run as root.

# flush existing (caution: this will remove rules)
sudo iptables -F
sudo iptables -X
sudo iptables -t nat -F
sudo iptables -t nat -X
sudo iptables -t mangle -F
sudo iptables -t mangle -X

# default policy: deny incoming, allow outgoing, allow established
sudo iptables -P INPUT DROP
sudo iptables -P FORWARD DROP
sudo iptables -P OUTPUT ACCEPT

# allow loopback
sudo iptables -A INPUT -i lo -j ACCEPT

# allow established connections
sudo iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# allow SSH from admin (replace <ADMIN_IP> or remove to allow any)
# Recommended: restrict to management IP range
sudo iptables -A INPUT -p tcp -s <ADMIN_IP>/32 --dport 22 -j ACCEPT

# allow local dashboard access only (if you need external access, restrict to your IP)
# Uncomment to allow remote dashboard (8501) from admin IP only
# sudo iptables -A INPUT -p tcp -s <ADMIN_IP>/32 --dport 8501 -j ACCEPT

# allow approval server remote access if needed (careful!)
# sudo iptables -A INPUT -p tcp -s <ADMIN_IP>/32 --dport 8000 -j ACCEPT

# allow access to target-app if needed
# sudo iptables -A INPUT -p tcp -s <ADMIN_IP>/32 --dport 9000 -j ACCEPT

# Persist rules (Ubuntu): install iptables-persistent
sudo apt install -y iptables-persistent
sudo netfilter-persistent save

Security notes
- Keep .env readable only by the deployment user (chmod 600). Do not commit it.
- Prefer using TELEGRAM (secure, no public URL) for approvals rather than exposing approval-server publicly.
- If you must expose approval-server, secure it behind HTTPS + authentication; avoid open public endpoints.

7) Observability export

A helper script is included to export 90 days of metrics to CSV:

python scripts/export_metrics.py --days 90 --out ./data/metrics_90days.csv

8) Verification checklist
- docker compose up -d succeeded
- .env contains TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
- sudo systemctl status self-healing-agent reports active
- Test target app: curl http://localhost:9000/health
- Run demo/inject_failure.py (on host) to simulate failures and verify Telegram alerts

Appendix: iptables brief explanation
- The example rules above default-deny incoming traffic and only allow SSH from a management IP.
- Adjust allowed ports and source IPs to your operational requirements.

If you'd like, I can produce a small shell script to automate steps 2–6 (instance bootstrap) that you can run in cloud-init or after SSH-ing into the instance.