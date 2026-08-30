#!/usr/bin/env bash
# Deploy liqfade on the VPS. Run as root on the box, or over ssh:
#
#   ssh -p 2222 root@vps 'bash -s' < scripts/deploy.sh
#
# Pulls the repo, syncs dependencies from the lockfile, installs the systemd units, and
# restarts only the services whose unit file actually changed. Restarting the trading
# loop because the panel's unit was edited is exactly the kind of surprise this avoids.
set -euo pipefail

REPO_URL="${LIQFADE_REPO_URL:-}"
BRANCH="${LIQFADE_BRANCH:-main}"
SERVICE_USER="liqfade"
APP_ROOT="/srv/liqfade"
APP_DIR="$APP_ROOT/app"
UNIT_DIR="/etc/systemd/system"
UV="/home/$SERVICE_USER/.local/bin/uv"

log() { printf '\n== %s\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root" >&2
  exit 1
fi
if [ ! -x "$UV" ]; then
  echo "uv not found at $UV. Run scripts/vps_bootstrap.sh first." >&2
  exit 1
fi
if [ ! -f "$APP_ROOT/.env" ]; then
  echo "missing $APP_ROOT/.env (mode 0600, owned by $SERVICE_USER). See .env.example." >&2
  exit 1
fi

# --- code ----------------------------------------------------------------------

log "code"
if [ -d "$APP_DIR/.git" ]; then
  su -s /bin/bash -c "cd '$APP_DIR' && git fetch --quiet origin && \
    git checkout --quiet '$BRANCH' && git reset --hard --quiet 'origin/$BRANCH'" "$SERVICE_USER"
else
  if [ -z "$REPO_URL" ]; then
    echo "first deploy needs LIQFADE_REPO_URL=git@github.com:you/hl-robotrader.git" >&2
    exit 1
  fi
  rm -rf "$APP_DIR"
  su -s /bin/bash -c "git clone --quiet --branch '$BRANCH' '$REPO_URL' '$APP_DIR'" "$SERVICE_USER"
fi
su -s /bin/bash -c "cd '$APP_DIR' && git log -1 --oneline" "$SERVICE_USER"

log "dependencies"
su -s /bin/bash -c "cd '$APP_DIR' && '$UV' sync --frozen --extra panel" "$SERVICE_USER"

# --- units ----------------------------------------------------------------------
# Compare before copying so we know which services actually need a restart.

log "units"
changed=()
for src in "$APP_DIR"/scripts/systemd/*.service; do
  name="$(basename "$src")"
  dest="$UNIT_DIR/$name"
  if ! cmp -s "$src" "$dest"; then
    install -m 0644 "$src" "$dest"
    changed+=("$name")
    echo "  updated $name"
  else
    echo "  unchanged $name"
  fi
done

systemctl daemon-reload

if [ ${#changed[@]} -eq 0 ]; then
  echo "  no unit changed, nothing restarted"
else
  log "restarting changed units"
  for name in "${changed[@]}"; do
    # liqfade-live refuses to start while data/KILL exists; that is the intended behaviour.
    systemctl enable "$name"
    systemctl restart "$name" || echo "  $name did not start, check: journalctl -u $name -n 50"
  done
fi

log "status"
systemctl --no-pager --lines=0 status \
  liqfade-recorder liqfade-live liqfade-watchdog liqfade-panel 2>&1 | grep -E 'liqfade|Active' || true

cat <<'NOTE'

Code changed but a unit did not? Restart the service yourself, on purpose:
    systemctl restart liqfade-recorder
    systemctl restart liqfade-live

Live trading still needs LIQFADE_ENV=mainnet and LIQFADE_LIVE_TRADING=1 in
/srv/liqfade/.env and no /srv/liqfade/data/KILL file. Deploying does not enable it.
NOTE
