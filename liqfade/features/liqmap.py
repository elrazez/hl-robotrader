"""The projected liquidation map: where the people who bought the extreme get closed out.

`levels.py` says where price turned. This says where the traders who entered at that turn
are liquidated. A trader who opened at a swing extreme with leverage `L` is force-closed a
fixed fraction away, `d = 1/L - m`, so one anchor projects a whole ladder of prices, one
rung per rung of the exchange's leverage menu. Where ladders from several different anchors
land on the same price, that price is where forced flow concentrates, and the confluence
count is the claim the whole idea rests on.

The ladder is percentages, never dollars. The public indicator this is decoded from draws
it in dollars and therefore only works on one coin at one price era; see
`docs/research/projected_liqmap.md`.

Pure functions on polars frames: no I/O, no clock, no live-only branch, so a backtest
replay and the live loop run the same code. Whether any of it measures anything is Q10,
preregistered in `reports/research/PREREGISTRATION_Q10.md`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import polars as pl

__all__ = ["band_weights", "ladder_bands", "map_features"]

MINUTE_MS = 60_000

BAND_COLUMNS = (
    "coin",
    "ts_ms",
    "px",
    "side",
    "known_from",
    "anchor_px",
    "leverage",
    "source",
)


def ladder_bands(
    levels: pl.DataFrame,
    *,
    ladder: Sequence[float],
    mm_frac: float,
    max_dist_bps: float,
) -> pl.DataFrame:
    """Project one band per anchor per rung of the leverage ladder.

    Args:
        levels: rows from `features.levels.swing_levels` or `range_levels`. Needs `coin`,
            `ts_ms`, `px`, `kind`, `known_from`, `source`.
        ladder: leverage multiples, e.g. `[3, 5, 10, 15, 20, 25, 40, 50]`. From
            `features.liqmap.ladder`.
        mm_frac: maintenance margin as a fraction of notional, e.g. 0.005. From
            `features.liqmap.mm_frac`.
        max_dist_bps: bands further than this from their own anchor are dropped, in bps of
            the anchor price. From `features.liqmap.max_dist_bps`.

    Returns:
        `coin`, `ts_ms` (the anchor's, so two rungs of one anchor stay one anchor), `px`
        (the band price), `side` (`long_liq` where trapped longs are force-sold,
        `short_liq` where trapped shorts are force-bought), `known_from` (the anchor's,
        copied unchanged, never recomputed), `anchor_px`, `leverage` and `source`.

        A `resistance` anchor traps longs, so its bands sit **below** it at
        `px * (1 - (1/L - mm_frac))`. A `support` anchor traps shorts, so its bands sit
        **above** at `px * (1 + (1/L - mm_frac))`. A rung whose `1/L` is at or below
        `mm_frac` projects nothing, since the position is liquidated where it is opened.
    """
    # ponytail: one flat maintenance margin for every coin. Hyperliquid's real rule is half
    # the initial margin at the asset's own max leverage, so the exact value is per coin,
    # but at the rungs here it shifts a band by well under a tenth of the gap to the next
    # rung. Upgrade, if a per-coin table ever earns its keep: join `maxLeverage` per coin
    # off `hl_positions` or `meta` and take `mm_frac` from that instead of this scalar.
    rungs = [
        (float(leverage), 1.0 / float(leverage) - mm_frac)
        for leverage in ladder
        if float(leverage) > 0.0 and 1.0 / float(leverage) > mm_frac
    ]
    if levels.is_empty() or not rungs:
        return _empty_bands()
    trapped = {"resistance": ("long_liq", -1.0), "support": ("short_liq", 1.0)}
    parts = [
        levels.filter(pl.col("kind") == kind).select(
            "coin",
            "ts_ms",
            (pl.col("px") * (1.0 + direction * distance)).alias("px"),
            pl.lit(side).alias("side"),
            "known_from",
            pl.col("px").alias("anchor_px"),
            pl.lit(leverage).alias("leverage"),
            "source",
        )
        for kind, (side, direction) in trapped.items()
        for leverage, distance in rungs
        if distance * 1e4 <= max_dist_bps
    ]
    kept = [part for part in parts if not part.is_empty()]
    return pl.concat(kept).sort("coin", "known_from", "px") if kept else _empty_bands()


def band_weights(
    bands: pl.DataFrame,
    oi: pl.DataFrame,
    *,
    histogram: Mapping[float, float],
    half_life_min: float,
) -> pl.DataFrame:
    """Attach one notional weight per band, before decay.

    Args:
        bands: rows from `ladder_bands`.
        oi: `asset_ctx` rows. Needs `coin`, `ts_ms`, `open_interest` in coin units.
        histogram: leverage multiple -> share of notional opened at that leverage, e.g.
            `{10.0: 0.32, 20.0: 0.18}`. A rung the histogram does not name carries no
            weight. From `features.liqmap.histogram`.
        half_life_min: minutes after which a band carries half its weight. Carried out as
            a column rather than applied here, because the decay depends on when the band
            is *asked about* and one frame of bands is asked about at many times.

    Returns:
        `bands` plus `weight_usd` and `half_life_min`. `weight_usd` is the open interest
        that appeared while the anchor was forming, in USD at the anchor price, times the
        rung's share. An anchor whose window shows no increase weighs **zero, not null**: a
        null would silently drop the band out of every sum it belongs in.

        The window is the anchor's own bar to the moment it became knowable, which is the
        money that opened while price was at the extreme. Hyperliquid reports open interest
        as both sides of the book, so every weight here is twice a one-sided notional; that
        is a constant factor and cancels in every comparison this module is used for.
    """
    if bands.is_empty():
        return bands.with_columns(
            pl.lit(0.0).alias("weight_usd"), pl.lit(half_life_min).alias("half_life_min")
        )
    share = pl.col("leverage").replace_strict(
        {float(rung): float(value) for rung, value in histogram.items()},
        default=0.0,
        return_dtype=pl.Float64,
    )
    anchors = bands.select("coin", "ts_ms", "known_from", "anchor_px").unique()
    opened = _opened_usd(anchors, oi)
    return (
        bands.join(opened, on=["coin", "ts_ms", "anchor_px"], how="left")
        .with_columns(
            (pl.col("_opened_usd").fill_null(0.0) * share).alias("weight_usd"),
            pl.lit(half_life_min).alias("half_life_min"),
        )
        .drop("_opened_usd")
    )


def _opened_usd(anchors: pl.DataFrame, oi: pl.DataFrame) -> pl.DataFrame:
    """Open interest that appeared between an anchor's bar and its confirmation, in USD."""
    if oi.is_empty():
        return anchors.with_columns(pl.lit(0.0).alias("_opened_usd")).drop("known_from")
    samples = oi.select("coin", "ts_ms", "open_interest").sort("ts_ms")
    at = {}
    for edge in ("ts_ms", "known_from"):
        at[edge] = (
            anchors.sort(edge)
            .join_asof(samples, left_on=edge, right_on="ts_ms", by="coin", strategy="backward")
            .select("coin", "ts_ms", "anchor_px", pl.col("open_interest").alias(f"_oi_{edge}"))
        )
    joined = at["ts_ms"].join(
        at["known_from"].select("coin", "ts_ms", "anchor_px", "_oi_known_from"),
        on=["coin", "ts_ms", "anchor_px"],
        how="left",
    )
    # Clipped at zero: interest that left while the anchor formed is not negative fuel.
    increase = (pl.col("_oi_known_from") - pl.col("_oi_ts_ms")).fill_null(0.0).clip(lower_bound=0.0)
    return joined.select(
        "coin", "ts_ms", "anchor_px", (increase * pl.col("anchor_px")).alias("_opened_usd")
    )


def map_features(
    bands: pl.DataFrame,
    queries: pl.DataFrame,
    *,
    band_width_bps: float,
    fuel_window_bps: float,
) -> pl.DataFrame:
    """What the projected map looks like from one price at one moment.

    Shaped like `features.levels.zone_features`, so the two can be read side by side and a
    caller can ask both questions of the same query frame.

    Args:
        bands: rows from `band_weights`. Needs its `weight_usd` and `half_life_min`.
        queries: `key`, `coin`, `ts_ms`, `px`. One row per question, typically one per
            cascade start or per band approach.
        band_width_bps: how close to a band counts as being in it, and how close two
            anchors' bands must be to count as stacked.
        fuel_window_bps: how far each way the forced flow is summed over.

    Returns:
        `queries` plus `dist_to_band_below_bps` and `dist_to_band_above_bps` (null when no
        band of that side is known yet, never capped: `ladder_bands` already dropped the
        rungs too far from their own anchor), `in_band`, `band_leverage` (the nearest
        band's rung either way, null when none), `bands_stacked` (**distinct anchors**, not
        bands, with a band within `band_width_bps`, so one anchor's two adjacent rungs
        count once) and `fuel_below_usd` / `fuel_above_usd`, the decayed weight inside
        `fuel_window_bps` each way.

        Weight decays as `0.5 ** (age_min / half_life_min)` from the anchor's `known_from`,
        because positions opened at a swing do not live forever.

        **Only bands with `known_from <= ts_ms` are considered.** A band whose anchor was
        not yet confirmed at the query is invisible, which is the whole no-lookahead
        contract this module inherits from `levels.py`.
    """
    blank = queries.with_columns(
        pl.lit(None, dtype=pl.Float64).alias("dist_to_band_below_bps"),
        pl.lit(None, dtype=pl.Float64).alias("dist_to_band_above_bps"),
        pl.lit(False).alias("in_band"),
        pl.lit(None, dtype=pl.Float64).alias("band_leverage"),
        pl.lit(0, dtype=pl.UInt32).alias("bands_stacked"),
        pl.lit(0.0).alias("fuel_below_usd"),
        pl.lit(0.0).alias("fuel_above_usd"),
    )
    if bands.is_empty() or queries.is_empty():
        return blank
    age_min = (pl.col("ts_ms") - pl.col("known_from")) / MINUTE_MS
    signed = 1e4 * (pl.col("_band_px") - pl.col("px")) / pl.col("px")
    seen = (
        queries.select("key", "coin", "ts_ms", "px")
        .join(
            bands.select(
                "coin",
                pl.col("px").alias("_band_px"),
                "ts_ms",
                "known_from",
                "anchor_px",
                "leverage",
                "weight_usd",
                "half_life_min",
            ).rename({"ts_ms": "_anchor_ts"}),
            on="coin",
        )
        .filter(pl.col("known_from") <= pl.col("ts_ms"))
        .with_columns(signed.alias("_signed"))
        .with_columns(
            pl.col("_signed").abs().alias("_dist"),
            (pl.col("weight_usd") * (0.5 ** (age_min / pl.col("half_life_min")))).alias("_fuel"),
        )
    )
    if seen.is_empty():
        return blank
    inside = pl.col("_dist") <= band_width_bps
    below = pl.col("_signed") < 0.0
    fuelled = pl.col("_dist") <= fuel_window_bps
    found = seen.group_by("key").agg(
        pl.col("_dist").filter(below).min().alias("dist_to_band_below_bps"),
        pl.col("_dist").filter(~below).min().alias("dist_to_band_above_bps"),
        pl.col("leverage").sort_by("_dist").first().alias("band_leverage"),
        # Distinct anchors, not distinct bands: two rungs of one swing are one trader's
        # ladder, and counting them twice would make every anchor look like confluence.
        pl.struct("_anchor_ts", "anchor_px").filter(inside).n_unique().alias("bands_stacked"),
        pl.col("_fuel").filter(fuelled & below).sum().alias("fuel_below_usd"),
        pl.col("_fuel").filter(fuelled & ~below).sum().alias("fuel_above_usd"),
    )
    out = queries.join(found, on="key", how="left").with_columns(
        pl.col("bands_stacked").fill_null(0),
        pl.col("fuel_below_usd").fill_null(0.0),
        pl.col("fuel_above_usd").fill_null(0.0),
    )
    nearest = pl.min_horizontal("dist_to_band_below_bps", "dist_to_band_above_bps")
    return out.with_columns((nearest <= band_width_bps).fill_null(False).alias("in_band"))


def _empty_bands() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "coin": pl.String,
            "ts_ms": pl.Int64,
            "px": pl.Float64,
            "side": pl.String,
            "known_from": pl.Int64,
            "anchor_px": pl.Float64,
            "leverage": pl.Float64,
            "source": pl.String,
        }
    )
