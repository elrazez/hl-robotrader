"""Spread, depth and book imbalance from L2 snapshots.

**These columns are null for almost the whole research window, and that is not a bug.** The
historical year has no `bbo` at all, and `l2` covers four coins only, so for every other
coin the honest answer to "what was the spread" is that nobody recorded it. The
cost model handles that by subtracting an estimated effective spread built from Hyperliquid's
own impact prices in `asset_ctx` (see `scripts/research/_lib.py`), which is why the research
can proceed without this module.

Null means absent, never zero. A caller that reads a null depth as no liquidity blocks a
trade for a reason that does not exist; a caller that reads it as infinite liquidity sizes
into a book it cannot see. Both are worse than refusing to answer, so every function here
returns nulls and every caller has to decide what to do about them.

Once the recorder has run for a few weeks its live `bbo` and `l2` fill these in for the
recorded window, and the same code path serves both.
"""

from __future__ import annotations

import polars as pl

from liqfade.features.grid import effective_ts

__all__ = ["book_features"]

BOOK_COLUMNS = ("spread_bps", "depth_bid", "depth_ask", "book_imbalance")


def book_features(grid: pl.DataFrame, l2: pl.DataFrame, *, depth_bps: float) -> pl.DataFrame:
    """Join the touch and the depth within `depth_bps` onto the grid.

    Args:
        grid: `build_grid` output; needs `coin`, `ts_ms`, `mid`.
        l2: canonical `l2` rows, twenty levels per snapshot, or an empty frame. Empty is
            the normal case for a historical window.
        depth_bps: how far from the snapshot's own mid a level must be to count.
            `features.depth_bps`, default 10.

    Returns:
        `grid` plus `spread_bps`, `depth_bid` and `depth_ask` (USD notional resting within
        the band on each side) and `book_imbalance` (`(bid - ask) / (bid + ask)`, in
        `[-1, 1]`). All four are null wherever no snapshot precedes the row, including
        every row when `l2` is empty.
    """
    out = grid.sort("coin", "ts_ms")
    if l2.is_empty():
        return out.with_columns(
            [pl.lit(None, dtype=pl.Float64).alias(name) for name in BOOK_COLUMNS]
        )
    touch = (
        pl.col("bid_px").max().over("coin", "known"),
        pl.col("ask_px").min().over("coin", "known"),
    )
    levels = l2.with_columns(effective_ts()).with_columns(
        touch[0].alias("_best_bid"), touch[1].alias("_best_ask")
    )
    book_mid = (pl.col("_best_bid") + pl.col("_best_ask")) / 2
    band = depth_bps / 1e4
    inside_bid = pl.col("bid_px") >= book_mid * (1 - band)
    inside_ask = pl.col("ask_px") <= book_mid * (1 + band)
    per_snapshot = levels.group_by("coin", "known").agg(
        (
            1e4
            * (pl.col("_best_ask").first() - pl.col("_best_bid").first())
            / ((pl.col("_best_ask").first() + pl.col("_best_bid").first()) / 2)
        ).alias("spread_bps"),
        (pl.col("bid_px") * pl.col("bid_sz")).filter(inside_bid).sum().alias("depth_bid"),
        (pl.col("ask_px") * pl.col("ask_sz")).filter(inside_ask).sum().alias("depth_ask"),
    )
    per_snapshot = per_snapshot.with_columns(
        (
            (pl.col("depth_bid") - pl.col("depth_ask"))
            / (pl.col("depth_bid") + pl.col("depth_ask"))
        ).alias("book_imbalance")
    ).sort("known")
    return (
        out.with_columns((pl.col("ts_ms") - 1).alias("_before"))
        .sort("_before")
        .join_asof(
            per_snapshot, left_on="_before", right_on="known", by="coin", strategy="backward"
        )
        .drop("_before", "known")
        .sort("coin", "ts_ms")
    )
