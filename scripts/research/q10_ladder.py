"""Q10: does a ladder of projected liquidation prices mark anything real?

Every untouched swing extreme projects a ladder of prices where the traders who entered
there are force-closed: at leverage `L` and maintenance margin `m`, `1/L - m` away. Where
ladders from several different anchors land on the same price, that price is where forced
flow concentrates. Four questions, preregistered in
`reports/research/PREREGISTRATION_Q10.md` before this script read anything:

- **Q10a** reaction: does price react at a projected band more than at a shifted control
  band the same distance scale away?
- **Q10b** confluence: Q7's question with projected bands in place of raw levels, and
  broken out by how many distinct anchors stack on the same price.
- **Q10c** fuel: do episodes with little forced flow ahead of them revert better?
- **Q10d** reality (descriptive): do the projected bands line up with the liquidation
  prices `hl_positions` actually records?

**Q7 already failed the neighbouring claim** and this one inherits its burden of proof:
cascades ending within 15 bps of a raw swing level reverted +36.1 bps net-stress against
+146.3 for cascades ending nowhere near one. Q7's row is printed beside every row of
Q10b's table so the two are always read together.

    uv run python scripts/research/q10_ladder.py --start 2025-09-01 --end 2026-08-31 \
        --data-dir /Volumes/<drive>/liqfade
"""

from __future__ import annotations

import sys
from itertools import pairwise
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).parent))

import _lib as lib

from liqfade.config.loader import load_settings
from liqfade.features.levels import range_levels, swing_levels
from liqfade.features.liqmap import band_weights, ladder_bands, map_features

BAND_WIDTHS_BPS = (10.0, 15.0, 25.0)
HORIZONS_MIN = (5, 15, 30)
# Under 40 in a cell is `inconclusive`, never a result. The preregistration's floor.
MIN_CELL = 40
MINUTE_MS = 60_000
DAY_MS = 86_400_000
# Q8's winning cell, from reports/research/q8_path.md. Q10c reads its stop-out rate.
K_STOP, K_TARGET = 1.0, 0.8

# Q7's own numbers, transcribed from reports/research/q7_zones.md so every Q10b row can be
# read against the result it has to beat. Width -> (in zone, out of zone), net-stress.
Q7_ROWS = {
    10.0: ("+34.5 [+17.3, +55.9] (n=1431)", "+140.3 [+22.2, +337.6] (n=1289)"),
    15.0: ("+36.1 [+19.1, +57.7] (n=1523)", "+146.3 [+22.6, +367.1] (n=1197)"),
    25.0: ("+37.9 [+20.0, +61.6] (n=1635)", "+155.1 [+21.0, +388.9] (n=1085)"),
}


# --- anchors and bands ---------------------------------------------------------------


def anchors(candles: pl.DataFrame, cfg: object) -> pl.DataFrame:
    """Every level Q7 uses, plus the moment price first traded back through it.

    `touched_from` is an upper bound on an anchor's validity, computed once per anchor and
    compared per query by `map_features`. A touch that happens later can never change what
    an earlier query saw, so this is not lookahead; it is the "untouched extremes only"
    rule expressed as a window rather than as a filter over the whole chunk.
    """
    levels = pl.concat(
        [
            swing_levels(candles, fractal_n=cfg.fractal_n),  # type: ignore[attr-defined]
            range_levels(candles, range_d=cfg.range_d),  # type: ignore[attr-defined]
        ]
    )
    if levels.is_empty():
        return levels
    keyed = levels.with_columns(
        pl.concat_str(
            ["coin", "kind", pl.col("ts_ms").cast(pl.String), pl.col("px").cast(pl.String)],
            separator="/",
        ).alias("key")
    )
    touched = first_touch(keyed, candles).rename({"touch_ts": "touched_from"})
    return keyed.join(touched, on="key", how="left").drop("key")


