"""Liquidation intensity: how much forced flow hit a coin in the last w seconds.

Windows are `[t - w, t)`, matching `grid.py`, so a liquidation stamped exactly at t is
first visible at the next step. Everything is summed on the grid and then rolled, so a
year of one coin is two passes and no Python loop.

Venue weights exist because the feeds are not equally complete: Bybit and the Hyperliquid
S3 pull publish every event, while Binance and OKX publish at most one per symbol per
second and so report a floor. See `docs/DATA_SOURCES.md` and the `liq_weight` key on each
venue in `config/default.yaml`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import polars as pl

from liqfade.features.grid import effective_ts

__all__ = ["liq_intensity"]

# Guards the imbalance ratio against 0/0 in a window with no liquidations, which is most
# windows. Small enough against USD notional that it never moves a real ratio.
_EPS = 1e-9


def liq_intensity(
    liquidations: pl.DataFrame,
    grid: pl.DataFrame,
    *,
    windows_s: Sequence[int],
    step_ms: int,
    weights: Mapping[str, float] | None = None,
) -> pl.DataFrame:
    """Per-side liquidation notional and imbalance over each trailing window.

    Args:
        liquidations: canonical `liquidations` rows across any set of venues. Needs
            `venue`, `coin`, `side`, `ts_ms`, `rx_ms`, `notional_usd`.
        grid: the frame from `build_grid`, or any regular `coin`/`ts_ms` grid.
        windows_s: trailing windows in seconds, one set of columns per window.
        step_ms: the grid's step. Each window must be a whole number of steps.
        weights: venue to weight. Missing venues weigh 1.0.

    Returns:
        `coin`, `ts_ms` and, per window w, `liq_long_usd_{w}s`, `liq_short_usd_{w}s` and
        `liq_imbalance_{w}s`. Notionals are USD and never negative; imbalance is
        `(long - short) / (long + short)` in `[-1, 1]` and 0.0 in an empty window. The
        first w seconds of the grid are warm-up and understate, since the window reaches
        back past the start of the data.
    """
    weights = weights or {}
    bucketed = (
        liquidations.with_columns(effective_ts())
        .with_columns(
            ((pl.col("known") // step_ms) * step_ms + step_ms).alias("ts_ms"),
            (
                pl.col("notional_usd")
                * pl.col("venue").replace_strict(weights, default=1.0, return_dtype=pl.Float64)
            ).alias("weighted"),
        )
        .group_by("coin", "ts_ms", "side")
        .agg(pl.col("weighted").sum())
    )
    per_step = grid.select("coin", "ts_ms").sort("coin", "ts_ms")
    for side in ("long", "short"):
        column = bucketed.filter(pl.col("side") == side).select(
            "coin", "ts_ms", pl.col("weighted").alias(side)
        )
        per_step = per_step.join(column, on=["coin", "ts_ms"], how="left").with_columns(
            pl.col(side).fill_null(0.0)
        )

    out = per_step
    for window_s in windows_s:
        steps = window_s * 1000 // step_ms
        rolled = {
            side: pl.col(side).rolling_sum(window_size=steps, min_samples=1).over("coin")
            for side in ("long", "short")
        }
        out = out.with_columns(
            rolled["long"].alias(f"liq_long_usd_{window_s}s"),
            rolled["short"].alias(f"liq_short_usd_{window_s}s"),
        ).with_columns(
            (
                (pl.col(f"liq_long_usd_{window_s}s") - pl.col(f"liq_short_usd_{window_s}s"))
                / (
                    pl.col(f"liq_long_usd_{window_s}s")
                    + pl.col(f"liq_short_usd_{window_s}s")
                    + _EPS
                )
            ).alias(f"liq_imbalance_{window_s}s")
        )
    return out.drop("long", "short")
