"""Price and flow features, all pure transforms of the grid.

Every function takes the frame `grid.build_grid` produces and returns it with columns
added, so the no-lookahead rule is inherited rather than re-implemented: a grid row at t
already holds only what was knowable before t.

Windows are `[t - w, t)` and are expressed as a whole number of grid steps. Sums over a
partial window at the start of a grid are reported as they stand, because a sum of what
happened is still what happened; statistics that would be biased by a short window
(`realized_vol`) are null until their window is full, because an understated volatility is
a denominator that inflates everything divided by it.
"""

from __future__ import annotations

from collections.abc import Sequence

import polars as pl

__all__ = ["cvd", "displacement", "realized_vol", "returns", "session_vwap"]

DAY_MS = 86_400_000


def _steps(window_s: int, step_ms: int) -> int:
    """Grid rows spanned by a window. Windows must be a whole number of steps."""
    span, remainder = divmod(window_s * 1000, step_ms)
    if remainder or span < 1:
        raise ValueError(f"window {window_s}s is not a whole number of {step_ms}ms steps")
    return span


def returns(grid: pl.DataFrame, *, windows_s: Sequence[int], step_ms: int) -> pl.DataFrame:
    """Log return of mid over each window.

    Args:
        grid: `build_grid` output; needs `coin`, `ts_ms`, `mid`.
        windows_s: window lengths in seconds.
        step_ms: the grid step.

    Returns:
        `grid` plus `ret_{w}s`, dimensionless log returns. Null for the first w of each
        coin and wherever either end of the window has no mid.
    """
    out = grid.sort("coin", "ts_ms")
    for window_s in windows_s:
        span = _steps(window_s, step_ms)
        out = out.with_columns(
            (pl.col("mid") / pl.col("mid").shift(span).over("coin")).log().alias(f"ret_{window_s}s")
        )
    return out


def realized_vol(grid: pl.DataFrame, *, windows_s: Sequence[int], step_ms: int) -> pl.DataFrame:
    """Realized volatility over each window, from step-to-step mid changes.

    Args:
        grid: `build_grid` output; needs `coin`, `ts_ms`, `mid`.
        windows_s: window lengths in seconds.
        step_ms: the grid step.

    Returns:
        `grid` plus `rv_{w}s`, the root of the summed squared step returns. A fraction of
        price over the window, not annualised, so `rv_300s * mid` is a price distance and
        is what `k_stop` is measured in. Never negative. Null until the window is full,
        which is the first w of each coin.
    """
    out = grid.sort("coin", "ts_ms")
    step_return = (pl.col("mid") / pl.col("mid").shift(1).over("coin")).log()
    out = out.with_columns(step_return.pow(2).alias("_r2"))
    for window_s in windows_s:
        span = _steps(window_s, step_ms)
        out = out.with_columns(
            pl.col("_r2")
            .rolling_sum(window_size=span, min_samples=span)
            .over("coin")
            .sqrt()
            .alias(f"rv_{window_s}s")
        )
    return out.drop("_r2")


def cvd(
    grid: pl.DataFrame, *, windows_s: Sequence[int], slope_window_s: int, step_ms: int
) -> pl.DataFrame:
    """Cumulative volume delta over each window, and the slope of the CVD curve.

    Args:
        grid: `build_grid` output; needs `coin`, `ts_ms`, `signed_notional`.
        windows_s: window lengths in seconds.
        slope_window_s: the window the slope is measured over. Must be in `windows_s`.
        step_ms: the grid step.

    Returns:
        `grid` plus `cvd_{w}s` in USD, buy aggressor positive, and `cvd_slope_{w}s` in USD
        per second for the slope window. The slope of a cumulative curve over a window is
        that window's increment divided by its length, so the slope column is exactly
        `cvd_{w}s / w`; it exists as its own column because that is the sign the exhaustion
        rule watches for a flip.
    """
    if slope_window_s not in windows_s:
        raise ValueError(f"slope window {slope_window_s}s must be one of {list(windows_s)}")
    out = grid.sort("coin", "ts_ms")
    for window_s in windows_s:
        span = _steps(window_s, step_ms)
        out = out.with_columns(
            pl.col("signed_notional")
            .rolling_sum(window_size=span, min_samples=1)
            .over("coin")
            .alias(f"cvd_{window_s}s")
        )
    return out.with_columns(
        (pl.col(f"cvd_{slope_window_s}s") / slope_window_s).alias(f"cvd_slope_{slope_window_s}s")
    )


