"""The Hyperliquid-native forced flow proxy: what the live bot sees instead of liquidations.

The free public Hyperliquid feed does not flag a fill as a liquidation. The S3 archive
does, months later, which is what the whole of Phase 3 measures against, but a live bot has
only the tape. The proxy stands in for it: aggressive volume on the side a cascade would be
hitting, as a multiple of that coin's own trailing median, gated by two things that a
cascade produces and ordinary volume does not.

Three gates, per `docs/STRATEGY_AND_RISK.md` section 2:

1. aggressive volume over the window, over its trailing median. This is the score.
2. `oi_delta < 0`: forced flow closes positions, so open interest falls. Ordinary volume
   opens as many positions as it closes.
3. `abs(mark - mid) / mid` above its trailing 95th percentile: a cascade pushes the traded
   price away from the mark, which is what makes the liquidation cascade rather than clear.

Gates 2 and 3 come from `features/context.py`, which reads one venue's own `asset_ctx`, so
Hyperliquid's both-sides open interest needs no halving here.

**A null gate blocks.** A window with no `asset_ctx` cannot be gated, and treating unknown
as passed would turn every gap in the archive into a burst of proxy triggers. Q3 reports the
ungated variant separately (`gated=False`), and the difference between the two is the gate
lift the preregistration asks for.

The two sides read the two aggressors: forced selling is what liquidates longs, so
`hl_forced_proxy_long` is built from sell-aggressor volume.
"""

from __future__ import annotations

import polars as pl

__all__ = ["hl_forced_proxy"]

# The gate columns, when gating is on. Both are produced by features/context.py.
OI_GATE = "oi_delta_{window_s}s"
MARK_GATE = "mark_mid_over_p95"


def hl_forced_proxy(
    grid: pl.DataFrame,
    *,
    window_s: int,
    median_lookback_s: int,
    step_ms: int,
    gated: bool,
    oi_window_s: int | None = None,
) -> pl.DataFrame:
    """Score each side's aggressive volume against the coin's own trailing median.

    Args:
        grid: `build_grid` output; needs `coin`, `ts_ms`, `notional_usd`,
            `signed_notional`. When `gated` is true it also needs `oi_delta_{w}s` for the
            OI window and `mark_mid_over_p95` from `features/context.py`.
        window_s: the window aggressive volume is summed over.
        median_lookback_s: trailing seconds behind the median the sum is divided by.
        step_ms: the grid step.
        gated: apply gates 2 and 3. False computes the trades-only variant, which is the
            gate-lift measure Q3 reports beside the headline and never decides on.
        oi_window_s: which `oi_delta` column the OI gate reads. Defaults to `window_s`.

    Returns:
        `grid` plus `hl_forced_proxy_long_{w}s` and `hl_forced_proxy_short_{w}s`, both a
        dimensionless multiple of the trailing median and never negative. Null until the
        lookback is full or where the coin has never traded. Zero, not null, where a gate
        blocks: the proxy was computable and says no.
    """
    span = window_s * 1000 // step_ms
    lookback = median_lookback_s * 1000 // step_ms
    if span < 1 or lookback < span:
        raise ValueError("median_lookback_s must be at least window_s, and both above one step")
    buy = ((pl.col("notional_usd") + pl.col("signed_notional")) / 2).alias("_buy")
    sell = ((pl.col("notional_usd") - pl.col("signed_notional")) / 2).alias("_sell")
    out = grid.sort("coin", "ts_ms").with_columns(buy, sell)

    for side, column in (("long", "_sell"), ("short", "_buy")):
        windowed = pl.col(column).rolling_sum(window_size=span, min_samples=span).over("coin")
        out = out.with_columns(windowed.alias(f"_w_{side}"))
        # The median is of the same windowed quantity, so the ratio is like for like, and
        # the window is shifted a full window back so a cascade is never part of the median
        # that judges it.
        typical = (
            pl.col(f"_w_{side}")
            .shift(span)
            .over("coin")
            .rolling_median(window_size=lookback, min_samples=lookback // 2)
            .over("coin")
        )
        out = out.with_columns(typical.alias(f"_m_{side}"))
        out = out.with_columns(
            pl.when(pl.col(f"_m_{side}") > 0)
            .then(pl.col(f"_w_{side}") / pl.col(f"_m_{side}"))
            .otherwise(None)
            .alias(f"hl_forced_proxy_{side}_{window_s}s")
        )

    if gated:
        oi_column = OI_GATE.format(window_s=oi_window_s or window_s)
        missing = [name for name in (oi_column, MARK_GATE) if name not in out.columns]
        if missing:
            raise ValueError(f"gated proxy needs {missing} from features/context.py")
        # A null gate is not a passed gate: unknown blocks.
        open_gates = (pl.col(oi_column) < 0).fill_null(False) & pl.col(MARK_GATE).fill_null(False)
        out = out.with_columns(
            [
                pl.when(open_gates)
                .then(pl.col(f"hl_forced_proxy_{side}_{window_s}s"))
                .otherwise(0.0)
                .alias(f"hl_forced_proxy_{side}_{window_s}s")
                for side in ("long", "short")
            ]
        )
    return out.drop("_buy", "_sell", "_w_long", "_w_short", "_m_long", "_m_short")
