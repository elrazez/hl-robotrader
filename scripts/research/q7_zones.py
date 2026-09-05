"""Q7: does a cascade that ends on a pre-existing level revert better than one in open air?

The owner's addition to the six. Split Q1's episodes by whether the cascade extreme landed
inside a supply or demand zone that already existed when the cascade started, and compare
net-stress reversion, hit rate and the loss tail. Tested at zone widths 10, 15 and 25 bps.

The reverse use is tested too: does distance to the nearest level at the cascade **start**
predict how far the cascade goes on to extend? That is a stop-placement input rather than
an entry filter, and it is reported whether or not the entry question passes.

DECISION yes if in-zone episodes beat out-of-zone net-stress reversion with the
cluster-bootstrap 95% intervals separated, on at least 100 in-zone episodes. A yes wires
the zone rule into Phase 4 as an optional confluence gate; a no leaves it built but off.

Levels come from `features/levels.py`: fractal swings plus rolling and prior-period range
edges, each carrying the moment it became knowable. Only levels with
`known_from <= cascade start` are considered, so a cascade cannot land in a level its own
move created.

    uv run python scripts/research/q7_zones.py --start 2025-09-01 --end 2026-08-31
"""

from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).parent))

import _lib as lib

from liqfade.config.loader import load_settings
from liqfade.features.levels import range_levels, swing_levels, zone_features

ZONE_WIDTHS_BPS = (10.0, 15.0, 25.0)
MIN_IN_ZONE = 100


def zoned(args: object, settings: object, episodes: pl.DataFrame) -> pl.DataFrame:
    """Attach the zone columns to every episode, a month at a time."""
    cfg = settings.features.levels  # type: ignore[attr-defined]
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
        levels = pl.concat(
            [
                swing_levels(candles, fractal_n=cfg.fractal_n),
                range_levels(candles, range_d=cfg.range_d),
            ]
        )
        # Two queries per episode: the extreme, which is what `extreme_in_zone` is about,
        # and the start, which is the stop-placement input. Both are asked as of the
        # cascade start, so no level the cascade itself created can be seen.
        extremes = month.select(
            "key", "coin", pl.col("start_ts").alias("ts_ms"), pl.col("extreme_px").alias("px")
        )
        starts = month.select(
            "key", "coin", pl.col("start_ts").alias("ts_ms"), pl.col("anchor_px").alias("px")
        )
        at_extreme = zone_features(
            levels,
            extremes,
            candles,
            max_dist_bps=cfg.max_dist_bps,
            zone_width_bps=max(ZONE_WIDTHS_BPS),
        )
        at_start = zone_features(
            levels,
            starts,
            candles,
            max_dist_bps=cfg.max_dist_bps,
            zone_width_bps=cfg.zone_width_bps,
        ).select(
            "key",
            pl.min_horizontal("dist_to_support_bps", "dist_to_resistance_bps").alias(
                "dist_at_start_bps"
            ),
        )
        parts.append(
            month.join(
                at_extreme.select(
                    "key",
                    "dist_to_support_bps",
                    "dist_to_resistance_bps",
                    "support_touches",
                    "resistance_touches",
                    "support_fresh",
                    "resistance_fresh",
                ),
                on="key",
                how="left",
            ).join(at_start, on="key", how="left")
        )
    return pl.concat(parts, how="diagonal_relaxed") if parts else pl.DataFrame()


def in_zone(width_bps: float) -> pl.Expr:
    nearest = pl.min_horizontal("dist_to_support_bps", "dist_to_resistance_bps")
    return (nearest <= width_bps).fill_null(False)


def width_table(
    episodes: pl.DataFrame,
) -> tuple[str, dict[float, tuple[lib.Estimate, lib.Estimate]]]:
    """In-zone against out-of-zone at each width, with both intervals so they can be read apart."""
    rows = []
    results: dict[float, tuple[lib.Estimate, lib.Estimate]] = {}
    for width in ZONE_WIDTHS_BPS:
        inside = episodes.filter(in_zone(width)).drop_nulls("net_stress_bps")
        outside = episodes.filter(~in_zone(width)).drop_nulls("net_stress_bps")
        estimates = tuple(
            lib.cluster_bootstrap(part["net_stress_bps"].to_numpy(), part["cluster"].to_numpy())
            for part in (inside, outside)
        )
        results[width] = estimates  # type: ignore[assignment]
        rows.append(
            [
                f"{width:g} bps",
                estimates[0].render(),
                estimates[1].render(),
                f"{hit_rate(inside):.0%}",
                f"{hit_rate(outside):.0%}",
                f"{stopped_share(inside):.0%}",
                f"{stopped_share(outside):.0%}",
            ]
        )
    header = [
        "zone width",
        "in zone, net-stress",
        "out of zone, net-stress",
        "hit rate in",
        "hit rate out",
        "loss tail in",
        "loss tail out",
    ]
    return lib.table(header, rows), results


def hit_rate(part: pl.DataFrame) -> float:
    if part.is_empty():
        return 0.0
    return part.filter(pl.col("net_stress_bps") > 0).height / part.height


