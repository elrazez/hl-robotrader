"""Supply and demand zones from 1m candles: fractals and rolling extremes, nothing cleverer.

Q7 asks whether a cascade that ends on a pre-existing level reverts better than one that
ends in open air. Answering it needs levels that a trader could have drawn beforehand, so
the detector is deliberately dumb and transparent: fractal swing points and rolling
extremes. No machine learning, no volume profile. Those are Phase 7 candidates and only if
Q7 says zones matter at all.

**When a level becomes knowable is the whole game.** A fractal at bar i needs N bars on
each side, so it does not exist until bar i+N has closed, which is when bar i+N+1 opens.
Every level carries `known_from` and every query filters on it. Without that the feature is
lookahead of exactly N minutes and any result it produces is an artefact.

Candles come from `candles_1m`, derived from trades by `liqfade derive candles`, because the
Hyperliquid API holds no 1m history. A bar is usable from its close and never mid-bar.
"""

from __future__ import annotations

import polars as pl

__all__ = ["range_levels", "swing_levels", "zone_features"]

MINUTE_MS = 60_000
DAY_MS = 86_400_000
LEVEL_COLUMNS = ("coin", "ts_ms", "px", "kind", "known_from", "source")


def swing_levels(candles: pl.DataFrame, *, fractal_n: int) -> pl.DataFrame:
    """Fractal swing highs and lows: a bar that beats the `fractal_n` bars on each side.

    Args:
        candles: canonical `candles_1m` rows. Needs `coin`, `ts_ms`, `h`, `l`.
        fractal_n: bars required on each side. `features.levels.fractal_n`, default 12.

    Returns:
        `coin`, `ts_ms` (the swing bar's open), `px`, `kind` (`resistance` for a high,
        `support` for a low), `known_from` (the millisecond the level first exists, which is
        `fractal_n` bars after the swing bar closes) and `source` (`swing`). Strictly
        greater on both sides, so a plateau produces no level.
    """
    frame = candles.select("coin", "ts_ms", "h", "l").sort("coin", "ts_ms")
    offsets = [step for step in range(-fractal_n, fractal_n + 1) if step != 0]
    is_high = pl.all_horizontal(
        [pl.col("h") > pl.col("h").shift(-step).over("coin") for step in offsets]
    )
    is_low = pl.all_horizontal(
        [pl.col("l") < pl.col("l").shift(-step).over("coin") for step in offsets]
    )
    # The bar that confirms the fractal is `fractal_n` later; it has closed when the bar
    # after it opens, which is (fractal_n + 1) minutes past the swing bar's own open.
    known = pl.col("ts_ms") + (fractal_n + 1) * MINUTE_MS
    marked = frame.with_columns(is_high.alias("_high"), is_low.alias("_low"))
    parts = [
        marked.filter(pl.col(flag)).select(
            "coin",
            "ts_ms",
            pl.col(column).alias("px"),
            pl.lit(kind).alias("kind"),
            known.alias("known_from"),
            pl.lit("swing").alias("source"),
        )
        for flag, column, kind in (("_high", "h", "resistance"), ("_low", "l", "support"))
    ]
    return pl.concat(parts).sort("coin", "known_from") if parts else _empty_levels()


def range_levels(candles: pl.DataFrame, *, range_d: int) -> pl.DataFrame:
    """Range edges: the rolling extremes and the prior UTC day's and week's high and low.

    Args:
        candles: canonical `candles_1m` rows.
        range_d: days behind the rolling extreme. `features.levels.range_d`, default 5.

    Returns:
        The same six columns `swing_levels` returns, with `source` one of `rolling`,
        `prior_day` or `prior_week`. A prior period's levels are knowable from the moment
        that period ends, which for a UTC day is midnight and for a week is Monday midnight.
    """
    daily = (
        candles.with_columns((pl.col("ts_ms") // DAY_MS * DAY_MS).alias("_day"))
        .group_by("coin", "_day")
        .agg(pl.col("h").max().alias("high"), pl.col("l").min().alias("low"))
        .sort("coin", "_day")
    )
    parts = [
        _period_levels(daily, "prior_day", pl.col("high"), pl.col("low"), shift=1),
        _rolling_levels(daily, range_d),
        _week_levels(candles),
    ]
    kept = [part for part in parts if not part.is_empty()]
    return pl.concat(kept).sort("coin", "known_from") if kept else _empty_levels()


def _empty_levels() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "coin": pl.String,
            "ts_ms": pl.Int64,
            "px": pl.Float64,
            "kind": pl.String,
            "known_from": pl.Int64,
            "source": pl.String,
        }
    )


def _period_levels(
    daily: pl.DataFrame, source: str, high: pl.Expr, low: pl.Expr, *, shift: int
) -> pl.DataFrame:
    """The previous period's high and low, knowable from the moment the period ends."""
    shifted = daily.with_columns(
        high.shift(shift).over("coin").alias("_high"), low.shift(shift).over("coin").alias("_low")
    ).drop_nulls("_high")
    return _stack(shifted, source)


def _rolling_levels(daily: pl.DataFrame, range_d: int) -> pl.DataFrame:
    """Highest high and lowest low over the prior `range_d` complete days."""
    rolled = daily.with_columns(
        pl.col("high")
        .shift(1)
        .over("coin")
        .rolling_max(window_size=range_d, min_samples=1)
        .over("coin")
        .alias("_high"),
        pl.col("low")
        .shift(1)
        .over("coin")
        .rolling_min(window_size=range_d, min_samples=1)
        .over("coin")
        .alias("_low"),
    ).drop_nulls("_high")
    return _stack(rolled, "rolling")


