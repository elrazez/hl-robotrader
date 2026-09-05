"""Q4: when Bybit liquidations spike and Hyperliquid's do not, does HL still revert?

This decides how much cross-exchange data matters for v0, and Q3 decides whether it
decides anything at all: if the three-gate proxy clears its bar, the v0 trigger is
HL-native and this question is non-blocking until Phase 7. If it does not, this becomes a
Phase 4 entry requirement at a reduced bar of 15 clusters and 60 recorder days, because it
is then asking whether a cross-venue trigger fires on HL-reverting events rather than
establishing the edge, which Q1 already did on exact data.

The first cut runs on the twelve Tardis days that carry Binance, Bybit and OKX
liquidations alongside exact HL ones. Twelve days cannot reach any bar. What they can show
is whether a cross-venue cascade ever coincides with an HL episode at all, which is worth
knowing before the recorder has run for two months.

A Bybit episode is orphaned when no HL episode on the same coin and side starts within
120s of it. For each orphan, the same reversion measure Q1 uses, on HL's own tape.

    uv run python scripts/research/q4_crossvenue.py --start 2025-09-01 --end 2026-08-31
"""

from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).parent))

import _lib as lib

from liqfade.config.loader import load_settings
from liqfade.features.episodes import group_episodes

MATCH_WINDOW_MS = 120_000
REDUCED_CLUSTER_BAR = 15
# The other half of the reduced bar. Both must hold; the recorder has not started.
REDUCED_DAYS_BAR = 60


def bybit_episodes(args: object, settings: object) -> pl.DataFrame:
    """Bybit cascades, detected with the same rules and priced on Hyperliquid's own tape.

    Two stages for the same reason `build_episodes` has two: the gap joining is pure
    liquidation data, and the tape is only read in a window around what survives it.
    """
    cfg = settings.features.episodes  # type: ignore[attr-defined]
    load_start, load_end = lib.padded(args.start, args.end)  # type: ignore[attr-defined]
    liquidations = lib.load_table(  # type: ignore[attr-defined]
        args.data_dir, "liquidations", load_start, load_end, venue="bybit"
    )
    if liquidations.is_empty():
        return pl.DataFrame()
    detector = {
        "join_gap_s": cfg.join_gap_s,
        "peak_window_s": cfg.peak_window_s,
        "participation": cfg.participation,
        "participation_window_s": cfg.participation_window_s,
    }
    empty_bars = pl.DataFrame(
        schema={
            "coin": pl.String,
            "ts_ms": pl.Int64,
            "mid": pl.Float64,
            "notional_usd": pl.Float64,
        }
    )
    window = (pl.col("start_ts") >= lib.ms(args.start)) & (  # type: ignore[attr-defined]
        pl.col("start_ts") < lib.ms(args.end) + 86_400_000  # type: ignore[attr-defined]
    )
    # Only the absolute notional floor here: the trailing p95 is a per-coin rule built from
    # a venue's own episode history, and Bybit's history in this lake is twelve days.
    candidates = (
        group_episodes(liquidations, empty_bars, **detector)
        .filter(window)
        .filter(pl.col("total_notional") >= cfg.min_notional_usd)
    )
    if candidates.is_empty():
        return pl.DataFrame()
    grid = lib.episode_grid(args.data_dir, candidates)  # type: ignore[attr-defined]
    if grid.is_empty():
        return pl.DataFrame()
    move = 1e4 * (pl.col("anchor_px") - pl.col("extreme_px")).abs() / pl.col("anchor_px")
    kept = (
        group_episodes(liquidations, grid, **detector)
        .filter(window)
        .with_columns(move.alias("move_bps"))
        .filter(
            (pl.col("total_notional") >= cfg.min_notional_usd)
            & (pl.col("move_bps") >= cfg.min_move_bps)
        )
    )
    if kept.is_empty():
        return pl.DataFrame()
    # Outcomes come from Hyperliquid's tape, not Bybit's: the question is whether HL
    # reverts, and the same function Q1 uses answers it so the cost model has one home.
    return lib.attach_outcomes(kept, grid, args.data_dir, load_start, load_end)  # type: ignore[attr-defined]


