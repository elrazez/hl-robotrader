"""The recorder's control backend, over a real Unix socket."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from liqfade.config import load_settings
from liqfade.control import ControlClient, ControlError, ControlServer
from liqfade.control.protocol import FeedState, ReloadResult, StateSnapshot
from liqfade.recorder.backend import LatencyTracker, RecorderBackend

FEEDS = [
    FeedState(
        name="hyperliquid.trades",
        connected=True,
        last_msg_ms=1788099600000,
        lag_ms=120,
        msgs_1m=900,
    )
]


@pytest.fixture
async def wired(sock_dir: Path) -> AsyncIterator[tuple[RecorderBackend, ControlClient]]:
    settings = load_settings("paper", environ={"LIQFADE_PATHS__DATA_DIR": str(sock_dir)})
    latency = LatencyTracker()
    latency.add_ws_ping(11.0)
    latency.add_rest(120.0)
    backend = RecorderBackend(
        settings,
        feeds=lambda _at: FEEDS,
        universe=lambda: ["BTC", "ETH"],
        latency=latency,
        counters={"rows_written": 5},
        stop=asyncio.Event(),
        reload=lambda: ReloadResult(changed=["recorder.gap_alert_s"]),
    )
    server = ControlServer(backend, sock_dir / "control.sock")
    await server.start()
    try:
        yield backend, ControlClient(server.socket_path)
    finally:
        await server.stop()


async def test_status_reports_real_feeds_and_the_universe(wired) -> None:
    _, client = wired
    state = await client.status()
    assert isinstance(state, StateSnapshot)
    assert state.proc == "recorder"
    assert state.live_trading is False
    assert [feed.name for feed in state.feeds] == ["hyperliquid.trades"]
    assert state.universe == ["BTC", "ETH"]
    assert state.latency.ws_ping_p50 == 11.0
    assert state.latency.rest_p50 == 120.0


@pytest.mark.parametrize("cmd", ["pause", "resume", "flat"])
async def test_the_trading_commands_are_refused_rather_than_faked(wired, cmd: str) -> None:
    """A recorder that answered "ok" to /flat would read as safe and would not be."""
    _, client = wired
    with pytest.raises(ControlError, match="not supported by recorder"):
        await client.send(cmd)  # type: ignore[arg-type]


async def test_kill_stops_the_recorder(wired) -> None:
    backend, client = wired
    result = await client.kill("test")
    assert result.killed is True
    assert backend._stop.is_set()
    assert (await client.status()).risk.killed is True


async def test_kill_writes_the_flag_file(wired) -> None:
    """The durable half of a kill. The live unit is Restart=always and gates on this file,

    so a kill that only stopped the process would be undone three seconds later.
    """
    backend, client = wired
    kill_file = backend.settings.kill_file
    assert not kill_file.exists()

    await client.kill("cascade went wrong")

    assert kill_file.exists()
    assert "cascade went wrong" in kill_file.read_text()
    # And the config gate now reads it, whatever the process does next.
    assert backend.settings.kill_switch_present is True


async def test_metrics_carry_the_process_counters(wired) -> None:
    _, client = wired
    metrics = await client.get_metrics()
    assert metrics.counters == {"rows_written": 5}
    assert metrics.feeds == FEEDS


async def test_reload_is_passed_through(wired) -> None:
    _, client = wired
    assert (await client.reload_config()).changed == ["recorder.gap_alert_s"]


# --- latency -------------------------------------------------------------------


def test_percentiles() -> None:
    tracker = LatencyTracker()
    for value in range(1, 101):
        tracker.add_ws_ping(float(value))
    state = tracker.state()
    # Nearest rank on a 0 based index: p50 of 1..100 is the 51st value.
    assert state.ws_ping_p50 == 51.0
    assert state.ws_ping_p95 == 95.0
    assert state.samples == 100


def test_the_window_keeps_only_the_most_recent_samples() -> None:
    tracker = LatencyTracker(window=3)
    for value in (1.0, 2.0, 3.0, 4.0):
        tracker.add_rest(value)
    assert tracker.rest == [2.0, 3.0, 4.0]


def test_no_samples_reads_as_unmeasured_rather_than_zero() -> None:
    state = LatencyTracker().state()
    assert state.ws_ping_p50 is None
    assert state.rest_p95 is None
    assert state.samples == 0
