"""Recorder wiring that is not a stream: flushing, config reload, compaction timing.

Constructing a Recorder opens nothing, so these run offline.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from liqfade.config import load_settings
from liqfade.config.models import Settings
from liqfade.control.protocol import now_ms
from liqfade.recorder.main import Recorder, _seconds_until_utc_hour


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return load_settings("paper", environ={"LIQFADE_PATHS__DATA_DIR": str(tmp_path)})


def test_every_enabled_venue_gets_a_stream_and_shows_up_in_the_feeds(
    settings: Settings,
) -> None:
    recorder = Recorder(settings)
    assert [stream.venue for stream in recorder.streams] == [
        "hyperliquid",
        "binance",
        "bybit",
        "okx",
    ]
    names = [state.name for state in recorder.feed_states(now_ms())]
    assert names == sorted(names)
    assert "hyperliquid.l2" in names
    assert "coinalyze.xchg_ctx" in names
    assert all(state.connected is False for state in recorder.feed_states(now_ms()))


def test_a_disabled_venue_is_not_wired_at_all(settings: Settings) -> None:
    off = settings.model_copy(
        update={
            "venues": settings.venues.model_copy(
                update={"okx": settings.venues.okx.model_copy(update={"enabled": False})}
            )
        }
    )
    assert "okx" not in [stream.venue for stream in Recorder(off).streams]


async def test_the_universe_reaches_every_venue_in_its_own_spelling(
    settings: Settings,
) -> None:
    recorder = Recorder(settings)
    await recorder._apply_universe(["BTC", "kPEPE"])
    symbols = {stream.venue: stream.symbols for stream in recorder.streams}
    assert symbols["hyperliquid"] == ["BTC", "kPEPE"]
    assert symbols["bybit"] == ["1000PEPEUSDT", "BTCUSDT"]
    assert symbols["okx"] == ["1000PEPE-USDT-SWAP", "BTC-USDT-SWAP"]
    assert recorder.poller.coins == ["BTC", "kPEPE"]


async def test_shutdown_flushes_every_buffer_to_parquet(settings: Settings) -> None:
    """Buffered rows are the last five minutes of data. Losing them on stop is not ok."""
    recorder = Recorder(settings)
    at = now_ms()
    recorder.buffers.add(
        "trades",
        "hyperliquid",
        {
            "venue": "hyperliquid",
            "coin": "BTC",
            "ts_ms": at,
            "rx_ms": at,
            "px": 1.0,
            "sz": 2.0,
            "side": "buy",
            "tid": 7,
        },
    )
    await recorder.flush_all()

    written = sorted(recorder.root.glob("trades/date=*/*.parquet"))
    assert len(written) == 1
    assert pq.read_metadata(written[0]).num_rows == 1
    assert recorder.counters == {"rows_written": 1, "files_written": 1, "flushes": 0}
    assert recorder.buffers.pending_rows() == 0


async def test_flush_ready_leaves_the_current_hour_alone(settings: Settings) -> None:
    recorder = Recorder(settings)
    at = now_ms()
    recorder.buffers.add("bbo", "hyperliquid", {"coin": "BTC", "ts_ms": at, "rx_ms": at})
    await recorder.flush_ready()
    assert recorder.buffers.pending_rows() == 1
    assert list(recorder.root.glob("**/*.parquet")) == []


def test_reload_reports_what_changed_and_applies_it(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder(settings)
    assert recorder.reload().changed == []

    monkeypatch.setenv("LIQFADE_RECORDER__RING_BUFFER_ROWS", "1234")
    monkeypatch.setenv("LIQFADE_UNIVERSE__TOP_N_BY_VOLUME", "5")
    result = recorder.reload()
    assert result.error is None
    assert set(result.changed) == {"recorder.ring_buffer_rows", "universe.top_n_by_volume"}
    assert recorder.buffers.max_rows == 1234
    assert recorder.universe.cfg.top_n_by_volume == 5


def test_a_broken_config_is_refused_and_the_running_one_is_kept(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder(settings)
    before = recorder.buffers.max_rows
    monkeypatch.setenv("LIQFADE_RECORDER__RING_BUFFER_ROWS", "-1")
    result = recorder.reload()
    assert result.error is not None
    assert recorder.buffers.max_rows == before


@pytest.mark.parametrize(
    ("now", "hour", "expected_h"),
    [
        ("2026-08-30T01:00:00", 3, 2.0),
        ("2026-08-30T03:00:00", 3, 24.0),  # exactly on the hour means tomorrow
        ("2026-08-30T23:30:00", 3, 3.5),
    ],
)
def test_compaction_is_scheduled_for_the_next_occurrence_of_the_hour(
    now: str, hour: int, expected_h: float
) -> None:
    at = datetime.fromisoformat(now).replace(tzinfo=UTC)
    assert _seconds_until_utc_hour(hour, at) == pytest.approx(expected_h * 3600)
