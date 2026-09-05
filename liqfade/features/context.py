"""Open interest, mark divergence, funding, premium and session, joined onto the grid.

Everything here comes from `asset_ctx`, which for the historical year is Hyperliquid's own
archive at one sample a minute and for the live bot is `activeAssetCtx` at every block. One
table, one code path, two cadences.

Two rules that are easy to get wrong:

- **A value at grid time t is the last sample strictly before t.** The archive stamps a
  sample on the minute; using it at that same instant would be reading a number a decision
  at t could not have had. One asof join, `t - 1ms`, same as everywhere else.
- **Hyperliquid's `open_interest` counts both sides of the book**, longs plus shorts, so it
  is twice what Binance and Bybit publish. It is halved only for a cross-venue comparison
  and never here: `oi_delta` reads one venue's own series, where the factor cancels in the
  ratio, and halving would leave the raw column wrong for no gain. See
  `docs/DATA_SOURCES.md`, "What the wire actually sends".

A coin with no `asset_ctx` rows gets nulls, not zeros or a forward fill from another coin.
Callers must handle them; `features/proxy.py` treats a null gate as ungated rather than as
passed.
"""

from __future__ import annotations

from collections.abc import Sequence

import polars as pl

from liqfade.features.grid import effective_ts

__all__ = ["session_hour", "with_ctx"]

CTX_COLUMNS = ("mark_px", "oracle_px", "funding", "premium", "open_interest")


def with_ctx(
    grid: pl.DataFrame,
    ctx: pl.DataFrame,
    *,
    windows_s: Sequence[int],
    step_ms: int,
    quantile_lookback_s: int = 86_400,
    quantile: float = 0.95,
) -> pl.DataFrame:
    """Join the asset context onto the grid and derive the proxy's two gates.

    Args:
        grid: `build_grid` output; needs `coin`, `ts_ms`, `mid`.
        ctx: canonical `asset_ctx` rows. May be empty.
        windows_s: windows for `oi_delta_{w}s`, in seconds.
        step_ms: the grid step.
        quantile_lookback_s: trailing seconds behind the mark divergence percentile.
        quantile: the percentile the divergence gate compares against. 0.95 per the spec.

    Returns:
        `grid` plus `mark_px`, `oracle_px`, `funding`, `premium`, `open_interest`,
        `oi_delta_{w}s` (a fraction of the venue's own open interest over the window,
        negative while positions are being closed), `mark_mid_bps` (absolute mark to mid
        divergence in bps) and `mark_mid_over_p95` (bool, null where the trailing
        distribution is not yet full). Every column is null for a coin with no context
        rows and for the span before that coin's first sample.
    """
    out = grid.sort("coin", "ts_ms")
    if ctx.is_empty():
        blanks = [pl.lit(None, dtype=pl.Float64).alias(name) for name in CTX_COLUMNS]
        out = out.with_columns(blanks)
    else:
        samples = (
            ctx.with_columns(effective_ts())
            .select("coin", pl.col("known").alias("_ctx_ts"), *CTX_COLUMNS)
            .sort("_ctx_ts")
        )
        out = (
            out.with_columns((pl.col("ts_ms") - 1).alias("_before"))
            .sort("_before")
            .join_asof(
                samples, left_on="_before", right_on="_ctx_ts", by="coin", strategy="backward"
            )
            .drop("_before", "_ctx_ts")
            .sort("coin", "ts_ms")
        )
    for window_s in windows_s:
        span = window_s * 1000 // step_ms
        past = pl.col("open_interest").shift(span).over("coin")
        out = out.with_columns(
            pl.when(past > 0)
            .then((pl.col("open_interest") - past) / past)
            .otherwise(None)
            .alias(f"oi_delta_{window_s}s")
        )
    return _mark_divergence(out, quantile_lookback_s, quantile, step_ms)


def _mark_divergence(
    frame: pl.DataFrame, lookback_s: int, quantile: float, step_ms: int
) -> pl.DataFrame:
    """abs(mark - mid) / mid in bps, and whether it is over its own trailing percentile.

    The percentile is sampled once a minute for the same reason the z-score is: a trailing
    day at 1s resolution is 86,400 windows per point and does not compute over a year. The
    window is `closed="left"`, so a divergence is never part of the distribution that
    judges it.
    """
    divergence = pl.when((pl.col("mid") > 0) & pl.col("mark_px").is_not_null()).then(
        1e4 * (pl.col("mark_px") - pl.col("mid")).abs() / pl.col("mid")
    )
    out = frame.with_columns(divergence.alias("mark_mid_bps"))
    sample_ms = max(60_000, step_ms)
    thresholds = (
        out.filter(pl.col("ts_ms") % sample_ms == 0)
        .with_columns(pl.col("ts_ms").cast(pl.Datetime("ms")).alias("_when"))
        .sort("_when")
        .with_columns(
            pl.col("mark_mid_bps")
            .rolling_quantile_by(
                "_when", window_size=f"{lookback_s * 1000}ms", quantile=quantile, closed="left"
            )
            .over("coin")
            .alias("mark_mid_threshold")
        )
        .select("coin", pl.col("ts_ms").alias("_th_ts"), "mark_mid_threshold")
        .sort("_th_ts")
    )
    return (
        out.with_columns((pl.col("ts_ms") - 1).alias("_before"))
        .sort("_before")
        .join_asof(thresholds, left_on="_before", right_on="_th_ts", by="coin", strategy="backward")
        .drop("_before", "_th_ts")
        .with_columns(
            pl.when(pl.col("mark_mid_threshold").is_not_null())
            .then(pl.col("mark_mid_bps") > pl.col("mark_mid_threshold"))
            .otherwise(None)
            .alias("mark_mid_over_p95")
        )
        .sort("coin", "ts_ms")
    )


def session_hour(grid: pl.DataFrame) -> pl.DataFrame:
    """Add `session_hour`, the UTC hour 0 to 23. The bucket Q6 splits the sample by."""
    return grid.with_columns(
        pl.col("ts_ms").cast(pl.Datetime("ms")).dt.hour().alias("session_hour")
    )