def displacement(
    grid: pl.DataFrame, *, windows_s: Sequence[int], anchor_s: int, step_ms: int
) -> pl.DataFrame:
    """How far mid has been pushed from the VWAP that preceded the window.

    The anchor is deliberately the VWAP of `[t - w - anchor_s, t - w)`, the quiet tape
    before the window rather than the tape inside it, so a cascade cannot drag its own
    reference along with it.

    Args:
        grid: `build_grid` output; needs `coin`, `ts_ms`, `mid`, `notional_usd`, `volume`.
        windows_s: window lengths in seconds.
        anchor_s: length of the VWAP anchor window in seconds.
        step_ms: the grid step.

    Returns:
        `grid` plus `disp_{w}s_bps`, basis points of mid above the anchor VWAP, signed.
        Null when nothing traded in the anchor window, which is a missing reference rather
        than a zero displacement.
    """
    out = grid.sort("coin", "ts_ms")
    anchor_span = _steps(anchor_s, step_ms)
    traded = pl.col("notional_usd").rolling_sum(window_size=anchor_span, min_samples=1).over("coin")
    size = pl.col("volume").rolling_sum(window_size=anchor_span, min_samples=1).over("coin")
    out = out.with_columns(traded.alias("_anchor_notional"), size.alias("_anchor_volume"))
    for window_s in windows_s:
        span = _steps(window_s, step_ms)
        notional = pl.col("_anchor_notional").shift(span).over("coin")
        volume = pl.col("_anchor_volume").shift(span).over("coin")
        vwap = pl.when(volume > 0).then(notional / volume).otherwise(None)
        out = out.with_columns((1e4 * (pl.col("mid") - vwap) / vwap).alias(f"disp_{window_s}s_bps"))
    return out.drop("_anchor_notional", "_anchor_volume")


def session_vwap(grid: pl.DataFrame) -> pl.DataFrame:
    """How far mid sits from the session VWAP, cumulative from 00:00 UTC.

    The other displacement here is measured against a fixed window that ended before the
    cascade; this one is the reference a discretionary trader actually watches, the average
    price everyone who traded today paid, and it resets at midnight rather than rolling.

    The cumulative sums are shifted one grid step, so a row is compared against the VWAP of
    everything that traded strictly before it and never against a reference its own trades
    helped set. That is the same no-lookahead rule the rest of the module inherits from the
    grid, applied to a running total where it would otherwise not hold.

    Args:
        grid: `build_grid` output; needs `coin`, `ts_ms`, `mid`, `notional_usd`, `volume`.

    Returns:
        `grid` plus `disp_day_bps`, basis points of mid above the session VWAP, signed.
        Null until the day's first trade, which is a missing reference and not a zero
        displacement. A grid spanning several days resets at each UTC midnight, so a day
        built with the previous day in front of it is unaffected by the carry.
    """
    out = grid.sort("coin", "ts_ms").with_columns((pl.col("ts_ms") // DAY_MS).alias("_session"))
    traded = pl.col("notional_usd").cum_sum().shift(1).over("coin", "_session")
    size = pl.col("volume").cum_sum().shift(1).over("coin", "_session")
    out = out.with_columns(traded.alias("_day_notional"), size.alias("_day_volume"))
    vwap = pl.when(pl.col("_day_volume") > 0).then(pl.col("_day_notional") / pl.col("_day_volume"))
    return out.with_columns((1e4 * (pl.col("mid") - vwap) / vwap).alias("disp_day_bps")).drop(
        "_session", "_day_notional", "_day_volume"
    )
