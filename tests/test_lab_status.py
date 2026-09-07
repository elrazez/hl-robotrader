"""The Status tab: read-only, and honest about there being nothing to show."""

from __future__ import annotations

from pathlib import Path

from liqfade.control.protocol import FeedState, RiskState, StateSnapshot, now_ms
from liqfade.control.state import write_state
from liqfade.lab.app import LabApp
from liqfade.lab.status import read_status


def snapshot(**over: object) -> StateSnapshot:
    fields: dict[str, object] = {
        "proc": "recorder",
        "env": "paper",
        "uptime_s": 3600.0,
        "risk": RiskState(paused=False, equity=10000.0),
        "feeds": [
            FeedState(name="hyperliquid.trades", connected=True, last_msg_ms=now_ms(), lag_ms=40),
            FeedState(name="bybit.liquidations", connected=False, last_msg_ms=0, lag_ms=99000),
        ],
    }
    fields.update(over)
    return StateSnapshot(**fields)  # type: ignore[arg-type]


def test_no_state_file_is_reported_as_nothing_running(tmp_path: Path) -> None:
    view = read_status(tmp_path / "state.json")
    assert view.running is False
    assert view.state is None
    assert "no live or recorder process" in view.message


def test_a_fresh_snapshot_is_a_running_process(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    write_state(path, snapshot())
    view = read_status(path)
    assert view.running is True
    assert view.state is not None
    assert view.state.proc == "recorder"
    assert view.age_s is not None and view.age_s < 5


def test_a_stale_snapshot_is_not_a_running_process(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    write_state(path, snapshot(ts_ms=now_ms() - 600_000))
    view = read_status(path, stale_s=10.0)
    assert view.running is False
    assert view.state is not None, "the last thing it said is still worth showing"
    assert "10:00" in view.message


def test_an_unreadable_snapshot_says_so_rather_than_raising(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json")
    view = read_status(path)
    assert view.running is False
    assert view.state is None
    assert "could not be read" in view.message


async def test_the_status_tab_says_plainly_when_nothing_is_running(tmp_path: Path) -> None:
    app = LabApp(config_dir=tmp_path, reports_dir=tmp_path / "reports", data_dir=tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("4")
        await pilot.pause()
        shown = str(app.query_one("#status-body").content)
        assert "no live or recorder process" in shown


async def test_the_status_tab_shows_the_process_and_its_feeds(tmp_path: Path) -> None:
    write_state(tmp_path / "state.json", snapshot())
    app = LabApp(config_dir=tmp_path, reports_dir=tmp_path / "reports", data_dir=tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("4")
        await pilot.pause()
        shown = str(app.query_one("#status-body").content)
        assert "recorder" in shown
        assert "hyperliquid.trades" in shown
        assert "DOWN" in shown, "a disconnected feed has to be visible at a glance"


async def test_the_status_tab_writes_nothing(tmp_path: Path) -> None:
    """Read-only means read-only: opening the tab must not create data/ or touch state."""
    data = tmp_path / "data"
    app = LabApp(config_dir=tmp_path, reports_dir=tmp_path / "reports", data_dir=data)
    async with app.run_test() as pilot:
        await pilot.press("4")
        await pilot.pause()
    assert not data.exists()