def _week_levels(candles: pl.DataFrame) -> pl.DataFrame:
    """The prior ISO week's high and low, knowable from Monday midnight UTC."""
    weekly = (
        candles.with_columns(
            (pl.col("ts_ms").cast(pl.Datetime("ms")).dt.truncate("1w").dt.timestamp("ms")).alias(
                "_day"
            )
        )
        .group_by("coin", "_day")
        .agg(pl.col("h").max().alias("high"), pl.col("l").min().alias("low"))
        .sort("coin", "_day")
    )
    return _period_levels(weekly, "prior_week", pl.col("high"), pl.col("low"), shift=1)


def _stack(frame: pl.DataFrame, source: str) -> pl.DataFrame:
    """Turn a frame with `_high`/`_low` into two level rows apiece."""
    parts = [
        frame.select(
            "coin",
            pl.col("_day").alias("ts_ms"),
            pl.col(column).alias("px"),
            pl.lit(kind).alias("kind"),
            pl.col("_day").alias("known_from"),
            pl.lit(source).alias("source"),
        )
        for column, kind in (("_high", "resistance"), ("_low", "support"))
    ]
    return pl.concat(parts)


def zone_features(
    levels: pl.DataFrame,
    queries: pl.DataFrame,
    candles: pl.DataFrame,
    *,
    max_dist_bps: float,
    zone_width_bps: float,
) -> pl.DataFrame:
    """Distance to the nearest level each side of a price, and whether it is in a zone.

    Args:
        levels: rows from `swing_levels` and/or `range_levels`.
        queries: `key`, `coin`, `ts_ms`, `px`. One row per question, typically one per
            cascade extreme.
        candles: the bars touches are counted from. Only bars present here are counted, so
            a caller working a month at a time gets a month's touch count and should say so.
        max_dist_bps: a level further than this is treated as absent, not as far away.
            `features.levels.max_dist_bps`, default 200.
        zone_width_bps: how close counts as inside the zone.
            `features.levels.zone_width_bps`, default 15.

    Returns:
        `queries` plus `dist_to_support_bps`, `dist_to_resistance_bps` (null when no level
        of that kind is within the cap), `support_px`, `resistance_px`, `support_touches`,
        `resistance_touches`, `support_fresh`, `resistance_fresh` and `extreme_in_zone`
        (true when the query price is within `zone_width_bps` of either nearest level).
        Only levels with `known_from <= ts_ms` are considered.
    """
    out = queries
    for kind in ("support", "resistance"):
        nearest = _nearest(levels.filter(pl.col("kind") == kind), queries, max_dist_bps, kind)
        out = out.join(nearest, on="key", how="left")
    out = _with_touches(out, candles)
    in_zone = pl.max_horizontal(
        (zone_width_bps >= pl.col("dist_to_support_bps")).fill_null(False),
        (zone_width_bps >= pl.col("dist_to_resistance_bps")).fill_null(False),
    )
    return out.with_columns(in_zone.alias("extreme_in_zone"))


def _nearest(
    levels: pl.DataFrame, queries: pl.DataFrame, max_dist_bps: float, kind: str
) -> pl.DataFrame:
    """The closest level of one kind that already existed, within the distance cap."""
    columns = {
        "key": pl.String,
        f"{kind}_px": pl.Float64,
        f"dist_to_{kind}_bps": pl.Float64,
        f"{kind}_known_from": pl.Int64,
    }
    if levels.is_empty() or queries.is_empty():
        return pl.DataFrame(schema=columns)
    distance = 1e4 * (pl.col("px") - pl.col("px_level")).abs() / pl.col("px")
    return (
        queries.select("key", "coin", "ts_ms", "px")
        .join(
            levels.select("coin", pl.col("px").alias("px_level"), "known_from"),
            on="coin",
        )
        .filter(pl.col("known_from") <= pl.col("ts_ms"))
        .with_columns(distance.alias("_dist"))
        .filter(pl.col("_dist") <= max_dist_bps)
        .sort("key", "_dist")
        .group_by("key")
        .agg(
            pl.col("px_level").first().alias(f"{kind}_px"),
            pl.col("_dist").first().alias(f"dist_to_{kind}_bps"),
            pl.col("known_from").first().alias(f"{kind}_known_from"),
        )
    )


def _with_touches(frame: pl.DataFrame, candles: pl.DataFrame) -> pl.DataFrame:
    """Count the bars between a level's birth and the query whose range contains it.

    Freshness is the property Q7 is actually testing: a level price has already come back
    to several times is a level the market has digested, while an untested one is where
    resting orders still are. A level is fresh when nothing has touched it since it formed.
    """
    for kind in ("support", "resistance"):
        price, born = f"{kind}_px", f"{kind}_known_from"
        if price not in frame.columns:
            continue
        touched = (
            frame.select("key", "coin", "ts_ms", price, born)
            .drop_nulls(price)
            .join(candles.select("coin", pl.col("ts_ms").alias("_bar"), "h", "l"), on="coin")
            .filter(
                (pl.col("_bar") >= pl.col(born))
                & (pl.col("_bar") < pl.col("ts_ms"))
                & (pl.col("l") <= pl.col(price))
                & (pl.col("h") >= pl.col(price))
            )
            .group_by("key")
            .agg(pl.len().alias(f"{kind}_touches"))
        )
        frame = frame.join(touched, on="key", how="left").with_columns(
            pl.when(pl.col(price).is_not_null())
            .then(pl.col(f"{kind}_touches").fill_null(0))
            .otherwise(None)
            .alias(f"{kind}_touches")
        )
        frame = frame.with_columns(
            pl.when(pl.col(price).is_not_null())
            .then(pl.col(f"{kind}_touches") == 0)
            .otherwise(None)
            .alias(f"{kind}_fresh")
        )
    return frame
