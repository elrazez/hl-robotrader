"""Liquidation intensity on the grid, and the robust z-score built from it.

Windows are `[t - w, t)`, the same half-open convention the grid uses, so a liquidation
stamped exactly at t counts from the next step onward and never from the step that a
decision at t would read.
"""

from __future__ import annotations

import polars as pl
import pytest

from liqfade.data.schemas import SOURCE_HL_S3, rows_to_table
from liqfade.features.intensity import liq_intensity

DAY = 1_772_323_200_000
STEP = 1000


def liqs(*rows: tuple[int, str, float, str]) -> pl.DataFrame:
    """(offset_ms, side, notional_usd, venue) tuples as canonical liquidation rows."""
    return pl.from_arrow(
        rows_to_table(
            "liquidations",
            [
                {
                    "venue": venue,
                    "coin": "BTC",
                    "venue_symbol": "BTC",
                    "ts_ms": DAY + offset,
                    "rx_ms": 0,
                    "side": side,
                    "px": 100.0,
                    "sz": notional / 100.0,
                    "notional_usd": notional,
                    "method": "market",
                    "complete": True,
                }
                for offset, side, notional, venue in rows
            ],
            SOURCE_HL_S3,
        )
    )


def grid(seconds: int) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "coin": ["BTC"] * seconds,
            "ts_ms": [DAY + i * STEP for i in range(seconds)],
        }
    )


def at(frame: pl.DataFrame, second: int, column: str) -> float:
    return frame.filter(pl.col("ts_ms") == DAY + second * STEP)[column][0]


def test_a_liquidation_enters_the_window_on_the_next_step_and_leaves_it_after_w() -> None:
    """The boundary that decides whether a cascade is visible one tick early or one late."""
    intensity = liq_intensity(
        liqs((0, "long", 1e6, "hyperliquid")), grid(30), windows_s=[15], step_ms=STEP
    )
    assert at(intensity, 0, "liq_long_usd_15s") == 0.0
    assert at(intensity, 1, "liq_long_usd_15s") == 1e6
    assert at(intensity, 15, "liq_long_usd_15s") == 1e6
    assert at(intensity, 16, "liq_long_usd_15s") == 0.0


def test_imbalance_is_signed_towards_the_liquidated_side_and_stays_inside_one() -> None:
    intensity = liq_intensity(
        liqs((0, "long", 3e6, "hyperliquid"), (0, "short", 1e6, "hyperliquid")),
        grid(30),
        windows_s=[15],
        step_ms=STEP,
    )
    assert at(intensity, 1, "liq_imbalance_15s") == pytest.approx(0.5)
    assert at(intensity, 20, "liq_imbalance_15s") == 0.0  # empty window, not a division by zero
    assert intensity["liq_imbalance_15s"].abs().max() <= 1.0


def test_venue_weights_scale_each_venues_contribution() -> None:
    """Binance and OKX publish at most one event a second, so their notional is a floor."""
    intensity = liq_intensity(
        liqs((0, "long", 1e6, "bybit"), (0, "long", 1e6, "binance")),
        grid(30),
        windows_s=[15],
        step_ms=STEP,
        weights={"bybit": 1.0, "binance": 0.5},
    )
    assert at(intensity, 1, "liq_long_usd_15s") == 1.5e6


def test_intensity_is_never_negative_and_later_events_cannot_change_earlier_steps() -> None:
    early = liqs((0, "long", 1e6, "hyperliquid"))
    late = pl.concat([early, liqs((20_000, "long", 5e8, "hyperliquid"))])
    kwargs = {"windows_s": [15, 60], "step_ms": STEP}
    before = liq_intensity(early, grid(30), **kwargs).filter(pl.col("ts_ms") <= DAY + 20 * STEP)
    after = liq_intensity(late, grid(30), **kwargs).filter(pl.col("ts_ms") <= DAY + 20 * STEP)
    assert before.to_dicts() == after.to_dicts()
    assert before["liq_long_usd_60s"].min() >= 0.0
