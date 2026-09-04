#!/usr/bin/env bash
# Ubuntu 24.04 hardening and setup for the liqfade VPS. Run once as root:
#
#   bash vps_bootstrap.sh [SSH_PORT]        # default 2222
#
# Idempotent: safe to run again after a change. Order matters. The SSH port is moved and
# the firewall is opened for it BEFORE ufw is enabled and BEFORE sshd is restarted, so a
# mistake cannot lock you out mid script. Keep your current session open until you have
# proved a new one works on the new port.
#
# It does not give the box access to a private repo. scripts/deploy.sh clones as the
# liqfade user, which is created here with no keys of its own, so a private repo needs a
# read-only deploy key at /home/liqfade/.ssh/id_ed25519 plus github.com in that user's
# known_hosts. Stage 8 of scripts/wizard_phase1_recorder.sh creates both.
set -euo pipefail

SSH_PORT="${1:-2222}"
SERVICE_USER="liqfade"
APP_ROOT="/srv/liqfade"

log() { printf '\n== %s\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root" >&2
  exit 1
fi
if ! [[ "$SSH_PORT" =~ ^[0-9]+$ ]] || [ "$SSH_PORT" -lt 1 ] || [ "$SSH_PORT" -gt 65535 ]; then
  echo "ssh port must be 1-65535, got '$SSH_PORT'" >&2
  exit 1
fi

# --- service user and layout -------------------------------------------------
# First, so that everything installed afterwards can be owned correctly.

log "service user and $APP_ROOT"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "/home/$SERVICE_USER" --shell /usr/sbin/nologin \
    "$SERVICE_USER"
fi
mkdir -p "$APP_ROOT"/{app,data,logs,reports}
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_ROOT"
chmod 750 "$APP_ROOT"
chmod 700 "$APP_ROOT/data"

# --- packages ----------------------------------------------------------------

log "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  ufw fail2ban unattended-upgrades chrony rsync git curl lz4 ca-certificates \
  logrotate build-essential

# --- uv for the service user --------------------------------------------------

log "uv for $SERVICE_USER"
if [ ! -x "/home/$SERVICE_USER/.local/bin/uv" ]; then
  # The installer writes into the invoking user's home, so run it as the service user.
  su -s /bin/bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh' "$SERVICE_USER"
fi

# --- tailscale ----------------------------------------------------------------
# Installed but deliberately not brought up: `tailscale up` needs an interactive auth
# URL, and bringing up a tunnel unattended is not something a bootstrap should decide.

log "tailscale"
if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi

# --- sshd ---------------------------------------------------------------------
# Written as a drop-in so an unattended-upgrades rewrite of sshd_config cannot silently
# re-enable password login.

log "sshd on port $SSH_PORT"
mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/10-liqfade.conf <<CONF
# Managed by scripts/vps_bootstrap.sh. Edit there, not here.
Port $SSH_PORT
PasswordAuthentication no
PermitRootLogin prohibit-password
PubkeyAuthentication yes
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
X11Forwarding no
MaxAuthTries 3
ClientAliveInterval 300
ClientAliveCountMax 2
AllowTcpForwarding yes
CONF

# Ubuntu 24.04 uses socket activation for ssh, which ignores the Port directive.
if systemctl list-unit-files ssh.socket >/dev/null 2>&1 && \
   systemctl is-enabled ssh.socket >/dev/null 2>&1; then
  mkdir -p /etc/systemd/system/ssh.socket.d
  cat > /etc/systemd/system/ssh.socket.d/10-liqfade.conf <<CONF
[Socket]
ListenStream=
ListenStream=$SSH_PORT
CONF
  systemctl daemon-reload
fi

log "validating sshd config before touching the running daemon"
sshd -t

# --- firewall -----------------------------------------------------------------
# Rules first, enable last. Both the new and the old port stay open until you confirm.

log "ufw"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow "$SSH_PORT/tcp" comment 'ssh (liqfade)'
ufw allow 22/tcp comment 'ssh (default, remove once the new port is proven)'
# Tailscale carries the panel and rsync; the panel itself only ever binds loopback.
ufw allow in on tailscale0 comment 'tailscale'
ufw allow 41641/udp comment 'tailscale direct'
ufw --force enable
ufw status verbose

log "restarting ssh"
if systemctl is-enabled ssh.socket >/dev/null 2>&1; then
  systemctl restart ssh.socket
fi
systemctl restart ssh

# --- fail2ban -----------------------------------------------------------------

log "fail2ban"
cat > /etc/fail2ban/jail.d/liqfade-sshd.local <<CONF
[sshd]
enabled = true
port    = $SSH_PORT
backend = systemd
maxretry = 4
findtime = 600
bantime  = 3600
CONF
systemctl enable --now fail2ban
systemctl restart fail2ban

# --- unattended upgrades and time ---------------------------------------------

log "unattended-upgrades and chrony"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
CONF
systemctl enable --now unattended-upgrades
# Correct clock is not cosmetic here: Hyperliquid nonces are millisecond timestamps and
# must be increasing, and every feature uses receive time.
systemctl enable --now chrony

# --- kernel and limits ---------------------------------------------------------

log "sysctl and file limits"
cat > /etc/sysctl.d/99-liqfade.conf <<'CONF'
# BBR plus fair queueing: steadier throughput and lower latency on the long haul to the
# Hyperliquid API than the default cubic plus pfifo_fast.
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
# Many concurrent websocket connections across five venues.
net.core.somaxconn = 1024
net.ipv4.tcp_fin_timeout = 20
CONF
sysctl --system >/dev/null

cat > /etc/security/limits.d/99-liqfade.conf <<CONF
$SERVICE_USER soft nofile 65536
$SERVICE_USER hard nofile 65536
CONF

# --- logrotate ------------------------------------------------------------------

log "logrotate"
cat > /etc/logrotate.d/liqfade <<CONF
$APP_ROOT/logs/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    su $SERVICE_USER $SERVICE_USER
}
CONF

