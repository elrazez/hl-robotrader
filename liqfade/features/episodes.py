"""Liquidation cascade episodes: the research ground truth.

An episode is a run of liquidations on one coin and one side with no gap longer than
`join_gap_s` between consecutive events. Grouping is the whole of `group_episodes`;
whether an episode is large enough to study is `qualify_episodes`, kept separate so the
unfiltered count is always available beside the filtered one.

Built from exact Hyperliquid liquidations (the S3 `fills_full` pull, `side` naming the
side that was liquidated, so `long` is forced selling and price falls into it). Vectorized
in polars: a year of one coin is a group_by, not a loop.

This detector is deliberately allowed to see a whole episode, because it is measuring one
after the fact rather than trading it. What it may not do is let data from after an
episode's own window change that episode's numbers; `tests/test_features_episodes.py`
pins that.
"""

from __future__ import annotations

import polars as pl

__all__ = ["group_episodes", "qualify_episodes"]


def group_episodes(
    liquidations: pl.DataFrame,
    bars: pl.DataFrame,
    *,
    join_gap_s: float,
    peak_window_s: float,
    participation: float,
    participation_window_s: float,
) -> pl.DataFrame:
    """Join liquidation events into episodes, one row per episode.

    Args:
        liquidations: canonical `liquidations` rows, one venue. Needs `coin`, `side`,
            `ts_ms`, `px`, `notional_usd`. Order does not matter; it is sorted here.
        bars: the 1s grid, columns `coin`, `ts_ms`, `mid`, `notional_usd`. `mid` is the
            last trade price in that second for the historical year, where no bbo exists.
        join_gap_s: seconds between consecutive events that ends an episode.
        peak_window_s: seconds of the rolling window behind `peak_liq_15s`.
        participation: fraction of post-extreme traded notional assumed takeable.
        participation_window_s: seconds after the extreme that capacity is measured over.

    Returns:
        One row per episode sorted by `coin`, `side`, `start_ts`, with columns `coin`,
        `side`, `start_ts`, `end_ts`, `n_events`, `total_notional`. Empty input gives an
        empty frame with the same columns.
    """
    gap_ms = int(join_gap_s * 1000)
    events = liquidations.select("coin", "side", "ts_ms", "px", "notional_usd").sort(
        "coin", "side", "ts_ms"
    )
    starts = (pl.col("ts_ms").diff().over("coin", "side").fill_null(gap_ms + 1) > gap_ms).cum_sum()
    # (t - peak_window, t] summed at every event, so the peak is the busiest burst inside
    # the episode rather than its average rate. Grouped by episode as well as coin and
    # side: a window can never reach the previous episode, which ended over join_gap ago,
    # but saying so costs nothing and survives a smaller join_gap.
    peak = pl.col("notional_usd").rolling_sum_by(
        "when", window_size=f"{int(peak_window_s * 1000)}ms", closed="right"
    )
    episodes = (
        events.with_columns(
            starts.alias("episode"), pl.col("ts_ms").cast(pl.Datetime("ms")).alias("when")
        )
        .with_columns(peak.over("coin", "side", "episode").alias("liq_rolling"))
        .group_by("coin", "side", "episode")
        .agg(
            pl.col("ts_ms").first().alias("start_ts"),
            pl.col("ts_ms").last().alias("end_ts"),
            pl.len().alias("n_events"),
            pl.col("notional_usd").sum().alias("total_notional"),
            pl.col("liq_rolling").max().alias(f"peak_liq_{int(peak_window_s)}s"),
        )
        .sort("coin", "side", "start_ts")
    )
    grid = bars.select("coin", "ts_ms", "mid").sort("ts_ms")
    episodes = _with_anchor(episodes, grid)
    episodes = _with_extreme(episodes, grid, gap_ms)
    episodes = _with_capacity(episodes, bars, participation, int(participation_window_s * 1000))
    return episodes.drop("episode").sort("coin", "side", "start_ts")


def qualify_episodes(
    episodes: pl.DataFrame,
    *,
    min_notional_usd: float,
    min_move_bps: float,
    p95_lookback_d: int,
    liq_z_min: float,
) -> pl.DataFrame:
    """Mark which episodes are large enough to study. Adds columns, never drops rows.

    Nothing is filtered here so that the unfiltered count is always available beside the
    filtered one, which is what stops a report quoting an episode count that was never
    the population.

    Args:
        episodes: `group_episodes` output. An optional `max_liq_z` column carries the
            highest `liq_z_60s` reached during the episode; where it is absent or null the
            z rule cannot fire and qualification rests on the p95 rule alone.
        min_notional_usd: absolute floor on episode notional. Not tunable by Phase 3.
        min_move_bps: absolute floor on abs(anchor - extreme) / anchor. Not tunable.
        p95_lookback_d: days of the coin's own prior episodes behind the p95 threshold.
        liq_z_min: the z-score that qualifies an episode regardless of the p95 rule.

    Returns:
        The frame plus `move_bps`, `p95_notional`, `passes_floors`, `passes_p95`,
        `passes_z` and `qualified`. `qualified` is `passes_floors AND (passes_p95 OR
        passes_z)`.
    """
    frame = episodes
    if "max_liq_z" not in frame.columns:
        frame = frame.with_columns(pl.lit(None, dtype=pl.Float64).alias("max_liq_z"))
    move_bps = 1e4 * (pl.col("anchor_px") - pl.col("extreme_px")).abs() / pl.col("anchor_px")
    # closed="left" is the no-lookahead rule in one argument: the threshold an episode is
    # judged against is built from strictly earlier episodes of the same coin, never from
    # itself or from the future. The first episodes of a coin have no prior distribution,
    # so p95 is null and only the z rule can qualify them.
    p95 = pl.col("total_notional").rolling_quantile_by(
        "started", window_size=f"{p95_lookback_d}d", quantile=0.95, closed="left"
    )
    return (
        frame.with_columns(
            move_bps.alias("move_bps"),
            pl.col("start_ts").cast(pl.Datetime("ms")).alias("started"),
        )
        .sort("started")
        .with_columns(p95.over("coin").alias("p95_notional"))
        .with_columns(
            (
                (pl.col("total_notional") >= min_notional_usd)
                & (pl.col("move_bps") >= min_move_bps)
            ).alias("passes_floors"),
            (pl.col("total_notional") >= pl.col("p95_notional"))
            .fill_null(False)
            .alias("passes_p95"),
            (pl.col("max_liq_z") >= liq_z_min).fill_null(False).alias("passes_z"),
        )
        .with_columns(
            (pl.col("passes_floors") & (pl.col("passes_p95") | pl.col("passes_z"))).alias(
                "qualified"
            )
        )
        .drop("started")
        .sort("coin", "side", "start_ts")
    )


