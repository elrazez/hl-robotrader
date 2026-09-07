"""A deploy writes only the systemd units belonging to the checkout it deploys.

`scripts/deploy.sh` used to install every unit file it shipped whenever it ran from the
default directory, so deploying a recorder branch to /srv/liqfade/app rewrote
liqfade-paper and liqfade-watchdog, which run from the isolated /srv/liqfade/paper
checkout, to point at app. Nothing restarts them, so the running soak was untouched and
would have come up on the wrong code at the next reboot.

`LIQFADE_PLAN_ONLY=1` prints the plan and exits before the script needs root or a box, so
the rule is testable on a laptop.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "scripts" / "deploy.sh"
SHIPPED = ("recorder", "panel", "paper", "watchdog", "live")


def checkout(root: Path, name: str) -> Path:
    """A checkout shipping the same unit files the repo does, all naming the default dir."""
    app = root / name
    (app / "scripts" / "systemd").mkdir(parents=True)
    for unit in SHIPPED:
        (app / "scripts" / "systemd" / f"liqfade-{unit}.service").write_text(
            f"[Service]\nWorkingDirectory={root}/app\nExecStart={root}/app/.venv/bin/liqfade\n"
        )
    return app


def install(unit_dir: Path, unit: str, workdir: Path) -> None:
    """Put a unit on the box already running from `workdir`."""
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / f"liqfade-{unit}.service").write_text(f"[Service]\nWorkingDirectory={workdir}\n")


def plan(root: Path, app_dir: Path, unit_dir: Path, units: str | None = None) -> dict[str, str]:
    """unit name without its prefix or suffix -> "install" or "skip"."""
    env = os.environ | {
        "LIQFADE_PLAN_ONLY": "1",
        "LIQFADE_APP_ROOT": str(root),
        "LIQFADE_APP_DIR": str(app_dir),
        "LIQFADE_UNIT_DIR": str(unit_dir),
    }
    env.pop("LIQFADE_UNITS", None)
    if units is not None:
        env["LIQFADE_UNITS"] = units
    result = subprocess.run(
        ["bash", str(DEPLOY)], capture_output=True, text=True, check=True, env=env
    )
    decided = {}
    for line in result.stdout.splitlines():
        verb, name = line.split(" ")[:2]
        decided[name.removeprefix("liqfade-").removesuffix(".service")] = verb
    return decided


@pytest.fixture
def box(tmp_path: Path) -> Path:
    """A box with an app checkout, a paper checkout, and units installed from each."""
    checkout(tmp_path, "app")
    checkout(tmp_path, "paper")
    units = tmp_path / "etc"
    for unit in ("recorder", "panel", "live"):
        install(units, unit, tmp_path / "app")
    for unit in ("paper", "watchdog"):
        install(units, unit, tmp_path / "paper")
    return tmp_path


def test_a_default_deploy_leaves_the_isolated_checkouts_units_alone(box: Path) -> None:
    """The regression: liqfade-paper and liqfade-watchdog run from the paper checkout."""
    decided = plan(box, box / "app", box / "etc")
    assert decided["paper"] == "skip"
    assert decided["watchdog"] == "skip"


def test_a_default_deploy_still_writes_the_units_it_owns(box: Path) -> None:
    """Including liqfade-live, which is installed ready to be started by hand and is the
    reason the default deploy writes more than the units it restarts."""
    decided = plan(box, box / "app", box / "etc")
    assert decided["recorder"] == "install"
    assert decided["panel"] == "install"
    assert decided["live"] == "install"


def test_an_isolated_deploy_writes_only_what_it_was_asked_for(box: Path) -> None:
    decided = plan(box, box / "paper", box / "etc", units="liqfade-paper liqfade-watchdog")
    assert decided["paper"] == "install"
    assert decided["watchdog"] == "install"
    assert decided["recorder"] == "skip"
    assert decided["panel"] == "skip"
    assert decided["live"] == "skip"


def test_naming_a_unit_another_checkout_owns_claims_it(box: Path) -> None:
    """Ownership is a guard against a silent rewrite, never against a deliberate one:
    moving the paper loop back onto the app checkout has to stay possible."""
    decided = plan(box, box / "app", box / "etc", units="liqfade-recorder liqfade-paper")
    assert decided["paper"] == "install"


def test_a_fresh_box_gets_every_unit_from_the_default_checkout(tmp_path: Path) -> None:
    """Nothing is installed, so nothing is owned, and the first deploy lands them all."""
    checkout(tmp_path, "app")
    decided = plan(tmp_path, tmp_path / "app", tmp_path / "etc")
    assert set(decided.values()) == {"install"}
    assert len(decided) == len(SHIPPED)


def test_the_live_unit_is_never_in_the_units_a_deploy_starts() -> None:
    """Ownership decides what is written; NEVER_ENABLE still decides what is started, and
    the trading loop is not something a deploy turns on."""
    assert 'NEVER_ENABLE="liqfade-live"' in DEPLOY.read_text()
