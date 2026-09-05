"""The Hyperliquid-native forced flow proxy, with and without its two gates.

The free HL feed does not flag liquidations, so the live bot cannot see what the S3
archive shows after the fact. The proxy is the stand-in, and Q3 decides whether it is good
enough to trigger on. Its value is a multiple of the coin's own trailing median aggressive
volume, so the numbers below are ratios that can be counted.
"""

from __future__ import annotations

import polars as pl
import pytest

from liqfade.features.proxy import hl_forced_proxy

DAY = 1_772_323_200_000
STEP = 1000


def grid(sell: list[float], buy: list[float], **columns: list[object]) -> pl.DataFrame:
    rows = len(sell)
    frame = pl.DataFrame(
        {
            "coin": ["BTC"] * rows,
            "ts_ms": [DAY + i * STEP for i in range(rows)],
            "notional_usd": [s + b for s, b in zip(sell, buy, strict=True)],
            "signed_notional": [b - s for s, b in zip(sell, buy, strict=True)],
        }
    )
    for name, values in columns.items():
        frame = frame.with_columns(pl.Series(name, values))
    return frame


def at(frame: pl.DataFrame, second: int, column: str) -> object:
    return frame.filter(pl.col("ts_ms") == DAY + second * STEP)[column][0]


def test_the_proxy_is_the_window_volume_over_its_own_trailing_median() -> None:
    """A steady 100 a second, then a 15s window of 400: four times the usual rate."""
    quiet, loud = [100.0] * 600, [400.0] * 15
    frame = grid(quiet + loud, [0.0] * 615)
    proxy = hl_forced_proxy(frame, window_s=15, median_lookback_s=300, step_ms=STEP, gated=False)
    assert at(proxy, 599, "hl_forced_proxy_long_15s") == pytest.approx(1.0)
    assert at(proxy, 614, "hl_forced_proxy_long_15s") == pytest.approx(4.0)
    assert proxy["hl_forced_proxy_long_15s"].drop_nulls().min() >= 0.0


def test_the_two_gates_zero_the_proxy_when_either_one_is_shut() -> None:
    """Forced flow closes positions and pushes mark off mid. Volume alone is just volume."""
    quiet, loud = [100.0] * 600, [400.0] * 15
    rows = 615
    frame = grid(
        quiet + loud,
        [0.0] * rows,
        oi_delta_60s=[0.01] * 605 + [-0.05] * 10,
        mark_mid_over_p95=[False] * 610 + [True] * 5,
    )
    proxy = hl_forced_proxy(
        frame,
        window_s=15,
        median_lookback_s=300,
        step_ms=STEP,
        gated=True,
        oi_window_s=60,
    )
    assert at(proxy, 604, "hl_forced_proxy_long_15s") == 0.0  # open interest still rising
    assert at(proxy, 608, "hl_forced_proxy_long_15s") == 0.0  # mark still inside its p95
    assert at(proxy, 614, "hl_forced_proxy_long_15s") == pytest.approx(4.0)


def test_a_missing_gate_column_blocks_rather_than_waves_the_proxy_through() -> None:
    """Null is not the same as passed. A window with no asset_ctx cannot be gated."""
    frame = grid(
        [100.0] * 600 + [400.0] * 15,
        [0.0] * 615,
        oi_delta_60s=[None] * 615,
        mark_mid_over_p95=[None] * 615,
    )
    proxy = hl_forced_proxy(
        frame,
        window_s=15,
        median_lookback_s=300,
        step_ms=STEP,
        gated=True,
        oi_window_s=60,
    )
    assert at(proxy, 614, "hl_forced_proxy_long_15s") == 0.0


def test_the_two_sides_read_the_two_aggressors() -> None:
    """Forced selling is what liquidates longs, so the long proxy reads sell volume."""
    frame = grid([100.0] * 600 + [400.0] * 15, [100.0] * 615)
    proxy = hl_forced_proxy(frame, window_s=15, median_lookback_s=300, step_ms=STEP, gated=False)
    assert at(proxy, 614, "hl_forced_proxy_long_15s") == pytest.approx(4.0)
    assert at(proxy, 614, "hl_forced_proxy_short_15s") == pytest.approx(1.0)


def test_volume_arriving_later_cannot_change_an_earlier_proxy_value() -> None:
    base = grid([100.0] * 600 + [400.0] * 15, [0.0] * 615)
    later = base.with_columns(
        pl.when(pl.col("ts_ms") > DAY + 610 * STEP)
        .then(1e9)
        .otherwise(pl.col("notional_usd"))
        .alias("notional_usd")
    )
    cut = pl.col("ts_ms") <= DAY + 610 * STEP
    kwargs = {"window_s": 15, "median_lookback_s": 300, "step_ms": STEP, "gated": False}
    assert (
        hl_forced_proxy(base, **kwargs).filter(cut).to_dicts()
        == hl_forced_proxy(later, **kwargs).filter(cut).to_dicts()
    )