def orphans(bybit: pl.DataFrame, hl: pl.DataFrame) -> pl.DataFrame:
    """Bybit episodes with no HL episode on the same coin and side inside the window."""
    if bybit.is_empty():
        return bybit
    if hl.is_empty():
        return bybit
    matched = (
        bybit.select("coin", "side", "start_ts")
        .join(hl.select("coin", "side", pl.col("start_ts").alias("_hl_ts")), on=["coin", "side"])
        .filter((pl.col("_hl_ts") - pl.col("start_ts")).abs() <= MATCH_WINDOW_MS)
        .select("coin", "side", "start_ts")
        .unique()
        .with_columns(pl.lit(True).alias("_matched"))
    )
    return (
        bybit.join(matched, on=["coin", "side", "start_ts"], how="left")
        .filter(pl.col("_matched").is_null())
        .drop("_matched")
    )


def main() -> None:
    args = lib.parse_args(__doc__ or "")
    settings = load_settings()
    out_dir = Path(args.out_dir)
    title = "Q4: cross-venue cascades that Hyperliquid did not see"
    hl = lib.build_episodes(
        args.data_dir, args.start, args.end, settings, cache=out_dir / "episodes.parquet"
    )
    hl_qualified = hl.filter(pl.col("qualified")) if not hl.is_empty() else hl
    bybit = bybit_episodes(args, settings)
    if bybit.is_empty():
        lib.write_report(
            out_dir / "q4_crossvenue.md",
            title,
            [
                f"No Bybit liquidations in the lake between {args.start} and {args.end}. The "
                f"twelve Tardis first-of-month days are the only cross-venue source until "
                f"the VPS recorder has run.",
                lib.decision("inconclusive", "There is no cross-venue data to answer this."),
            ],
        )
        return
    lonely = orphans(bybit, hl_qualified)
    coincidence = 1.0 - (lonely.height / bybit.height if bybit.height else 0.0)
    rows = [
        ["Bybit episodes clearing both floors", str(bybit.height)],
        ["of those, matched by an HL episode within 120s", str(bybit.height - lonely.height)],
        ["orphans, Bybit only", str(lonely.height)],
        ["coincidence rate", f"{coincidence:.0%}"],
        ["HL qualifying episodes over the same window", str(hl_qualified.height)],
    ]
    clusters = (
        lib.with_clusters(lonely, window_s=lib.CLUSTER_WINDOW_S)["cluster"].n_unique()
        if not lonely.is_empty()
        else 0
    )
    # The reduced bar is 15 clusters AND 60 recorder days, and the recorder has not run at
    # all: every cross-venue row in the lake is one of the twelve Tardis first-of-month
    # days. Counting clusters alone and calling it a pass was reading half a bar.
    recorder_days = (
        bybit.select(pl.col("start_ts").cast(pl.Datetime("ms")).dt.strftime("%Y-%m-%d"))
        .to_series()
        .n_unique()
    )
    if clusters < REDUCED_CLUSTER_BAR or recorder_days < REDUCED_DAYS_BAR:
        verdict = lib.decision(
            "inconclusive",
            f"{lonely.height} orphaned Bybit cascades in {clusters} clusters over "
            f"{recorder_days} days of cross-venue data, against the reduced bar of "
            f"{REDUCED_CLUSTER_BAR} clusters **and** {REDUCED_DAYS_BAR} recorder days. The "
            f"recorder has not run at all, so every cross-venue row here is one of the "
            f"twelve Tardis first-of-month days. Whether this blocks Phase 4 is Q3's trigger "
            f"branch to decide, and this report does not decide it. What the sample does "
            f"say: {coincidence:.0%} of Bybit cascades coincide with an HL episode, so the "
            f"two venues are {'largely the same event' if coincidence > 0.5 else 'often'} "
            f"{'' if coincidence > 0.5 else 'independent'}.",
        )
    else:
        reverted = lonely.with_columns(
            (
                1e4 * (pl.col("mid_at_end") - pl.col("extreme_px")).abs() / pl.col("extreme_px")
            ).alias("moved_bps")
        )
        verdict = lib.decision(
            "yes" if reverted["moved_bps"].mean() > 0 else "no",
            f"{lonely.height} orphaned cascades in {clusters} clusters clears the reduced "
            f"bar. Rerun once the recorder has 60 days; this is the twelve-day first cut.",
        )
    blocks = [
        f"Window: {args.start} to {args.end}.",
        "",
        "## Coincidence between the two venues",
        "",
        lib.table(["measure", "count"], rows),
        "",
        "Reversion on an orphan is measured on Hyperliquid's own tape with Q1's measure, "
        "net-stress, so a yes here would mean a Bybit-only cascade is tradeable on HL.",
        verdict,
    ]
    path = lib.write_report(out_dir / "q4_crossvenue.md", title, blocks)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