def _with_anchor(episodes: pl.DataFrame, grid: pl.DataFrame) -> pl.DataFrame:
    """anchor_px is the last mid at or before the episode's first liquidation."""
    return episodes.sort("start_ts").join_asof(
        grid.rename({"mid": "anchor_px"}),
        left_on="start_ts",
        right_on="ts_ms",
        by="coin",
        strategy="backward",
    )


def _with_extreme(episodes: pl.DataFrame, grid: pl.DataFrame, gap_ms: int) -> pl.DataFrame:
    """The furthest mid in the liquidated direction over [start_ts, end_ts + join_gap].

    The window runs `join_gap` past the last event because that silence is what ended the
    episode: the price impact of the last liquidation lands in the seconds after it, and a
    single-event episode would otherwise record anchor == extreme and a zero move. Episode
    windows on one coin and side cannot overlap, since the next episode starts more than
    `join_gap` after this one's last event, so each bar belongs to at most one of them.
    """
    windows = episodes.select("coin", "side", "episode", "start_ts", "end_ts").with_columns(
        (pl.col("end_ts") + gap_ms).alias("window_end")
    )
    in_window = (
        grid.join(windows.select("coin", "side").unique(), on="coin")
        .sort("ts_ms")
        .join_asof(
            windows.sort("start_ts"),
            left_on="ts_ms",
            right_on="start_ts",
            by=["coin", "side"],
            strategy="backward",
        )
        .filter(pl.col("episode").is_not_null(), pl.col("ts_ms") <= pl.col("window_end"))
    )
    # arg_min/arg_max take the first occurrence, so a repeated extreme reports the
    # earliest time it was reached, which is when a trade could first have been made.
    low = pl.col("mid").arg_min()
    high = pl.col("mid").arg_max()
    extremes = in_window.group_by("coin", "side", "episode").agg(
        pl.when(pl.col("side").first() == "long")
        .then(pl.col("mid").min())
        .otherwise(pl.col("mid").max())
        .alias("extreme_px"),
        pl.when(pl.col("side").first() == "long")
        .then(pl.col("ts_ms").get(low))
        .otherwise(pl.col("ts_ms").get(high))
        .alias("extreme_ts"),
    )
    return episodes.join(extremes, on=["coin", "side", "episode"], how="left")


def _with_capacity(
    episodes: pl.DataFrame, bars: pl.DataFrame, participation: float, window_ms: int
) -> pl.DataFrame:
    """A share of the notional traded in (extreme_ts, extreme_ts + window].

    What the strategy could have taken without being most of the tape, which is the number
    that turns an edge in bps into a business or does not. Computed from a running total
    and two asof lookups rather than one window per episode, so a year is two sorts.
    """
    running = (
        bars.select("coin", "ts_ms", "notional_usd")
        .sort("ts_ms")
        .with_columns(pl.col("notional_usd").cum_sum().over("coin").alias("traded"))
    )

    def traded_at(frame: pl.DataFrame, column: str, alias: str) -> pl.DataFrame:
        # The lookup key is renamed per call: two asof joins against the same right frame
        # would otherwise both try to bring back a column called ts_ms.
        key = f"{alias}_ts"
        return frame.sort(column).join_asof(
            running.select("coin", pl.col("ts_ms").alias(key), pl.col("traded").alias(alias)),
            left_on=column,
            right_on=key,
            by="coin",
            strategy="backward",
        )

    with_ends = episodes.with_columns((pl.col("extreme_ts") + window_ms).alias("capacity_end"))
    with_ends = traded_at(with_ends, "extreme_ts", "traded_at_extreme")
    with_ends = traded_at(with_ends, "capacity_end", "traded_at_end")
    name = f"notional_at_{round(participation * 100)}pct_participation"
    return with_ends.with_columns(
        (
            participation
            * (pl.col("traded_at_end").fill_null(0.0) - pl.col("traded_at_extreme").fill_null(0.0))
        ).alias(name)
    ).drop(
        "capacity_end",
        "traded_at_extreme",
        "traded_at_extreme_ts",
        "traded_at_end",
        "traded_at_end_ts",
    )
