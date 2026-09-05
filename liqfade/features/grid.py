"""The regular time grid every other feature is computed on.

One row per coin per step. A row stamped `ts_ms = t` summarises the rows whose
`effective_ts` falls in `[t - step, t)`, strictly before t. That half-open convention is
the whole no-lookahead rule: a value read at t was knowable at t, so no downstream feature
has to re-check it, and a trade landing exactly on the second belongs to the next bar.

`effective_ts` is `rx_ms` when the row has one and `ts_ms` otherwise. Historical pulls
write `rx_ms = 0` because they have no local receive time, so for the backfilled year the
grid is on exchange time; for recorded data it is on the time the data reached us, which
is the only time a live decision could have used.

`mid` prefers the bbo midpoint and falls back to the last trade price. **The historical
year has no bbo at all**, so for every backfilled window `mid` is the last trade price and
carries no spread. Nothing may treat it as a tradeable price: the cost model subtracts an
estimated effective spread per side precisely because of this.
"""

from __future__ import annotations

import polars as pl

__all__ = ["build_grid", "effective_ts"]


def effective_ts() -> pl.Expr:
    """The timestamp a row became knowable: `rx_ms` when recorded, `ts_ms` when backfilled."""
    return (
        pl.when(pl.col("rx_ms") > 0).then(pl.col("rx_ms")).otherwise(pl.col("ts_ms")).alias("known")
    )


def build_grid(
    trades: pl.DataFrame,
    bbo: pl.DataFrame | None,
    *,
    start_ms: int,
    end_ms: int,
    step_ms: int,
) -> pl.DataFrame:
    """Build the price and volume grid over `[start_ms, end_ms)`.

    Args:
        trades: canonical `trades` rows. Needs `coin`, `ts_ms`, `rx_ms`, `px`, `sz`.
        bbo: canonical `bbo` rows, or None. Absent for the whole historical year.
        start_ms: first grid timestamp, inclusive.
        end_ms: end of the grid, exclusive.
        step_ms: milliseconds between rows. 1000 for the research grid, 250 for decisions.

    Returns:
        `coin`, `ts_ms`, `mid`, `notional_usd`, `volume`, `signed_notional`, `n_trades`,
        one row per coin per step, sorted by coin then time. `mid` is null until the coin's
        first print and is forward filled through steps with no trades. The four
        aggregates are 0 in a step with no trades, never null: no trades is a real
        measurement of zero. `volume` is base units and `signed_notional` is USD with the
        buy aggressor positive, which is what the CVD and VWAP features are built from,
        summed here so every downstream feature reads the grid rather than the tape.
    """
    coins = trades.select("coin").unique().sort("coin")
    times = pl.DataFrame({"ts_ms": range(start_ms, end_ms, step_ms)}, schema={"ts_ms": pl.Int64})
    grid = coins.join(times, how="cross").sort("ts_ms")

    marked = trades.with_columns(effective_ts()).sort("known")
    # asof joins include an exactly equal key, and the grid needs strictly before t. One
    # millisecond off the lookup key is exact here because every timestamp in the lake is
    # an integer millisecond.
    grid = grid.with_columns((pl.col("ts_ms") - 1).alias("before"))
    grid = _with_mid(grid, marked, bbo)
    # `known` rides in on the asof join's right key and is not part of the contract.
    return (
        _with_volume(grid.drop("before"), marked, step_ms)
        .drop("known", strict=False)
        .sort("coin", "ts_ms")
    )


def _with_mid(grid: pl.DataFrame, trades: pl.DataFrame, bbo: pl.DataFrame | None) -> pl.DataFrame:
    """Last bbo midpoint before t, falling back to the last trade price before t."""
    last_trade = trades.select("coin", "known", pl.col("px").alias("trade_mid"))
    grid = grid.join_asof(
        last_trade, left_on="before", right_on="known", by="coin", strategy="backward"
    )
    if bbo is not None and bbo.height:
        quotes = (
            bbo.with_columns(effective_ts())
            .sort("known")
            .select(
                "coin",
                "known",
                ((pl.col("bid_px") + pl.col("ask_px")) / 2).alias("book_mid"),
            )
        )
        grid = grid.join_asof(
            quotes, left_on="before", right_on="known", by="coin", strategy="backward"
        )
    else:
        grid = grid.with_columns(pl.lit(None, dtype=pl.Float64).alias("book_mid"))
    return grid.with_columns(pl.coalesce("book_mid", "trade_mid").alias("mid")).drop(
        "book_mid", "trade_mid"
    )


def _with_volume(grid: pl.DataFrame, trades: pl.DataFrame, step_ms: int) -> pl.DataFrame:
    """Notional and print count in `[t - step, t)`, zero where nothing traded."""
    bucket = ((pl.col("known") // step_ms) * step_ms + step_ms).alias("ts_ms")
    per_step = (
        trades.with_columns(bucket)
        .group_by("coin", "ts_ms")
        .agg(
            (pl.col("px") * pl.col("sz")).sum().alias("notional_usd"),
            pl.col("sz").sum().alias("volume"),
            (
                pl.when(pl.col("side") == "buy")
                .then(pl.col("px") * pl.col("sz"))
                .otherwise(-pl.col("px") * pl.col("sz"))
            )
            .sum()
            .alias("signed_notional"),
            pl.len().alias("n_trades"),
        )
    )
    return grid.join(per_step, on=["coin", "ts_ms"], how="left").with_columns(
        pl.col("notional_usd").fill_null(0.0),
        pl.col("volume").fill_null(0.0),
        pl.col("signed_notional").fill_null(0.0),
        pl.col("n_trades").fill_null(0),
    )
