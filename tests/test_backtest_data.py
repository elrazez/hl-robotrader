"""The event iterator and the clock: ordering, absence, and the no-lookahead tie rule."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import polars as pl
import pytest

from liqfade.backtest.clock import Tick, merge, tick_times
from liqfade.backtest.data import (
    L2,
    AssetCtx,
    Bbo,
    Trade,
    iter_events,
    load_window,
    utc_days,
)

FIXTURE_LAKE = Path(__file__).parent / "fixtures"
FIXTURE_DAY = "2026-08-30"  # the day tests/fixtures/parquet was captured on
DAY_MS = 86_400_000
T0 = 1_772_323_200_000  # 2026-03-01T00:00:00Z


def trades(*rows: tuple[int, int, str, float]) -> pl.DataFrame:
    """(ts_ms, rx_ms, coin, px) rows as a canonical trades frame."""
    return pl.DataFrame(
        [
            {
                "coin": coin,
                "ts_ms": ts,
                "rx_ms": rx,
                "px": px,
                "sz": 1.0,
                "side": "buy",
            }
            for ts, rx, coin, px in rows
        ]
    )


class TestUtcDays:
    def test_a_window_inside_one_day_is_one_partition(self) -> None:
        assert utc_days(T0, T0 + 1000) == ["2026-03-01"]

    def test_a_window_spanning_midnight_is_two(self) -> None:
        assert utc_days(T0, T0 + DAY_MS + 1) == ["2026-03-01", "2026-03-02"]

    def test_the_exclusive_end_does_not_pull_an_extra_day(self) -> None:
        """A window ending exactly at midnight does not touch the next day's partition."""
        assert utc_days(T0, T0 + DAY_MS) == ["2026-03-01"]


class TestOrdering:
    def test_events_come_out_in_effective_time_order(self) -> None:
        frame = trades((T0 + 500, 0, "BTC", 3.0), (T0, 0, "BTC", 1.0), (T0 + 100, 0, "BTC", 2.0))
        assert [event.px for event in iter_events(trades=frame)] == [1.0, 2.0, 3.0]  # type: ignore[union-attr]

    def test_recorded_rows_order_on_receive_time_not_exchange_time(self) -> None:
        """rx_ms is the only time a live decision could have used the row.

        A row whose exchange timestamp is older but which reached us later has to be
        replayed later, or the backtest sees information before it existed locally.
        """
        frame = trades((T0 + 900, T0 + 100, "BTC", 1.0), (T0 + 100, T0 + 900, "BTC", 2.0))
        assert [event.px for event in iter_events(trades=frame)] == [1.0, 2.0]  # type: ignore[union-attr]

    def test_backfilled_and_recorded_rows_interleave_on_their_own_clocks(self) -> None:
        frame = trades((T0 + 500, 0, "BTC", 1.0), (T0 + 900, T0 + 200, "BTC", 2.0))
        assert [event.px for event in iter_events(trades=frame)] == [2.0, 1.0]  # type: ignore[union-attr]

    def test_book_state_precedes_the_prints_that_fill_against_it(self) -> None:
        """Within one timestamp: book, quote, print, context, liquidation.

        A resting order has to see the book that preceded the print it might fill against,
        so the order within a tie is fixed rather than left to whichever frame was built
        first.
        """
        stamp = T0
        book = pl.DataFrame(
            [
                {
                    "coin": "BTC",
                    "ts_ms": stamp,
                    "rx_ms": 0,
                    "level": 0,
                    "bid_px": 99.0,
                    "bid_sz": 1.0,
                    "ask_px": 101.0,
                    "ask_sz": 1.0,
                }
            ]
        )
        quote = pl.DataFrame(
            [
                {
                    "coin": "BTC",
                    "ts_ms": stamp,
                    "rx_ms": 0,
                    "bid_px": 99.0,
                    "bid_sz": 1.0,
                    "ask_px": 101.0,
                    "ask_sz": 1.0,
                }
            ]
        )
        liqs = pl.DataFrame(
            [
                {
                    "venue": "hyperliquid",
                    "coin": "BTC",
                    "ts_ms": stamp,
                    "rx_ms": 0,
                    "side": "long",
                    "px": 100.0,
                    "sz": 1.0,
                    "notional_usd": 100.0,
                }
            ]
        )
        ctx = pl.DataFrame(
            [
                {
                    "coin": "BTC",
                    "ts_ms": stamp,
                    "rx_ms": 0,
                    "mark_px": 100.0,
                    "oracle_px": 100.0,
                    "mid_px": 100.0,
                    "funding": 1e-5,
                }
            ]
        )
        kinds = [
            type(event).__name__
            for event in iter_events(
                trades=trades((stamp, 0, "BTC", 100.0)),
                liquidations=liqs,
                bbo=quote,
                l2=book,
                asset_ctx=ctx,
            )
        ]
        assert kinds == ["L2", "Bbo", "Trade", "AssetCtx", "Liq"]

    def test_the_same_input_produces_the_same_order_twice(self) -> None:
        """Ties break on a fixed key, which is what makes byte-identical fills possible."""
        frame = trades(*[(T0, 0, "BTC", float(n)) for n in range(20)])
        first = [event.px for event in iter_events(trades=frame)]  # type: ignore[union-attr]
        second = [event.px for event in iter_events(trades=frame)]  # type: ignore[union-attr]
        assert first == second


