"""Q5: do partial liquidation chunks leave a rhythm we can wait out?

Hyperliquid liquidates a large position in chunks rather than all at once, and the
documented cooldown between chunks is 30 seconds. If that is visible in the tape, a cascade
has a predictable pause and the exhaustion rule has something concrete to wait for.

The measurement is a histogram of the gaps between consecutive liquidation events inside
each qualifying episode. The $250,000 episode floor already clears the 100,000 USDC
threshold above which a position is liquidated in chunks, so every episode here is a
candidate.

DECISION yes if the 25 to 35 second bin holds more than twice the mass of its neighbours.

    uv run python scripts/research/q5_rhythm.py --start 2025-09-01 --end 2026-08-31
"""

from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).parent))

import _lib as lib

from liqfade.config.loader import load_settings

# Five second bins out to a minute. The chunk cooldown is documented at 30s, so the bin of
# interest is 25 to 35 and its neighbours are 15 to 25 and 35 to 45.
BIN_S = 5
MAX_GAP_S = 60
CHUNK_BIN = (25, 35)
LIFT_BAR = 2.0


def gaps(args: object, settings: object, episodes: pl.DataFrame) -> pl.DataFrame:
    """Seconds between consecutive liquidations inside each episode."""
    parts = []
    for first, last in lib.month_chunks(args.start, args.end):  # type: ignore[attr-defined]
        load_start, load_end = lib.padded(first, last)
        liquidations = lib.load_table(  # type: ignore[attr-defined]
            args.data_dir, "liquidations", load_start, load_end, venue="hyperliquid"
        )
        if liquidations.is_empty():
            continue
        window = episodes.select("coin", "side", "start_ts", "end_ts")
        joined = (
            liquidations.select("coin", "side", "ts_ms")
            .join(window, on=["coin", "side"])
            .filter((pl.col("ts_ms") >= pl.col("start_ts")) & (pl.col("ts_ms") <= pl.col("end_ts")))
            .sort("coin", "side", "start_ts", "ts_ms")
        )
        if joined.is_empty():
            continue
        parts.append(
            joined.with_columns(
                (
                    (pl.col("ts_ms") - pl.col("ts_ms").shift(1).over("coin", "side", "start_ts"))
                    / 1000.0
                ).alias("gap_s")
            ).drop_nulls("gap_s")
        )
    return pl.concat(parts) if parts else pl.DataFrame()


def histogram(gap_frame: pl.DataFrame) -> tuple[str, dict[int, int]]:
    counts: dict[int, int] = {}
    for low in range(0, MAX_GAP_S, BIN_S):
        counts[low] = gap_frame.filter(
            (pl.col("gap_s") >= low) & (pl.col("gap_s") < low + BIN_S)
        ).height
    total = max(sum(counts.values()), 1)
    widest = max(counts.values()) or 1
    rows = [
        [
            f"{low} to {low + BIN_S}s",
            str(count),
            f"{count / total:.1%}",
            "#" * round(30 * count / widest),
        ]
        for low, count in counts.items()
    ]
    return lib.table(["gap", "events", "share", ""], rows), counts


def chunk_lift(counts: dict[int, int]) -> tuple[float, int, float]:
    """Mass in the 25 to 35s bins against the mean of the bins either side of them."""
    inside = sum(count for low, count in counts.items() if CHUNK_BIN[0] <= low < CHUNK_BIN[1])
    outside = [
        count
        for low, count in counts.items()
        if low
        in (CHUNK_BIN[0] - 2 * BIN_S, CHUNK_BIN[0] - BIN_S, CHUNK_BIN[1], CHUNK_BIN[1] + BIN_S)
    ]
    neighbour_mean = sum(outside) / len(outside) if outside else 0.0
    lift = inside / neighbour_mean if neighbour_mean else 0.0
    return lift, inside, neighbour_mean


def main() -> None:
    args = lib.parse_args(__doc__ or "")
    settings = load_settings()
    out_dir = Path(args.out_dir)
    title = "Q5: the partial liquidation rhythm"
    episodes = lib.build_episodes(
        args.data_dir, args.start, args.end, settings, cache=out_dir / "episodes.parquet"
    )
    episodes = episodes.filter(pl.col("qualified")) if not episodes.is_empty() else episodes
    gap_frame = gaps(args, settings, episodes) if not episodes.is_empty() else pl.DataFrame()
    if gap_frame.is_empty():
        lib.write_report(
            out_dir / "q5_rhythm.md",
            title,
            [
                f"No multi-event episodes between {args.start} and {args.end}.",
                lib.decision("inconclusive", "A rhythm needs at least two events per episode."),
            ],
            costs=False,
        )
        return
    table, counts = histogram(gap_frame)
    lift, inside, neighbour_mean = chunk_lift(counts)
    qualifying, facts = lib.qualifies(episodes)
    detail = (
        f"The 25 to 35s bins hold {inside} gaps against a neighbouring mean of "
        f"{neighbour_mean:.0f}, a lift of {lift:.2f}x against a bar of {LIFT_BAR:.0f}x. "
        f"{gap_frame.height} gaps from {episodes.height} episodes. {facts}."
    )
    if not qualifying:
        verdict = lib.decision("inconclusive", f"The sample does not clear the bar. {detail}")
    elif lift >= LIFT_BAR:
        verdict = lib.decision(
            "yes",
            f"The documented chunk cooldown is visible in the tape, so a cascade has a pause "
            f"an entry rule can wait for. {detail}",
        )
    else:
        verdict = lib.decision(
            "no",
            f"No mass at the documented cooldown: liquidation timing inside an episode looks "
            f"continuous, so there is no rhythm to wait out and exhaustion has to be read "
            f"from flow rather than from the clock. {detail}",
        )
    blocks = [
        f"Window: {args.start} to {args.end}. Qualifying episodes: **{episodes.height}**, "
        f"gaps between consecutive liquidations inside them: **{gap_frame.height}**. Only "
        f"gaps under {MAX_GAP_S}s appear, since a longer one would have ended the episode.",
        "",
        "This question is about timing, not money, so it carries no cost columns. Nothing in "
        "it can produce an edge on its own; a yes gives the exhaustion rule something to "
        "wait for and a no says the rule has to read flow instead.",
        "",
        "## Gaps between consecutive liquidations",
        "",
        table,
        verdict,
    ]
    path = lib.write_report(out_dir / "q5_rhythm.md", title, blocks, costs=False)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
