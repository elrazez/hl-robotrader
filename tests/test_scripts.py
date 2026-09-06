"""Static checks on the shell scripts and systemd units.

These run offline. They do not prove the bootstrap works on a real VPS, which only the
owner can do once the box exists; they catch the mistakes that are cheap to catch here:
a unit missing a hardening directive, a service pointing at the wrong path, the live unit
losing its kill switch guard.
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EM_DASH = "\u2014"  # spelled as an escape so this file does not trip its own check
UNIT_DIR = ROOT / "scripts" / "systemd"
UNITS = sorted(UNIT_DIR.glob("*.service"))

REQUIRED_SERVICE_KEYS = {
    "User": "liqfade",
    "EnvironmentFile": "/srv/liqfade/.env",
    "WorkingDirectory": "/srv/liqfade/app",
    "Restart": "always",
    "RestartSec": "3",
    "NoNewPrivileges": "yes",
    "ProtectSystem": "strict",
    "ProtectHome": "yes",
    "PrivateTmp": "yes",
    "ReadWritePaths": "/srv/liqfade/data /srv/liqfade/logs",
    "LimitNOFILE": "65536",
}


def _parse(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False)
    parser.optionxform = str  # systemd keys are case sensitive
    parser.read_string(path.read_text())
    return parser


def test_every_unit_exists() -> None:
    assert {p.name for p in UNITS} == {
        "liqfade-recorder.service",
        "liqfade-paper.service",
        "liqfade-live.service",
        "liqfade-watchdog.service",
        "liqfade-panel.service",
    }


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_unit_has_the_required_hardening(unit: Path) -> None:
    service = _parse(unit)["Service"]
    for key, expected in REQUIRED_SERVICE_KEYS.items():
        assert service.get(key) == expected, f"{unit.name}: {key}"


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_unit_starts_from_the_synced_venv_not_through_uv(unit: Path) -> None:
    # uv run needs a writable cache under /home on every start; the sandbox has none.
    exec_start = _parse(unit)["Service"]["ExecStart"]
    assert exec_start.startswith("/srv/liqfade/app/.venv/bin/")
    assert "uv run" not in exec_start


@pytest.mark.parametrize("name", ["liqfade-live.service", "liqfade-paper.service"])
def test_a_trading_unit_refuses_to_start_while_the_kill_flag_exists(name: str) -> None:
    """Paper places no real order and still gates on the flag: a kill some units ignore
    is not a kill switch, and the drill has to exercise the live unit's own path."""
    service = _parse(UNIT_DIR / name)["Service"]
    assert service["ExecStartPre"] == "/usr/bin/test ! -e /srv/liqfade/data/KILL"


def test_the_paper_unit_has_its_own_socket_and_state_file() -> None:
    """Three units on one box, and the control server takes an exclusive lock per socket."""
    text = (UNIT_DIR / "liqfade-paper.service").read_text()
    assert "LIQFADE_CONTROL__SOCKET_PATH=control-paper.sock" in text
    assert "LIQFADE_CONTROL__STATE_FILE=state-paper.json" in text


def test_watchdog_does_not_depend_on_the_process_it_watches() -> None:
    """A watchdog that dies with the loop is not a watchdog."""
    unit = _parse(UNIT_DIR / "liqfade-watchdog.service")["Unit"]
    joined = " ".join(unit.values())
    assert "liqfade-live" not in joined


def test_deploy_syncs_the_panel_extra_the_panel_unit_relies_on() -> None:
    # The unit no longer passes --extra panel itself, so the sync has to.
    deploy = (UNIT_DIR.parent / "deploy.sh").read_text()
    assert "sync --frozen --extra panel" in deploy


@pytest.mark.parametrize(
    "script", [ROOT / "scripts" / "vps_bootstrap.sh", ROOT / "scripts" / "deploy.sh"]
)
def test_shell_scripts_are_executable_and_strict(script: Path) -> None:
    text = script.read_text()
    assert script.stat().st_mode & 0o111, f"{script.name} is not executable"
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text


def test_bootstrap_opens_the_firewall_before_enabling_it() -> None:
    """Enabling ufw with default deny before allowing the SSH port locks the owner out."""
    text = (ROOT / "scripts" / "vps_bootstrap.sh").read_text()
    allow_new_port = text.index('ufw allow "$SSH_PORT/tcp"')
    allow_old_port = text.index("ufw allow 22/tcp")
    enable = text.index("ufw --force enable")
    assert allow_new_port < enable
    assert allow_old_port < enable  # the old port stays open until the owner proves the new one


def test_bootstrap_validates_sshd_before_restarting_it() -> None:
    text = (ROOT / "scripts" / "vps_bootstrap.sh").read_text()
    assert text.index("sshd -t") < text.index("systemctl restart ssh")