def first_touch(targets: pl.DataFrame, candles: pl.DataFrame) -> pl.DataFrame:
    """The first bar at or after `known_from` whose range contains each target's `px`.

    Args:
        targets: `key`, `coin`, `px`, `known_from`. A level, or a projected band.
        candles: 1m bars over the same window.

    Returns:
        `key` and `touch_ts`, for the targets price actually reached. A target never
        reached is absent, which a left join turns into the null its callers want.

    Walks days rather than joining every target to every bar. A month holds roughly 13,000
    bands and 43,000 bars per coin, so the direct join is billions of rows per coin and the
    research machine has 8 GB. Each day is prefiltered by that day's own high and low per
    coin, so only the targets price came near are ever joined to bars, and a target leaves
    the pool the moment it is touched.
    """
    empty = targets.head(0).select("key", pl.lit(None, dtype=pl.Int64).alias("touch_ts"))
    if targets.is_empty() or candles.is_empty():
        return empty
    bars = candles.select("coin", "ts_ms", "h", "l").with_columns(
        (pl.col("ts_ms") // DAY_MS).alias("_day")
    )
    found = []
    pool = targets.select("key", "coin", "px", "known_from")
    for day in sorted(bars["_day"].unique().to_list()):
        if pool.is_empty():
            break
        day_bars = bars.filter(pl.col("_day") == day).drop("_day")
        span = day_bars.group_by("coin").agg(
            pl.col("h").max().alias("_hi"), pl.col("l").min().alias("_lo")
        )
        candidates = (
            pool.filter(pl.col("known_from") < (day + 1) * DAY_MS)
            .join(span, on="coin")
            .filter((pl.col("px") >= pl.col("_lo")) & (pl.col("px") <= pl.col("_hi")))
            .select("key", "coin", "px", "known_from")
        )
        if candidates.is_empty():
            continue
        hit = (
            candidates.join(day_bars, on="coin")
            .filter(
                (pl.col("ts_ms") >= pl.col("known_from"))
                & (pl.col("l") <= pl.col("px"))
                & (pl.col("h") >= pl.col("px"))
            )
            .group_by("key")
            .agg(pl.col("ts_ms").min().alias("touch_ts"))
        )
        if hit.is_empty():
            continue
        found.append(hit)
        pool = pool.join(hit.select("key"), on="key", how="anti")
    return pl.concat(found) if found else empty


def shifted_ladder(ladder: list[float], mm_frac: float) -> list[float]:
    """Q10a's control: every rung moved by half the gap to the next rung.

    Expressed back as a leverage, so `ladder_bands` builds the control with the same code
    and the only thing that varies between arms is whether the distance is a real leverage
    level. A distance `d` is the leverage `1 / (d + mm_frac)`.
    """
    distances = sorted(1.0 / rung - mm_frac for rung in ladder if 1.0 / rung > mm_frac)
    midpoints = [(low + high) / 2.0 for low, high in pairwise(distances)]
    return [1.0 / (distance + mm_frac) for distance in midpoints]


def bands_of(
    levels: pl.DataFrame,
    oi: pl.DataFrame,
    cfg: object,
    ladder: list[float],
    *,
    measured: bool = False,
) -> pl.DataFrame:
    """One weighted band per anchor per rung, carrying the anchor's freshness window.

    `measured=False` is the **primary** scheme the preregistration fixes: every band
    carries the same weight, so fuel is a count of bands and no weighting choice can be
    tuned until a table passes. `measured=True` is the single preregistered secondary: the
    open interest that opened at the anchor, split by the leverage histogram measured off
    `hl_positions`. Both decay with age.
    """
    if levels.is_empty():
        return pl.DataFrame()
    bands = ladder_bands(
        levels,
        ladder=ladder,
        mm_frac=cfg.mm_frac,  # type: ignore[attr-defined]
        max_dist_bps=cfg.max_dist_bps,  # type: ignore[attr-defined]
    )
    if bands.is_empty():
        return bands
    weighted = (
        band_weights(
            bands,
            oi,
            histogram=cfg.histogram,  # type: ignore[attr-defined]
            half_life_min=cfg.half_life_min,  # type: ignore[attr-defined]
        )
        if measured
        else bands.with_columns(
            pl.lit(1.0).alias("weight_usd"),
            pl.lit(cfg.half_life_min).alias("half_life_min"),  # type: ignore[attr-defined]
        )
    )
    touched = levels.select(
        "coin", "ts_ms", pl.col("px").alias("anchor_px"), "touched_from"
    ).unique(subset=["coin", "ts_ms", "anchor_px"])
    return weighted.join(touched, on=["coin", "ts_ms", "anchor_px"], how="left")


# --- Q10a, reaction at a band --------------------------------------------------------


def with_archive_spread(touched: pl.DataFrame, ctx: pl.DataFrame) -> pl.DataFrame:
    """One effective spread per touch, from Hyperliquid's own impact prices.

    The archive estimator alone, not the wider of the two `_lib.effective_spread_bps`
    takes: the trade-based half of that pair is measured on a cascade minute and there is
    no cascade here, only a price arriving somewhere. The sample precedes the entry, so
    there is no lookahead. A touch with no context sample carries a null spread and drops
    out of the priced table rather than being priced at zero cost.
    """
    if touched.is_empty() or ctx.is_empty():
        return touched.with_columns(pl.lit(None, dtype=pl.Float64).alias("spread_bps"))
    spread = 1e4 * (pl.col("impact_ask_px") - pl.col("impact_bid_px")) / pl.col("mid_px")
    samples = (
        ctx.select("coin", "ts_ms", "impact_bid_px", "impact_ask_px", "mid_px")
        .with_columns(spread.alias("_spread"))
        .select("coin", pl.col("ts_ms").alias("_ctx_ts"), "_spread")
        .sort("_ctx_ts")
    )
    return (
        touched.sort("touch_ts")
        .join_asof(samples, left_on="touch_ts", right_on="_ctx_ts", by="coin", strategy="backward")
        .rename({"_spread": "spread_bps"})
        .drop("_ctx_ts", strict=False)
    )


def touches(bands: pl.DataFrame, candles: pl.DataFrame, ctx: pl.DataFrame) -> pl.DataFrame:
    """The first bar to reach each band, and the move over each horizon after it.

    On 1m candles rather than the 1s grid, and the entry is the touching bar's close: see
    the preregistration. That is up to 60s later than the preregistered entry, so on a
    reverting move it captures less and the substitution biases against Q10a itself.
    """
    if bands.is_empty() or candles.is_empty():
        return pl.DataFrame()
    bars = candles.select("coin", pl.col("ts_ms").alias("bar_ts"), "h", "l", "c", "o")
    keyed = bands.with_columns(
        pl.concat_str(
            [
                "coin",
                "side",
                pl.col("ts_ms").cast(pl.String),
                pl.col("leverage").cast(pl.String),
                pl.col("px").cast(pl.String),
            ],
            separator="/",
        ).alias("_band")
    )
    hit = first_touch(keyed.rename({"_band": "key"}), candles).rename({"key": "_band"})
    reached = (
        keyed.join(hit, on="_band")
        # The anchor's own ladder goes stale the moment price trades back through it, so a
        # band reached after that is not the trap this question is about.
        .filter(pl.col("touched_from").is_null() | (pl.col("touch_ts") < pl.col("touched_from")))
        .drop("_band")
    )
    if reached.is_empty():
        return pl.DataFrame()
    entry = reached.join(
        bars.select("coin", "bar_ts", pl.col("c").alias("entry_px"), pl.col("o").alias("open_px")),
        left_on=["coin", "touch_ts"],
        right_on=["coin", "bar_ts"],
        how="left",
    )
    # A `long_liq` band is where trapped longs are force-sold, so fading it is buying, which
    # `with_cost_columns` calls side `long`. The mirror for `short_liq`.
    entry = entry.with_columns(
        pl.when(pl.col("side") == "long_liq")
        .then(pl.lit("long"))
        .otherwise(pl.lit("short"))
        .alias("side"),
        pl.concat_str(
            ["coin", pl.col("touch_ts").cast(pl.String), pl.col("px").cast(pl.String)],
            separator="/",
        ).alias("key"),
        pl.col("touch_ts").alias("start_ts"),
    )
    for minutes in HORIZONS_MIN:
        entry = entry.with_columns((pl.col("touch_ts") + minutes * MINUTE_MS).alias("_at"))
        entry = (
            entry.sort("_at")
            .join_asof(
                bars.select("coin", "bar_ts", pl.col("c").alias(f"_c{minutes}")).sort("bar_ts"),
                left_on="_at",
                right_on="bar_ts",
                by="coin",
                strategy="forward",
            )
            .rename({f"_c{minutes}": f"px_plus_{minutes}m"})
            # The asof join carries the right key across; left behind it would collide with
            # itself on the next horizon.
            .drop("_at", "bar_ts", strict=False)
        )
    return with_archive_spread(lib.with_clusters(entry, window_s=300), ctx)


def reaction_table(
    ladder_touches: pl.DataFrame, control_touches: pl.DataFrame
) -> tuple[str, dict[int, tuple[lib.Estimate, lib.Estimate]]]:
    """The ladder against the shifted control, at each horizon, net-stress."""
    rows = []
    results: dict[int, tuple[lib.Estimate, lib.Estimate]] = {}
    for minutes in HORIZONS_MIN:
        estimates = []
        for part in (ladder_touches, control_touches):
            priced = _priced(part, minutes)
            estimates.append(
                lib.cluster_bootstrap(
                    priced["net_stress_bps"].to_numpy(), priced["cluster"].to_numpy()
                )
                if not priced.is_empty()
                else lib.Estimate(float("nan"), float("nan"), float("nan"), 0, 0)
            )
        results[minutes] = (estimates[0], estimates[1])
        rows.append([f"+{minutes} min", estimates[0].render(), estimates[1].render()])
    header = ["horizon", "ladder, net-stress", "shifted control, net-stress"]
    return lib.table(header, rows), results


def _priced(touched: pl.DataFrame, minutes: int) -> pl.DataFrame:
    """One horizon's outcomes, with the three cost columns attached."""
    if touched.is_empty() or f"px_plus_{minutes}m" not in touched.columns:
        return pl.DataFrame()
    frame = touched.with_columns(
        pl.col(f"px_plus_{minutes}m").alias("exit_px"),
        pl.col("entry_px").alias("entry_px_delayed"),
    ).drop_nulls(["exit_px", "entry_px"])
    if frame.is_empty():
        return frame
    return lib.with_cost_columns(frame).drop_nulls("net_stress_bps")


# --- Q10b, confluence ----------------------------------------------------------------


def in_band(width_bps: float) -> pl.Expr:
    nearest = pl.min_horizontal("dist_to_band_below_bps", "dist_to_band_above_bps")
    return (nearest <= width_bps).fill_null(False)


def confluence_table(
    episodes: pl.DataFrame,
) -> tuple[str, dict[float, tuple[lib.Estimate, lib.Estimate]]]:
    """In band against out of band at each width, with Q7's own row printed beside it."""
    rows = []
    results: dict[float, tuple[lib.Estimate, lib.Estimate]] = {}
    for width in BAND_WIDTHS_BPS:
        inside = episodes.filter(in_band(width)).drop_nulls("net_stress_bps")
        outside = episodes.filter(~in_band(width)).drop_nulls("net_stress_bps")
        estimates = tuple(
            lib.cluster_bootstrap(part["net_stress_bps"].to_numpy(), part["cluster"].to_numpy())
            for part in (inside, outside)
        )
        results[width] = estimates  # type: ignore[assignment]
        q7_in, q7_out = Q7_ROWS[width]
        rows.append([f"{width:g} bps", estimates[0].render(), estimates[1].render(), q7_in, q7_out])
    header = [
        "band width",
        "in band, net-stress",
        "out of band, net-stress",
        "Q7 in zone",
        "Q7 out of zone",
    ]
    return lib.table(header, rows), results


def stacked_table(episodes: pl.DataFrame, width: float) -> str:
    """Confluence is the actual claim, so it gets its own table: one anchor, two, three+."""
    rows = []
    inside = episodes.filter(in_band(width)).drop_nulls("net_stress_bps")
    for label, low, high in (("1 anchor", 1, 2), ("2 anchors", 2, 3), ("3 or more", 3, 10_000)):
        part = inside.filter((pl.col("bands_stacked") >= low) & (pl.col("bands_stacked") < high))
        if part.is_empty():
            rows.append([label, "0", "_no episodes_"])
            continue
        estimate = lib.cluster_bootstrap(
            part["net_stress_bps"].to_numpy(), part["cluster"].to_numpy()
        )
        rows.append([label, str(part.height), estimate.render()])
    return lib.table(["anchors stacked", "episodes", "net-stress"], rows)


# --- Q10c, fuel ----------------------------------------------------------------------


def fuel_table(
    episodes: pl.DataFrame, column: str = "fuel_ahead_usd"
) -> tuple[str, dict[str, lib.Estimate]]:
    """Terciles of the forced flow the cascade would have to run through, at each horizon."""
    frame = episodes.drop_nulls(column)
    results: dict[str, lib.Estimate] = {}
    if frame.is_empty():
        return "_No episode carried a fuel measurement._", results
    cuts = [frame[column].quantile(third) for third in (1 / 3, 2 / 3)]
    rows = []
    for label, low, high in (
        ("low fuel", float("-inf"), cuts[0]),
        ("middle", cuts[0], cuts[1]),
        ("high fuel", cuts[1], float("inf")),
    ):
        # Ties sit at the bottom: a band frame where nothing opened is all low fuel, not a
        # high-fuel bucket holding every episode there is.
        part = frame.filter((pl.col(column) > low) & (pl.col(column) <= high))
        cells = []
        for minutes in HORIZONS_MIN:
            priced = part.with_columns(pl.col(f"mid_plus_{minutes}m").alias("exit_px"))
            priced = lib.with_cost_columns(priced.drop("net_stress_bps")).drop_nulls(
                "net_stress_bps"
            )
            estimate = (
                lib.cluster_bootstrap(
                    priced["net_stress_bps"].to_numpy(), priced["cluster"].to_numpy()
                )
                if not priced.is_empty()
                else lib.Estimate(float("nan"), float("nan"), float("nan"), 0, 0)
            )
            if minutes == 15:
                results[label] = estimate
            cells.append(estimate.render())
        rows.append([label, str(part.height), *cells])
    header = ["fuel ahead", "episodes", *(f"+{minutes} min" for minutes in HORIZONS_MIN)]
    return lib.table(header, rows), results


# --- Q10d, does the projection match reality -----------------------------------------


def recorded_positions(args: object) -> pl.DataFrame:
    """The latest snapshot per address and coin, one day at a time.

    Not through `lib.load_table`, which refuses this table and is right to: a poll writes
    about a thousand rows and runs every minute, so a day is over a million rows and a
    month does not fit. Each day is reduced to its last row per address and coin inside the
    scan, which is the only shape Q10d needs.
    """
    parts = []
    for day in lib.dates(args.start, args.end):  # type: ignore[attr-defined]
        scan = lib.scan_day(
            args.data_dir,  # type: ignore[attr-defined]
            "hl_positions",
            day,
            columns=[
                "coin",
                "rx_ms",
                "address",
                "liquidation_px",
                "position_value_usd",
                "leverage_type",
            ],
        )
        if scan is None:
            continue
        collected = (
            scan.filter(pl.col("liquidation_px").is_not_null())
            .sort("rx_ms")
            .group_by("address", "coin")
            .last()
            .collect()
        )
        if not collected.is_empty():
            parts.append(collected)
    return pl.concat(parts) if parts else pl.DataFrame()


def reality_table(args: object, bands: pl.DataFrame) -> str:
    """Projected bands against the liquidation prices `hl_positions` actually recorded."""
    positions = recorded_positions(args)
    if positions.is_empty():
        return (
            "_No `hl_positions` days in this lake. The recorder writes them forward only, "
            "so this table fills in once `liqfade sync` has brought some down._"
        )
    real = positions
    rows = []
    for kind in ("isolated", "cross"):
        part = real.filter(pl.col("leverage_type") == kind)
        if part.is_empty() or bands.is_empty():
            rows.append([kind, "0", "n/a", "n/a"])
            continue
        matched = (
            bands.select("coin", pl.col("px").alias("band_px"), "weight_usd")
            .join(part.select("coin", "liquidation_px", "position_value_usd"), on="coin")
            .with_columns(
                (
                    1e4 * (pl.col("liquidation_px") - pl.col("band_px")).abs() / pl.col("band_px")
                ).alias("_dist")
            )
            .filter(pl.col("_dist") <= 25.0)
            .group_by("band_px")
            .agg(
                pl.col("position_value_usd").sum().alias("real_usd"),
                pl.col("weight_usd").first(),
            )
        )
        share = matched.height / max(bands["px"].n_unique(), 1)
        correlation = (
            matched.select(pl.corr("weight_usd", "real_usd", method="spearman")).item()
            if matched.height > 2
            else None
        )
        # A constant projected weight has no rank in it, so the correlation is undefined
        # rather than zero and the table has to say so.
        if correlation is not None and correlation != correlation:
            correlation = None
        rows.append(
            [
                kind,
                f"${part['position_value_usd'].sum() / 1e6:,.0f}M",
                f"{share:.0%}",
                f"{correlation:.2f}" if correlation is not None else "n/a",
            ]
        )
    return lib.table(
        ["margin", "real notional with a level", "bands with any real notional", "rank corr"],
        rows,
    )


# --- verdicts ------------------------------------------------------------------------


def reaction_verdict(results: dict[int, tuple[lib.Estimate, lib.Estimate]]) -> str:
    ladder, control = results.get(15, (None, None))
    if ladder is None or control is None or min(ladder.n, control.n) < MIN_CELL:
        return lib.decision(
            "inconclusive",
            f"Fewer than {MIN_CELL} priced band touches at +15 minutes in one of the two "
            f"arms, so there is nothing to compare.",
        )
    if ladder.lo > control.hi:
        return lib.decision(
            "yes",
            f"At +15 minutes the ladder is {ladder.render()} against the shifted control's "
            f"{control.render()}, and the intervals do not overlap. Real leverage distances "
            f"mark reactions that arbitrary distances the same scale away do not.",
        )
    if control.lo > ladder.hi:
        return lib.decision(
            "no",
            f"At +15 minutes the shifted control beats the ladder, {control.render()} "
            f"against {ladder.render()}, with the intervals separated. Whatever the bands "
            f"mark, it is not the leverage level.",
        )
    return lib.decision(
        "inconclusive",
        f"At +15 minutes the ladder is {ladder.render()} and the shifted control is "
        f"{control.render()}. The intervals overlap, so the ladder is not distinguishable "
        f"from a band the same distance scale away.",
    )


def confluence_verdict(
    results: dict[float, tuple[lib.Estimate, lib.Estimate]], qualifying: bool, facts: str
) -> str:
    if not qualifying:
        return lib.decision(
            "inconclusive", f"The sample does not clear the preregistered bar: {facts}."
        )
    separated = [
        (width, inside, outside)
        for width, (inside, outside) in results.items()
        if inside.n >= MIN_CELL and inside.lo > outside.hi
    ]
    if separated:
        width, inside, outside = max(separated, key=lambda item: item[1].mean - item[2].mean)
        return lib.decision(
            "yes",
            f"At a {width:g} bps band width, in-band episodes are {inside.render()} against "
            f"{outside.render()} out of band, on {inside.n} in-band episodes. The next "
            f"prompt is one confluence rule in `strategy/rules.py`, disabled by default, "
            f"plus a Q8-style stop and target grid keyed on band distance. {facts}.",
        )
    return lib.decision(
        "no",
        f"No band width separates the two intervals, which is the same answer Q7 gave for "
        f"raw levels and for the same episodes. `features/liqmap.py` stays in the repo and "
        f"stays off, beside the zone code, so nobody re-derives this. {facts}.",
    )


def fuel_verdict(results: dict[str, lib.Estimate], stop_rates: dict[str, float]) -> str:
    low, high = results.get("low fuel"), results.get("high fuel")
    if low is None or high is None or min(low.n, high.n) < MIN_CELL:
        return lib.decision(
            "inconclusive",
            f"Fewer than {MIN_CELL} episodes in a fuel tercile, which is the preregistered "
            f"floor for a cell.",
        )
    difference = low.mean - high.mean
    same_sign = stop_rates.get("low fuel", 1.0) <= stop_rates.get("high fuel", 0.0)
    if low.lo > high.hi and same_sign:
        return lib.decision(
            "yes",
            f"The low-fuel tercile reverts {low.render()} against the high-fuel tercile's "
            f"{high.render()} at +15 minutes, in the preregistered direction, and the "
            f"stop-out rate moves the same way. An exhaustion feature follows, and it "
            f"merges with whatever Q9b concludes.",
        )
    if high.lo > low.hi:
        return lib.decision(
            "no",
            f"The high-fuel tercile reverts better, {high.render()} against {low.render()}, "
            f"which is the opposite of the preregistered direction.",
        )
    return lib.decision(
        "inconclusive",
        f"Low fuel is {low.render()} and high fuel is {high.render()} at +15 minutes, a "
        f"difference of {difference:+.1f} bps whose interval does not exclude zero.",
    )


# --- the run -------------------------------------------------------------------------


def banded(args: object, settings: object, episodes: pl.DataFrame) -> pl.DataFrame:
    """Attach the map columns to every episode, a month at a time, asked as of the start."""
    levels_cfg = settings.features.levels  # type: ignore[attr-defined]
    cfg = settings.features.liqmap  # type: ignore[attr-defined]
    ladder = list(cfg.ladder)
    parts = []
    for first, last in lib.month_chunks(args.start, args.end):  # type: ignore[attr-defined]
        load_start, load_end = lib.padded(first, last)
        candles = lib.load_table(args.data_dir, "candles_1m", load_start, load_end)  # type: ignore[attr-defined]
        if candles.is_empty():
            continue
        window = (pl.col("start_ts") >= lib.ms(first)) & (
            pl.col("start_ts") < lib.ms(last) + 86_400_000
        )
        month = episodes.filter(window)
        if month.is_empty():
            continue
        oi = lib.load_table(
            args.data_dir,  # type: ignore[attr-defined]
            "asset_ctx",
            load_start,
            load_end,
            columns=["coin", "ts_ms", "open_interest"],
        )
        levels = anchors(candles, levels_cfg)
        bands = bands_of(levels, oi, cfg, ladder)
        measured = bands_of(levels, oi, cfg, ladder, measured=True)
        if bands.is_empty():
            continue
        # Two queries per episode, exactly as Q7 asks its zone question. Q10b is about
        # where the cascade *ended*, so it asks at the extreme; Q10c is about the flow the
        # cascade still had to burn through, so it asks at the start. Both are timed at the
        # cascade start, so no band the cascade itself created is ever visible.
        asked = month.select("key", "coin", pl.col("start_ts").alias("ts_ms"))
        at_extreme = asked.with_columns(month["extreme_px"].alias("px"))
        at_start = asked.with_columns(month["anchor_px"].alias("px"))
        found = map_features(
            bands,
            at_extreme,
            band_width_bps=cfg.band_width_bps,
            fuel_window_bps=cfg.fuel_window_bps,
        ).select(
            "key",
            "dist_to_band_below_bps",
            "dist_to_band_above_bps",
            "band_leverage",
            "bands_stacked",
        )
        fuel_at_start = map_features(
            bands,
            at_start,
            band_width_bps=cfg.band_width_bps,
            fuel_window_bps=cfg.fuel_window_bps,
        ).select("key", "fuel_below_usd", "fuel_above_usd")
        # Distances and the stacked count do not depend on weights at all, so only the fuel
        # columns are recomputed for the secondary scheme.
        secondary = map_features(
            measured,
            at_start,
            band_width_bps=cfg.band_width_bps,
            fuel_window_bps=cfg.fuel_window_bps,
        ).select(
            "key",
            pl.col("fuel_below_usd").alias("_measured_below"),
            pl.col("fuel_above_usd").alias("_measured_above"),
        )
        parts.append(
            month.join(found, on="key", how="left")
            .join(fuel_at_start, on="key", how="left")
            .join(secondary, on="key", how="left")
        )
    if not parts:
        return pl.DataFrame()
    joined = pl.concat(parts, how="diagonal_relaxed")
    # A long liquidation is forced selling, so the cascade runs down and the fuel it must
    # burn through is the weight below it. A short liquidation is the mirror.
    downhill = pl.col("side") == "long"
    return joined.with_columns(
        pl.when(downhill)
        .then(pl.col("fuel_below_usd"))
        .otherwise(pl.col("fuel_above_usd"))
        .alias("fuel_ahead_usd"),
        pl.when(downhill)
        .then(pl.col("_measured_below"))
        .otherwise(pl.col("_measured_above"))
        .alias("fuel_ahead_measured_usd"),
    )


def touch_arms(args: object, settings: object) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Q10a's two arms and the bands themselves, built a month at a time."""
    levels_cfg = settings.features.levels  # type: ignore[attr-defined]
    cfg = settings.features.liqmap  # type: ignore[attr-defined]
    ladder = list(cfg.ladder)
    control = shifted_ladder(ladder, cfg.mm_frac)
    real_parts, control_parts, band_parts = [], [], []
    for first, last in lib.month_chunks(args.start, args.end):  # type: ignore[attr-defined]
        load_start, load_end = lib.padded(first, last)
        candles = lib.load_table(args.data_dir, "candles_1m", load_start, load_end)  # type: ignore[attr-defined]
        if candles.is_empty():
            continue
        ctx = lib.load_table(
            args.data_dir,  # type: ignore[attr-defined]
            "asset_ctx",
            load_start,
            load_end,
            columns=["coin", "ts_ms", "open_interest", "impact_bid_px", "impact_ask_px", "mid_px"],
        )
        oi = ctx
        levels = anchors(candles, levels_cfg)
        real = bands_of(levels, oi, cfg, ladder)
        fake = bands_of(levels, oi, cfg, control)
        if not real.is_empty():
            # Q10d asks how projected weight ranks against real notional, and a uniform
            # weight has no ranking in it, so the bands it keeps are the measured ones.
            band_parts.append(bands_of(levels, oi, cfg, ladder, measured=True))
            real_parts.append(touches(real, candles, ctx))
        if not fake.is_empty():
            control_parts.append(touches(fake, candles, ctx))
    return (
        _stack(real_parts),
        _stack(control_parts),
        _stack(band_parts),
    )


def _stack(parts: list[pl.DataFrame]) -> pl.DataFrame:
    kept = [part for part in parts if not part.is_empty()]
    return pl.concat(kept, how="diagonal_relaxed") if kept else pl.DataFrame()


def stop_rates(args: object, episodes: pl.DataFrame) -> dict[str, float]:
    """Stop-out rate per fuel tercile in Q8's winning cell, from Q8's own code.

    Imported rather than copied: `_lib.attach_outcomes` is public for exactly this reason,
    and a second implementation of the stop and target resolution would be a second chance
    to get the cost model wrong.
    """
    import q8_path

    frame = episodes.drop_nulls("fuel_ahead_usd")
    if frame.is_empty():
        return {}
    cuts = [frame["fuel_ahead_usd"].quantile(third) for third in (1 / 3, 2 / 3)]
    labelled = frame.with_columns(
        pl.when(pl.col("fuel_ahead_usd") <= cuts[0])
        .then(pl.lit("low fuel"))
        .when(pl.col("fuel_ahead_usd") <= cuts[1])
        .then(pl.lit("middle"))
        .otherwise(pl.lit("high fuel"))
        .alias("tercile")
    )
    resolved = []
    for first, last in lib.week_chunks(args.start, args.end):  # type: ignore[attr-defined]
        window = (pl.col("start_ts") >= lib.ms(first)) & (
            pl.col("start_ts") < lib.ms(last) + 86_400_000
        )
        chunk = labelled.filter(window)
        if chunk.is_empty():
            continue
        tape = lib.episode_tape(args.data_dir, chunk, horizon_s=900)  # type: ignore[attr-defined]
        if tape.is_empty():
            continue
        priced = q8_path.outcomes(q8_path.levels(chunk, K_STOP, K_TARGET), tape)
        resolved.append(priced.select("tercile", "outcome"))
    if not resolved:
        return {}
    every = pl.concat(resolved)
    return {
        row["tercile"]: row["stopped"]
        for row in every.group_by("tercile")
        .agg((pl.col("outcome") == "stop").mean().alias("stopped"))
        .to_dicts()
    }


def _rates_line(rates: dict[str, float]) -> str:
    return ", ".join(f"{name} {rate:.0%}" for name, rate in sorted(rates.items())) or "not computed"


def main() -> None:
    args = lib.parse_args(__doc__ or "")
    settings = load_settings()
    out_dir = Path(args.out_dir)
    report = out_dir / "q10_ladder.md"
    title = "Q10: the projected liquidation map"
    cfg = settings.features.liqmap
    episodes = lib.build_episodes(
        args.data_dir, args.start, args.end, settings, cache=out_dir / "episodes.parquet"
    )
    preamble = [
        f"Window: {args.start} to {args.end}. Ladder: "
        f"{', '.join(f'{rung:g}x' for rung in cfg.ladder)} at a {cfg.mm_frac:.3f} maintenance "
        f"margin, capped {cfg.max_dist_bps:g} bps from the anchor. Anchors are untouched "
        f"swing and range extremes from `features/levels.py`, unchanged from Q7's. Rung "
        f"weights are **uniform**: the histogram is the preregistered secondary and is not "
        f"what any DECISION below reads.",
        "",
        "**Q7 failed the neighbouring claim and this question inherits its burden of "
        "proof.** Q7's own rows are printed beside Q10b's.",
        "",
    ]
    if episodes.is_empty():
        lib.write_report(
            report,
            title,
            [
                *preamble,
                f"No liquidations between {args.start} and {args.end}.",
                lib.decision("inconclusive", "There is no data to answer Q10a."),
                lib.decision("inconclusive", "There is no data to answer Q10b."),
                lib.decision("inconclusive", "There is no data to answer Q10c."),
            ],
        )
        print(f"wrote {report}")
        return

    episodes = lib.with_cost_columns(
        episodes.filter(pl.col("qualified")).with_columns(pl.col("mid_plus_15m").alias("exit_px"))
    )
    ladder_touches, control_touches, every_band = touch_arms(args, settings)
    reaction, reaction_results = reaction_table(ladder_touches, control_touches)
    with_bands = banded(args, settings, episodes)
    if with_bands.is_empty():
        lib.write_report(
            report,
            title,
            [
                *preamble,
                "No 1m candles in the lake over this window. Anchors are built from "
                "`candles_1m`, which `liqfade derive candles` produces from trades.",
                "## Q10a, reaction at a band",
                "",
                reaction,
                reaction_verdict(reaction_results),
                lib.decision("inconclusive", "There are no candles to build anchors from."),
                lib.decision("inconclusive", "There are no candles to build anchors from."),
            ],
        )
        print(f"wrote {report}")
        return

    confluence, confluence_results = confluence_table(with_bands)
    qualifying, facts = lib.qualifies(with_bands)
    fuel, fuel_results = fuel_table(with_bands)
    rates = stop_rates(args, with_bands)
    blocks = [
        *preamble,
        "## Q10a, reaction at a projected band",
        "",
        "Every band price first reaches over the window, against a control whose rungs are "
        "moved by half the gap to the next rung: the same anchors, the same distance scale, "
        "and the only thing varying is whether the distance is a real leverage level. Entry "
        "is the close of the bar that touched the band, which is up to 60s later than the "
        "preregistered entry and therefore captures less of any reaction.",
        "",
        reaction,
        f"\nLadder touches: **{ladder_touches.height}**. Control touches: "
        f"**{control_touches.height}**.",
        reaction_verdict(reaction_results),
        "## Q10b, cascade confluence",
        "",
        "Q7's question with projected bands in place of raw levels, on Q7's own episodes. "
        "Asked at the cascade **extreme**, which is where the cascade ended and what Q7 "
        "measured, but timed at the cascade start so no band the cascade itself created is "
        "visible. Q10c's fuel below is asked at the start price instead, because that is "
        "the flow the cascade still had ahead of it.",
        "",
        confluence,
        "",
        "### By how many distinct anchors stack",
        "",
        "Confluence between anchors is the actual claim; one anchor's own adjacent rungs "
        "are not evidence and are never counted twice.",
        "",
        stacked_table(with_bands, cfg.band_width_bps),
        confluence_verdict(confluence_results, qualifying, facts),
        "## Q10c, fuel and exhaustion",
        "",
        f"Terciles of `fuel_ahead_usd`, the weight of bands the cascade would have to run "
        f"through in its own direction within {cfg.fuel_window_bps:g} bps. Preregistered "
        f"direction: **low fuel reverts better**, because the forced flow ahead is spent. "
        f"Stop-out rates are Q8's winning cell, k_stop={K_STOP:g} and "
        f"k_target={K_TARGET:g}: {_rates_line(rates)}.",
        "",
        fuel,
        fuel_verdict(fuel_results, rates),
        "### The preregistered secondary: open interest scaled, histogram weighted",
        "",
        "One run, reported once, with no DECISION of its own. If the dumb version above "
        "works we never need this one, and a weighting scheme tuned until a table passes is "
        "the failure mode the preregistration exists to prevent.",
        "",
        fuel_table(with_bands, "fuel_ahead_measured_usd")[0],
        "",
        "## Q10d, does the projection match reality",
        "",
        "Descriptive, no DECISION. Real notional whose recorded `liquidation_px` falls "
        "within 25 bps of a projected band, split by margin type because a cross-margin "
        "liquidation price moves with the rest of that account's book and 98% of the "
        "tracked notional is cross.",
        "",
        reality_table(args, every_band),
    ]
    path = lib.write_report(report, title, blocks)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