def stopped_share(part: pl.DataFrame) -> float:
    """The loss tail here is the worst quartile by net-stress, since Q8 owns the stop grid."""
    if part.is_empty():
        return 0.0
    cut = part["net_stress_bps"].quantile(0.25)
    return part.filter(pl.col("net_stress_bps") <= cut).height / part.height


def extension_table(episodes: pl.DataFrame) -> str:
    """Does room to the nearest level at the start predict how far the cascade extends?"""
    rows = []
    frame = episodes.drop_nulls("dist_at_start_bps")
    if frame.is_empty():
        return "_No episode had a level within the distance cap at its start._"
    for label, low, high in (
        ("under 25 bps of room", 0.0, 25.0),
        ("25 to 75 bps", 25.0, 75.0),
        ("75 to 150 bps", 75.0, 150.0),
        ("over 150 bps", 150.0, float("inf")),
    ):
        part = frame.filter(
            (pl.col("dist_at_start_bps") >= low) & (pl.col("dist_at_start_bps") < high)
        )
        extension = part["move_bps"].mean() if not part.is_empty() else None
        rows.append(
            [label, str(part.height), f"{extension:.1f}" if extension is not None else "n/a"]
        )
    return lib.table(["room to the nearest level at start", "episodes", "mean move bps"], rows)


def verdict(
    results: dict[float, tuple[lib.Estimate, lib.Estimate]], qualifying: bool, facts: str
) -> str:
    if not qualifying:
        return lib.decision(
            "inconclusive", f"The sample does not clear the preregistered bar: {facts}."
        )
    separated = [
        (width, inside, outside)
        for width, (inside, outside) in results.items()
        if inside.n >= MIN_IN_ZONE and inside.lo > outside.hi
    ]
    if separated:
        width, inside, outside = max(separated, key=lambda item: item[1].mean - item[2].mean)
        return lib.decision(
            "yes",
            f"At a {width:g} bps zone width, in-zone episodes are {inside.render()} against "
            f"{outside.render()} out of zone, and the two 95% intervals do not overlap on "
            f"{inside.n} in-zone episodes. Phase 4 gets the zone rule as an **optional "
            f"confluence gate**, off by default until a backtest confirms it. {facts}.",
        )
    thin = [width for width, (inside, _) in results.items() if inside.n < MIN_IN_ZONE]
    reason = (
        f"Fewer than {MIN_IN_ZONE} in-zone episodes at width{'s' if len(thin) > 1 else ''} "
        f"{', '.join(f'{width:g}' for width in thin)}. "
        if thin
        else ""
    )
    return lib.decision(
        "no",
        f"{reason}No zone width separates the two intervals. The zone code stays in the "
        f"repo and stays off: it is built, tested and unused, which is cheaper than "
        f"deleting it and rebuilding it in Phase 7 if the picture changes. {facts}.",
    )


def main() -> None:
    args = lib.parse_args(__doc__ or "")
    settings = load_settings()
    out_dir = Path(args.out_dir)
    title = "Q7: supply and demand zone confluence"
    episodes = lib.build_episodes(
        args.data_dir, args.start, args.end, settings, cache=out_dir / "episodes.parquet"
    )
    if episodes.is_empty():
        lib.write_report(
            out_dir / "q7_zones.md",
            title,
            [
                f"No liquidations between {args.start} and {args.end}.",
                lib.decision("inconclusive", "There is no data to answer this question."),
            ],
        )
        return
    episodes = lib.with_cost_columns(
        episodes.filter(pl.col("qualified")).with_columns(pl.col("mid_plus_15m").alias("exit_px"))
    )
    episodes = zoned(args, settings, episodes)
    if episodes.is_empty():
        lib.write_report(
            out_dir / "q7_zones.md",
            title,
            [
                "No 1m candles in the lake over this window. Zones are built from "
                "`candles_1m`, which `liqfade derive candles` produces from trades.",
                lib.decision("inconclusive", "There are no candles to build levels from."),
            ],
        )
        return
    table, results = width_table(episodes)
    qualifying, facts = lib.qualifies(episodes)
    blocks = [
        f"Window: {args.start} to {args.end}. Qualifying episodes: **{episodes.height}**. "
        f"Levels are fractal swings plus rolling and prior-period range edges, each usable "
        f"only from the bar that confirms it. Every zone question is asked as of the cascade "
        f"start, so a level the cascade itself created is invisible.",
        "",
        "## In zone against out of zone, by zone width",
        "",
        "The loss tail here is the worst quartile by net-stress, not Q8's stopped-out set: "
        "this question is about where a cascade ends, and Q8 owns the stop grid.",
        "",
        table,
        "",
        "## The reverse use: does room predict extension?",
        "",
        "If a cascade that starts far from any level runs further, distance to the nearest "
        "level is a stop-placement input even when it is not an entry filter.",
        "",
        extension_table(episodes),
        verdict(results, qualifying, facts),
    ]
    path = lib.write_report(out_dir / "q7_zones.md", title, blocks)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