def test_bootstrap_does_not_bring_tailscale_up_by_itself() -> None:
    text = (ROOT / "scripts" / "vps_bootstrap.sh").read_text()
    # It may print the command in the checklist, but must not run it.
    assert "\ntailscale up" not in text
    assert "  tailscale up" in text or "       tailscale up" in text


def test_deploy_installs_every_unit_before_it_enables_any() -> None:
    """Install, then daemon-reload, then start. Restarting a unit systemd has not
    re-read gives you the old ExecStart with the new code."""
    text = (ROOT / "scripts" / "deploy.sh").read_text()
    install = text.index('install -m 0644 "$candidate" "$dest"')
    reload_units = text.index("systemctl daemon-reload")
    enable = text.index('systemctl enable "$name"')
    assert install < reload_units < enable


def test_deploy_enables_only_the_units_this_phase_runs() -> None:
    """A unit whose code arrives in a later phase crash loops from the first deploy on."""
    text = (ROOT / "scripts" / "deploy.sh").read_text()
    assert 'UNITS="${LIQFADE_UNITS:-liqfade-recorder liqfade-panel}"' in text
    # Only the mainnet loop is denied outright now. paper and the watchdog have code
    # behind them as of 05a, place no real order between them, and are what Phase 5
    # drills, so LIQFADE_UNITS may name either. Neither is in the default set.
    assert 'NEVER_ENABLE="liqfade-live"' in text
    assert "liqfade-paper" not in text.split("UNITS=")[1].split("\n")[0]
    # The denylist is consulted first, so LIQFADE_UNITS cannot talk the script into
    # starting the trading loop.
    assert text.index("for candidate in $NEVER_ENABLE") < text.index("for candidate in $UNITS")
    assert text.index('if ! runnable "$name"') < text.index('systemctl enable "$name"')


def test_deploy_never_starts_the_mainnet_trading_loop() -> None:
    """It may print the command in the closing note, but must never run it."""
    text = (ROOT / "scripts" / "deploy.sh").read_text()
    for unit in ("liqfade-live",):
        for command in ("systemctl enable", "systemctl enable --now", "systemctl start"):
            assert f"\n{command} {unit}" not in text
    assert "    systemctl enable --now liqfade-live" in text  # the note says how, by hand


def test_deploy_says_what_it_left_disabled() -> None:
    """Silence about a unit it deliberately skipped reads as a unit that failed."""
    text = (ROOT / "scripts" / "deploy.sh").read_text()
    assert 'echo "  left disabled, by design: $NEVER_ENABLE"' in text


def test_no_em_dashes_anywhere_in_the_repo_text() -> None:
    """Owner instruction: no em dashes in code comments, docs or output."""
    roots = [ROOT / "liqfade", ROOT / "scripts", ROOT / "docs", ROOT / "tests", ROOT / "config"]
    offenders = []
    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file() or "node_modules" in path.parts or "static" in path.parts:
                continue
            if path.suffix not in {".py", ".sh", ".md", ".yaml", ".yml", ".service", ".ts", ".tsx"}:
                continue
            if EM_DASH in path.read_text(errors="ignore"):
                offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"em dash found in: {offenders}"


def test_deploy_can_put_a_branch_in_its_own_directory() -> None:
    """Two branches on one box, without either rolling back the other's code.

    The recorder runs from its own checkout on its own branch. A deploy of this branch
    into the same directory would hand it code from before its fixes, which nothing
    restarts today and everything picks up at the next reboot.
    """
    text = (ROOT / "scripts" / "deploy.sh").read_text()
    assert 'APP_DIR="${LIQFADE_APP_DIR:-$APP_ROOT/app}"' in text
    # An isolated deploy has to name what it owns, install only that, and rewrite the
    # unit paths to its own directory.
    assert "LIQFADE_APP_DIR needs LIQFADE_UNITS" in text
    assert 'if [ "$ISOLATED" -eq 1 ] && ! runnable "$name"; then' in text
    assert 'sed "s|$DEFAULT_APP_DIR|$APP_DIR|g"' in text


def test_deploy_is_valid_bash() -> None:
    import subprocess

    subprocess.run(
        ["bash", "-n", str(ROOT / "scripts" / "deploy.sh")], check=True, capture_output=True
    )


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_environment_values_with_spaces_are_quoted(unit: Path) -> None:
    """systemd drops `Environment=K=two words` with a parse warning and carries on.

    The watchdog shipped with `Environment=LIQFADE_WATCH_LABEL=paper loop` and the box
    logged "Invalid environment assignment, ignoring: loop" while starting anyway, so the
    label silently fell back to the default. A unit that is half applied is worse than one
    that refuses.
    """
    for line in unit.read_text().splitlines():
        if not line.startswith("Environment="):
            continue
        value = line.removeprefix("Environment=")
        if " " in value:
            assert value.startswith('"') and value.endswith('"'), line
