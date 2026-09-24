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
# Overridable only so the unit plan below can be tested against temp directories; the box
# never sets either of these.
APP_ROOT="${LIQFADE_APP_ROOT:-/srv/liqfade}"
# Where this deploy's checkout lives. The default is the one checkout everything has used
# so far; LIQFADE_APP_DIR puts a branch somewhere else entirely, which is how two branches
# run on one box without one quietly rolling back the other's code. When it is set, this
# deploy installs ONLY the units named in LIQFADE_UNITS and rewrites their paths to point
# at this directory, so it cannot touch a unit another checkout owns.
APP_DIR="${LIQFADE_APP_DIR:-$APP_ROOT/app}"
DEFAULT_APP_DIR="$APP_ROOT/app"
ISOLATED=0
if [ "$APP_DIR" != "$DEFAULT_APP_DIR" ]; then ISOLATED=1; fi
UNIT_DIR="${LIQFADE_UNIT_DIR:-/etc/systemd/system}"
UV="/home/$SERVICE_USER/.local/bin/uv"

log() { printf '\n== %s\n' "$*"; }

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

# owner_of DEST: the checkout the installed unit runs from, empty when it is not installed.
owner_of() {
  [ -f "$1" ] || return 0
  sed -n 's|^WorkingDirectory=||p' "$1" | head -1
}

# plan_units: one line per unit this checkout ships, "install NAME" or "skip NAME REASON".
# The whole ownership rule lives here and nowhere else.
#
# A unit belongs to the checkout its installed WorkingDirectory names. Writing one that
# belongs to another checkout is how a deploy from /srv/liqfade/app silently repointed
# liqfade-paper and liqfade-watchdog, which run from /srv/liqfade/paper, at app's
# ExecStart. Nothing restarts them, so the paper soak kept running and would have come up
# on the wrong code at the next reboot: a rollback nobody would have connected to a
# deploy of a different branch days earlier.
#
# A unit named in LIQFADE_UNITS is always written, because naming it is how a checkout
# claims it. A unit nobody owns yet is written by the default checkout and skipped by an
# isolated one, which is what puts liqfade-live on a fresh box ready to be started by
# hand without an isolated branch deploy dragging it in.
plan_units() {
  local src name owner
  for src in "$APP_DIR"/scripts/systemd/*.service; do
    [ -e "$src" ] || continue
    name="$(basename "$src")"
    owner="$(owner_of "$UNIT_DIR/$name")"
    if runnable "$name"; then
      echo "install $name"
    elif [ -n "$owner" ] && [ "$owner" != "$APP_DIR" ]; then
      echo "skip $name owned by $owner"
    elif [ "$ISOLATED" -eq 1 ]; then
      echo "skip $name not in LIQFADE_UNITS"
    else
      echo "install $name"
    fi
  done
}

# The plan needs no privileges and changes nothing, so it is answerable before the root
# check: LIQFADE_PLAN_ONLY=1 prints what this deploy would write and exits, which is what
# tests/test_deploy_units.py asserts against.
if [ -n "${LIQFADE_PLAN_ONLY:-}" ]; then
  plan_units
  exit 0
fi

# Retired 2026-09-25: the owner hand-tunes the box, and a run of this script would overwrite
# that work. Only the owner runs it, by hand, with LIQFADE_OWNER_RUN=1 (CLAUDE.md section 6).
if [ "${LIQFADE_OWNER_RUN:-}" != "1" ]; then
  echo "refused: the owner manages the VPS by hand. Owner only: rerun with LIQFADE_OWNER_RUN=1." >&2
  exit 1
fi

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
# `plan_units` decides what this checkout may write; this loop only carries it out, and
# compares before copying so we know which services actually need a restart. Every unit
# written has its paths rewritten to this checkout, which for the default one is a no-op,
# so an installed unit always names the directory that installed it.

log "units"
if [ "$ISOLATED" -eq 1 ]; then
  echo "  isolated deploy from $APP_DIR: only $UNITS, paths rewritten"
fi
changed=()
staged="$(mktemp -d)"
trap 'rm -rf "$staged"' EXIT
# `< <(...)` and not a pipe: a pipe puts the loop in a subshell and `changed` comes back
# empty, so nothing would ever be restarted.
while read -r verb name reason; do
  if [ "$verb" != "install" ]; then
    echo "  skipped $name ($reason)"
    continue
  fi
  dest="$UNIT_DIR/$name"
  candidate="$staged/$name"
  sed "s|$DEFAULT_APP_DIR|$APP_DIR|g" "$APP_DIR/scripts/systemd/$name" > "$candidate"
  if ! cmp -s "$candidate" "$dest"; then
    install -m 0644 "$candidate" "$dest"
    changed+=("$name")
    echo "  updated $name"
  else
    echo "  unchanged $name"
  fi
done < <(plan_units)

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
