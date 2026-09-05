"""The 1s grid: the single place where "what was the price at time t" is decided.

Every feature and every research script reads this frame, so the no-lookahead rule is
enforced here once rather than in nine modules. A row at `ts_ms = t` summarises rows with
`effective_ts` in `[t - step, t)`, strictly before t, which is what makes a value read at
t safe to act on at t.
"""

from __future__ import annotations

import polars as pl

from liqfade.data.schemas import SOURCE_HL_S3, SOURCE_RECORDER, rows_to_table
from liqfade.features.grid import build_grid

DAY = 1_772_323_200_000


def trades(*rows: tuple[int, float, float, str], rx_offset_ms: int = 0) -> pl.DataFrame:
    """(offset_ms, px, sz, side) tuples as canonical trade rows."""
    return pl.from_arrow(
        rows_to_table(
            "trades",
            [
                {
                    "venue": "hyperliquid",
                    "coin": "BTC",
                    "ts_ms": DAY + offset,
                    "rx_ms": DAY + offset + rx_offset_ms if rx_offset_ms else 0,
                    "px": px,
                    "sz": sz,
                    "side": side,
                    "tid": index,
                }
                for index, (offset, px, sz, side) in enumerate(rows)
            ],
            SOURCE_HL_S3 if not rx_offset_ms else SOURCE_RECORDER,
        )
    )


def bbo(*rows: tuple[int, float, float]) -> pl.DataFrame:
    """(offset_ms, bid_px, ask_px) tuples as canonical bbo rows."""
    return pl.from_arrow(
        rows_to_table(
            "bbo",
            [
                {
                    "coin": "BTC",
                    "ts_ms": DAY + offset,
                    "rx_ms": DAY + offset,
                    "bid_px": bid,
                    "bid_sz": 1.0,
                    "ask_px": ask,
                    "ask_sz": 1.0,
                }
                for offset, bid, ask in rows
            ],
            SOURCE_RECORDER,
        )
    )


def test_the_mid_at_a_grid_time_is_the_last_trade_strictly_before_it() -> None:
    """A trade landing exactly on the second belongs to the next bar, not this one."""
    grid = build_grid(
        trades((500, 100.0, 1.0, "buy"), (1000, 101.0, 1.0, "buy"), (1500, 102.0, 1.0, "sell")),
        None,
        start_ms=DAY,
        end_ms=DAY + 4000,
        step_ms=1000,
    )
    assert grid["ts_ms"].to_list() == [DAY, DAY + 1000, DAY + 2000, DAY + 3000]
    assert grid["mid"].to_list() == [None, 100.0, 102.0, 102.0]


def test_each_step_carries_the_notional_and_print_count_of_the_step_before_it() -> None:
    """A step with no trades is zero, not null: no trades is a measurement, not a gap."""
    grid = build_grid(
        trades((500, 100.0, 1.0, "buy"), (1000, 101.0, 2.0, "buy"), (1500, 102.0, 3.0, "sell")),
        None,
        start_ms=DAY,
        end_ms=DAY + 4000,
        step_ms=1000,
    )
    assert grid["notional_usd"].to_list() == [0.0, 100.0, 202.0 + 306.0, 0.0]
    assert grid["volume"].to_list() == [0.0, 1.0, 5.0, 0.0]
    assert grid["n_trades"].to_list() == [0, 1, 2, 0]
    # The sell of 3 at 102 nets against the buy of 2 at 101 in the same step.
    assert grid["signed_notional"].to_list() == [0.0, 100.0, 202.0 - 306.0, 0.0]


def test_the_book_midpoint_wins_over_the_last_trade_when_a_quote_exists() -> None:
    grid = build_grid(
        trades((500, 105.0, 1.0, "buy")),
        bbo((400, 99.0, 101.0)),
        start_ms=DAY,
        end_ms=DAY + 2000,
        step_ms=1000,
    )
    assert grid["mid"].to_list() == [None, 100.0]


def test_a_recorded_row_is_knowable_at_its_receive_time_not_its_exchange_time() -> None:
    """600ms of transit means a trade stamped at 500ms is not in the bar closing at 1000."""
    grid = build_grid(
        trades((500, 100.0, 1.0, "buy"), rx_offset_ms=600),
        None,
        start_ms=DAY,
        end_ms=DAY + 3000,
        step_ms=1000,
    )
    assert grid["mid"].to_list() == [None, None, 100.0]
    assert grid["notional_usd"].to_list() == [0.0, 0.0, 100.0]


def test_rows_arriving_after_t_cannot_change_the_grid_at_t() -> None:
    """The no-lookahead property, on the module that decides it for everything downstream."""
    early = trades((500, 100.0, 1.0, "buy"), (1500, 101.0, 1.0, "buy"))
    late = pl.concat([early, trades((2500, 900.0, 50.0, "sell"), (3500, 5.0, 99.0, "sell"))])
    kwargs = {"start_ms": DAY, "end_ms": DAY + 5000, "step_ms": 1000}
    before = build_grid(early, None, **kwargs).filter(pl.col("ts_ms") <= DAY + 2000)
    after = build_grid(late, None, **kwargs).filter(pl.col("ts_ms") <= DAY + 2000)
    assert before.to_dicts() == after.to_dicts()