class TestAbsence:
    def test_missing_tables_are_not_an_error(self) -> None:
        """The historical year has no bbo at all. That degrades the fill model, not the run."""
        events = list(iter_events(trades=trades((T0, 0, "BTC", 1.0))))
        assert [type(event) for event in events] == [Trade]

    def test_an_empty_trades_frame_yields_nothing(self) -> None:
        assert list(iter_events(trades=pl.DataFrame())) == []

    def test_load_window_on_an_absent_table_returns_an_empty_frame(self, tmp_path: Path) -> None:
        assert load_window(tmp_path, "bbo", start_ms=T0, end_ms=T0 + 1000).is_empty()


class TestL2Assembly:
    def test_levels_are_ordered_outwards_from_the_touch(self) -> None:
        """A taker walk consumes them in this order, so the file's order is not trusted.

        Reversed here on purpose: a walk that took the far side of the book first would
        fill at a much worse price and report the difference as slippage.
        """
        rows = [
            {
                "coin": "BTC",
                "ts_ms": T0,
                "rx_ms": 0,
                "level": level,
                "bid_px": 99.0 - level,
                "bid_sz": 1.0,
                "ask_px": 101.0 + level,
                "ask_sz": 1.0,
            }
            for level in reversed(range(3))
        ]
        (event,) = list(iter_events(trades=pl.DataFrame(), l2=pl.DataFrame(rows)))
        assert isinstance(event, L2)
        assert [px for px, _ in event.bids] == [99.0, 98.0, 97.0]
        assert [px for px, _ in event.asks] == [101.0, 102.0, 103.0]


class TestRealFixtureLake:
    """Reads the committed five minutes of real recorder output, not a mock."""

    def test_it_reads_trades_bbo_l2_and_asset_ctx_off_the_lake(self) -> None:
        # Derived from the partition name rather than written as an epoch, so a
        # regenerated fixture on a different date does not need this number edited.
        day_start = int(datetime.fromisoformat(f"{FIXTURE_DAY}T00:00:00+00:00").timestamp() * 1000)
        window = {"start_ms": day_start, "end_ms": day_start + DAY_MS}
        loaded = {
            table: load_window(FIXTURE_LAKE, table, **window)
            for table in ("trades", "bbo", "l2", "asset_ctx")
        }
        for table, frame in loaded.items():
            assert not frame.is_empty(), f"{table} is empty in the fixture lake"

        events = list(
            iter_events(
                trades=loaded["trades"],
                bbo=loaded["bbo"],
                l2=loaded["l2"],
                asset_ctx=loaded["asset_ctx"],
            )
        )
        assert events, "the fixture lake produced no events"
        stamps = [event.ts_ms for event in events]
        assert stamps == sorted(stamps), "events must come out in time order"
        kinds = {type(event) for event in events}
        assert {Trade, Bbo, L2, AssetCtx} <= kinds


class TestClock:
    def test_ticks_are_aligned_to_an_absolute_multiple_of_the_cadence(self) -> None:
        """So two runs over overlapping windows put their ticks in the same places."""
        assert list(tick_times(1000, 2000, 250)) == [1000, 1250, 1500, 1750]
        assert list(tick_times(1001, 2000, 250)) == [1250, 1500, 1750]

    def test_a_zero_cadence_raises(self) -> None:
        with pytest.raises(ValueError, match="cadence"):
            list(tick_times(0, 1000, 0))

    def test_an_event_on_a_tick_is_delivered_before_it(self) -> None:
        """The no-lookahead rule at the loop level.

        A decision at T must not see a print stamped T. `features/grid.py` uses the same
        half-open convention, where a row stamped T summarises the interval that ended at T.
        """
        events = [Trade(ts_ms=1000, coin="BTC", px=1.0, sz=1.0, side="buy")]
        merged = list(merge(events, [1000]))
        assert [type(item).__name__ for item in merged] == ["Trade", "Tick"]

    def test_events_and_ticks_interleave_in_time_order(self) -> None:
        events = [
            Trade(ts_ms=900, coin="BTC", px=1.0, sz=1.0, side="buy"),
            Trade(ts_ms=1600, coin="BTC", px=2.0, sz=1.0, side="buy"),
        ]
        merged = list(merge(events, [1000, 1500, 2000]))
        assert [item.ts_ms for item in merged] == [
            900,
            1000,
            1500,
            1600,
            2000,
        ]
        assert [isinstance(item, Tick) for item in merged] == [
            False,
            True,
            True,
            False,
            True,
        ]

    def test_leftover_events_and_leftover_ticks_both_drain(self) -> None:
        events = [Trade(ts_ms=5000, coin="BTC", px=1.0, sz=1.0, side="buy")]
        assert len(list(merge(events, [1000]))) == 2
        assert len(list(merge([], [1000, 2000]))) == 2
        assert len(list(merge(events, []))) == 1
