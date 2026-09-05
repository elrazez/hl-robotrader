"""Recorder wiring that is not a stream: flushing, config reload, compaction timing.

Constructing a Recorder opens nothing, so these run offline.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import orjson
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
    assert await recorder._apply_universe(["BTC", "kPEPE"]) is True
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
    assert recorder.counters == {
        "rows_written": 1,
        "files_written": 1,
        "flushes": 0,
        "write_failures": 0,
        "quarantined_rows": 0,
    }
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


def _trade(at: int, tid: int) -> dict[str, object]:
    return {
        "venue": "hyperliquid",
        "coin": "BTC",
        "ts_ms": at,
        "rx_ms": at,
        "px": 1.0,
        "sz": 2.0,
        "side": "buy",
        "tid": tid,
    }


async def test_a_failed_write_keeps_the_rows_and_alerts(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed write must not be the thing that destroys the rows it was writing.

    They used to be taken out of the buffer before write_rows was called, so a full disk
    or one row failing schema validation lost the partition silently.
    """
    recorder = Recorder(settings)
    at = now_ms()
    recorder.buffers.add("trades", "hyperliquid", _trade(at, 7))

    def explode(*_args: object, **_kwargs: object) -> None:
        raise OSError("no space left on device")

    alerts: list[str] = []

    async def capture(text: str) -> bool:
        alerts.append(text)
        return True

    monkeypatch.setattr("liqfade.recorder.main.write_rows", explode)
    monkeypatch.setattr(recorder.health, "alert", capture)

    await recorder.flush_all()

    assert recorder.buffers.pending_rows() == 1  # still there for the next tick
    assert recorder.counters["write_failures"] == 1
    assert recorder.counters["rows_written"] == 0
    assert len(alerts) == 1
    assert "still buffered" in alerts[0]

    # And the retry succeeds once the failure clears.
    monkeypatch.undo()
    await recorder.flush_all()
    assert recorder.buffers.pending_rows() == 0
    assert recorder.counters["rows_written"] == 1


async def test_rows_arriving_during_a_write_are_not_dropped(settings: Settings) -> None:
    """The receive loop keeps appending while the write runs in a thread.

    Only the rows actually handed to write_rows may be forgotten, so the drop is from the
    front of the buffer rather than a wholesale clear.
    """
    recorder = Recorder(settings)
    at = now_ms()
    recorder.buffers.add("trades", "hyperliquid", _trade(at, 1))

    real_write = recorder.buffers.drop_written
    late = _trade(at, 2)

    def drop_after_a_late_arrival(key: tuple[str, str, str, int], count: int) -> None:
        # Stands in for a row appended while write_rows was in its thread.
        recorder.buffers.add("trades", "hyperliquid", late)
        real_write(key, count)

    recorder.buffers.drop_written = drop_after_a_late_arrival  # type: ignore[method-assign]
    await recorder.flush_all()

    assert recorder.counters["rows_written"] == 1
    assert recorder.buffers.pending_rows() == 1
    remaining = recorder.buffers.peek(("trades", "hyperliquid", *_partition(at)))
    assert remaining == [late]


def _partition(ms: int) -> tuple[str, int]:
    from liqfade.recorder.buffer import partition_of

    return partition_of(ms)


async def test_a_row_with_no_timestamp_is_quarantined_and_never_reaches_1970(
    settings: Settings,
) -> None:
    """The recorder cannot raise into the receive loop, so the row goes to a file."""
    recorder = Recorder(settings)
    alerts: list[str] = []

    async def capture(text: str) -> bool:
        alerts.append(text)
        return True

    recorder.health.alert = capture  # type: ignore[method-assign]
    recorder.sink("trades", "hyperliquid", {"coin": "BTC", "px": 1.0, "ts_ms": None})
    await recorder.flush_all()

    assert recorder.buffers.pending_rows() == 0
    assert list(recorder.root.glob("**/date=1970-01-01")) == []
    assert recorder.counters["quarantined_rows"] == 1
    (quarantined,) = sorted((settings.paths.data_dir / "quarantine").rglob("*.jsonl"))
    assert orjson.loads(quarantined.read_bytes().splitlines()[0])["coin"] == "BTC"
    assert alerts and "quarantine" in alerts[0]


async def test_a_stream_that_raises_does_not_stop_the_others_or_mark_the_refresh_done(
    settings: Settings,
) -> None:
    """Streams were resubscribed serially, so a ConnectionClosed on stream 2 left 3 and 4
    on the old universe, and the next refresh saw no change and never retried them."""
    recorder = Recorder(settings)
    binance_stream = next(s for s in recorder.streams if s.venue == "binance")

    async def closed(_symbols: object) -> None:
        raise ConnectionError("connection closed mid-send")

    binance_stream.set_symbols = closed  # type: ignore[method-assign]
    assert await recorder._apply_universe(["BTC", "kPEPE"]) is False

    took = {s.venue: s.symbols for s in recorder.streams if s.venue != "binance"}
    assert took["hyperliquid"] == ["BTC", "kPEPE"]
    assert took["bybit"] == ["1000PEPEUSDT", "BTCUSDT"]
    assert took["okx"] == ["1000PEPE-USDT-SWAP", "BTC-USDT-SWAP"]
    assert recorder.poller.coins == ["BTC", "kPEPE"]


async def test_a_cancelled_shutdown_still_finishes_the_flush(settings: Settings) -> None:
    """Shutdown runs inside runtime.run_main's grace window, which cancels the main
    coroutine when it expires. A slow drain used to lose every partition it had not
    reached yet, and there is no next flush tick at shutdown."""
    recorder = Recorder(settings)
    at = now_ms()
    recorder.buffers.add("trades", "hyperliquid", _trade(at, 1))

    started, release = asyncio.Event(), asyncio.Event()
    real_write = recorder._write

    async def slow(key: tuple[str, str, str, int]) -> None:
        started.set()
        await release.wait()
        await real_write(key)

    recorder._write = slow  # type: ignore[method-assign]
    task = asyncio.create_task(recorder._flush_shielded())
    await started.wait()
    task.cancel()  # the grace window expiring
    release.set()
    await task

    assert recorder.buffers.pending_rows() == 0
    assert len(sorted(recorder.root.glob("trades/date=*/*.parquet"))) == 1