# --- disk alert -----------------------------------------------------------------
# Writes a flag file the recorder picks up. Telegram wiring lands in Phase 1.

log "disk usage check"
cat > /usr/local/bin/liqfade-disk-check <<CONF
#!/usr/bin/env bash
set -euo pipefail
THRESHOLD=80
FLAG="$APP_ROOT/data/ALERT_DISK"
USED=\$(df --output=pcent "$APP_ROOT" | tail -1 | tr -dc '0-9')
if [ "\$USED" -ge "\$THRESHOLD" ]; then
  printf '%s disk %s%% used on %s\n' "\$(date -Is)" "\$USED" "$APP_ROOT" > "\$FLAG"
else
  rm -f "\$FLAG"
fi
CONF
chmod 755 /usr/local/bin/liqfade-disk-check
cat > /etc/cron.d/liqfade-disk <<CONF
# Managed by scripts/vps_bootstrap.sh
*/15 * * * * $SERVICE_USER /usr/local/bin/liqfade-disk-check
CONF
chmod 644 /etc/cron.d/liqfade-disk

# --- final ownership ------------------------------------------------------------

chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_ROOT"

cat <<CHECKLIST

================================ done ================================
Do these now, in this order, and do NOT close this session yet.

1. From your laptop, in a NEW terminal, prove the new port works:
       ssh -p $SSH_PORT <you>@<this host>
   Only once that succeeds, close this session.

2. Remove the old SSH port from the firewall:
       ufw delete allow 22/tcp

3. Bring up Tailscale and note the host name it prints:
       tailscale up --ssh=false

4. Put the environment file in place, readable by nobody else:
       install -o $SERVICE_USER -g $SERVICE_USER -m 0600 /dev/null $APP_ROOT/.env
       # then paste the values from .env.example into it. Never paste keys into chat.

5. Deploy the app and install the units. The repo is not on the box yet, and this
   script did not put it there, so pipe deploy.sh up from your laptop instead:
       ssh -p $SSH_PORT root@<host> "LIQFADE_REPO_URL=<your repo url> bash -s" < scripts/deploy.sh
   After the first deploy the clone is on the box, so the URL is no longer needed:
       ssh -p $SSH_PORT root@<host> 'bash -s' < scripts/deploy.sh
   It starts the recorder and the panel only. liqfade-live and liqfade-watchdog are
   installed but never enabled by deploy.sh.

Notes
  - The panel binds 127.0.0.1:8787 and nothing else, so it is reached through an SSH
    tunnel, which itself runs over Tailscale once that is up. Tailscale alone does not
    reach it: there is nothing listening on the tailnet address.
        ssh -p $SSH_PORT -L 8787:127.0.0.1:8787 <you>@<this host>
  - Nothing here starts trading. Live orders additionally need LIQFADE_ENV=mainnet,
    LIQFADE_LIVE_TRADING=1 and no $APP_ROOT/data/KILL file.
======================================================================
CHECKLIST
