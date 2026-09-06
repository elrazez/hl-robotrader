#!/usr/bin/env bash
# Deploy liqfade on the VPS. Run as root on the box, or over ssh:
#
#   ssh -p 2222 root@vps 'bash -s' < scripts/deploy.sh
#
# Pulls the repo, syncs dependencies from the lockfile, installs every systemd unit, and
# restarts only the units this phase runs whose file actually changed. Restarting the
# trading loop because the panel's unit was edited is exactly the kind of surprise this
# avoids, and so is enabling a unit whose code arrives two phases from now.
set -euo pipefail

REPO_URL="${LIQFADE_REPO_URL:-}"
BRANCH="${LIQFADE_BRANCH:-main}"
# Which units this deploy may enable and restart. Every unit file in scripts/systemd/ is
# installed either way; only these are started, because a unit whose code lands in a later
# phase would otherwise crash loop every RestartSec seconds from the first deploy on.
UNITS="${LIQFADE_UNITS:-liqfade-recorder liqfade-panel}"
# Never enabled here whatever UNITS says. Starting the trading loop is a decision the owner
# makes on the box, not a side effect of deploying a branch. liqfade-paper and
# liqfade-watchdog are absent from this list on purpose: paper places no real order,
# the watchdog only reads two files and posts, and both are what Phase 5 drills, so
# either can be deployed by naming it in LIQFADE_UNITS. Neither is in the default set.
NEVER_ENABLE="liqfade-live"
SERVICE_USER="liqfade"
APP_ROOT="/srv/liqfade"
# Where this deploy's checkout lives. The default is the one checkout everything has used
# so far; LIQFADE_APP_DIR puts a branch somewhere else entirely, which is how two branches
# run on one box without one quietly rolling back the other's code. When it is set, this
# deploy installs ONLY the units named in LIQFADE_UNITS and rewrites their paths to point
# at this directory, so it cannot touch a unit another checkout owns.
APP_DIR="${LIQFADE_APP_DIR:-$APP_ROOT/app}"
DEFAULT_APP_DIR="$APP_ROOT/app"
ISOLATED=0
if [ "$APP_DIR" != "$DEFAULT_APP_DIR" ]; then ISOLATED=1; fi
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
if [ "$ISOLATED" -eq 1 ] && [ -z "${LIQFADE_UNITS:-}" ]; then
  echo "LIQFADE_APP_DIR needs LIQFADE_UNITS: name the units this checkout owns." >&2
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
# Compare before copying so we know which services actually need a restart. Every unit
# file is installed; the enable and restart pass afterwards is the one that is selective.

# runnable NAME: is this unit one this deploy may start?
runnable() {
  local name="${1%.service}" candidate
  for candidate in $NEVER_ENABLE; do
    if [ "$candidate" = "$name" ]; then return 1; fi
  done
  for candidate in $UNITS; do
    if [ "$candidate" = "$name" ]; then return 0; fi
  done
  return 1
}

log "units"
if [ "$ISOLATED" -eq 1 ]; then
  echo "  isolated deploy from $APP_DIR: only $UNITS, paths rewritten"
fi
changed=()
staged="$(mktemp -d)"
trap 'rm -rf "$staged"' EXIT
for src in "$APP_DIR"/scripts/systemd/*.service; do
  name="$(basename "$src")"
  dest="$UNIT_DIR/$name"
  # An isolated deploy installs nothing it was not asked for. Installing every unit from
  # this checkout would hand another checkout's services this branch's ExecStart, which
  # nothing restarts today and everything picks up at the next reboot.
  if [ "$ISOLATED" -eq 1 ] && ! runnable "$name"; then
    echo "  skipped $name (not in LIQFADE_UNITS)"
    continue
  fi
  candidate="$src"
  if [ "$ISOLATED" -eq 1 ]; then
    candidate="$staged/$name"
    sed "s|$DEFAULT_APP_DIR|$APP_DIR|g" "$src" > "$candidate"
  fi
  if ! cmp -s "$candidate" "$dest"; then
    install -m 0644 "$candidate" "$dest"
    changed+=("$name")
    echo "  updated $name"
  else
    echo "  unchanged $name"
  fi
done

systemctl daemon-reload

log "enabling and restarting: $UNITS"
started=0
for name in "${changed[@]}"; do
  if ! runnable "$name"; then
    echo "  installed $name, left alone"
    continue
  fi
  systemctl enable "$name"
  systemctl restart "$name" || echo "  $name did not start, check: journalctl -u $name -n 50"
  started=$((started + 1))
done
if [ "$started" -eq 0 ]; then
  echo "  no unit of this phase changed, nothing restarted"
fi
echo "  left disabled, by design: $NEVER_ENABLE"

log "status"
systemctl --no-pager --lines=0 status \
  liqfade-recorder liqfade-paper liqfade-live liqfade-watchdog liqfade-panel 2>&1 \
  | grep -E 'liqfade|Active' || true

cat <<'NOTE'

Code changed but a unit did not? Restart the service yourself, on purpose:
    systemctl restart liqfade-recorder

liqfade-live and liqfade-watchdog are never enabled by this script. When Phase 5
gives them code, start them by hand on the box:
    systemctl enable --now liqfade-live

Live trading still needs LIQFADE_ENV=mainnet and LIQFADE_LIVE_TRADING=1 in
/srv/liqfade/.env and no /srv/liqfade/data/KILL file. Deploying does not enable it.
NOTE
