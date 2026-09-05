"""Buffer flush triggers: row count, hour roll, and the partition a row lands in."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from liqfade.recorder.buffer import Buffers, partition_of


def ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=UTC).timestamp() * 1000)


NOON = ms("2026-08-30T12:00:30")
LATE = ms("2026-08-30T12:59:59.900")
NEXT = ms("2026-08-30T13:00:00.100")


def _bbo(rx_ms: int) -> dict[str, float | str | int]:
    return {"coin": "BTC", "ts_ms": rx_ms - 5, "rx_ms": rx_ms}


def test_partition_of_is_utc() -> None:
    assert partition_of(NOON) == ("2026-08-30", 12)
    assert partition_of(NEXT) == ("2026-08-30", 13)


def test_nothing_is_ready_while_the_hour_is_still_running() -> None:
    buffers = Buffers(max_rows=1000)
    buffers.add("bbo", "hyperliquid", _bbo(NOON))
    assert buffers.ready(NOON) == []


def test_flush_on_row_count() -> None:
    buffers = Buffers(max_rows=3)
    for _ in range(2):
        buffers.add("bbo", "hyperliquid", _bbo(NOON))
    assert buffers.ready(NOON) == []
    buffers.add("bbo", "hyperliquid", _bbo(NOON))
    assert buffers.ready(NOON) == [("bbo", "hyperliquid", "2026-08-30", 12)]


def test_an_hour_roll_makes_the_previous_hour_ready_and_leaves_the_new_one_alone() -> None:
    buffers = Buffers(max_rows=1000)
    buffers.add("bbo", "hyperliquid", _bbo(LATE))
    buffers.add("bbo", "hyperliquid", _bbo(NEXT))
    ready = buffers.ready(NEXT)
    assert ready == [("bbo", "hyperliquid", "2026-08-30", 12)]
    assert len(buffers.take(ready[0])) == 1
    # The 13:00 row stays buffered until its own hour ends.
    assert buffers.pending_rows() == 1


def test_rows_are_partitioned_by_receive_time_not_exchange_time() -> None:
    """A message about a 12:59 event that arrives at 13:00 belongs to the 13:00 file."""
    buffers = Buffers(max_rows=1000)
    buffers.add("trades", "hyperliquid", {"ts_ms": LATE, "rx_ms": NEXT})
    assert next(iter(buffers.take_all()))[0] == ("trades", "hyperliquid", "2026-08-30", 13)


def test_candles_have_no_receive_time_and_fall_back_to_exchange_time() -> None:
    buffers = Buffers(max_rows=1000)
    buffers.add("candles_1m", "hyperliquid", {"coin": "BTC", "ts_ms": NOON})
    assert next(iter(buffers.take_all()))[0] == ("candles_1m", "hyperliquid", "2026-08-30", 12)


def test_take_all_drains_everything() -> None:
    buffers = Buffers(max_rows=1000)
    buffers.add("bbo", "hyperliquid", _bbo(NOON))
    buffers.add("trades", "binance", {"ts_ms": NOON, "rx_ms": NOON})
    assert len(buffers.take_all()) == 2
    assert buffers.pending_rows() == 0
    assert buffers.take_all() == []


def test_a_row_with_no_usable_timestamp_is_refused_rather_than_filed_under_1970() -> None:
    """`rx_ms or ts_ms or 0` used to bucket a fill whose `time` was missing into 1970."""
    buffers = Buffers(max_rows=1000)
    for row in ({}, {"rx_ms": 0}, {"rx_ms": 0, "ts_ms": None}, {"ts_ms": 0}):
        with pytest.raises(ValueError, match="no usable timestamp"):
            buffers.add("trades", "hyperliquid", dict(row))
    assert buffers.pending_rows() == 0


def test_a_historical_row_still_partitions_on_exchange_time() -> None:
    """`rx_ms = 0` is how every backfilled row is marked; only both being absent is bad."""
    buffers = Buffers(max_rows=1000)
    buffers.add("trades", "hyperliquid", {"ts_ms": NOON, "rx_ms": 0})
    assert next(iter(buffers.take_all()))[0] == ("trades", "hyperliquid", "2026-08-30", 12)
