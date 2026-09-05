"""Q8: the path, not the destination. Maximum adverse excursion and the stop/target grid.

The second gate, and the one most likely to kill the project. Nothing in Q1 to Q7 measures
the path, so nothing in them measures the loss: terminal reversion cannot say whether a
stop was hit before a target, and a strategy with +20 bps of mean reversion and a 60%
stop-out rate loses money. Built from trades rather than 1m bars, or MAE is meaningless.

Per episode, entering at the first print after `extreme_ts + 1s`:

- the maximum adverse excursion before the time stop, in bps and in units of `rv_5m`;
- the joint outcome over `k_stop` in {1.0, 1.5, 2.5, infinity} by `k_target` in
  {0.3, 0.5, 0.8} with a 15 minute time stop, net-stress, as a hit/stop/timeout breakdown.

`target_px = extreme_px + k_target * (anchor_px - extreme_px)` and
`stop_px = extreme_px - k_stop * rv_5m * extreme_px`, beyond the extreme and vol scaled,
exactly as `docs/STRATEGY_AND_RISK.md` section 3 defines them. When both levels are touched
inside the same second the stop is assumed first, because the tape cannot say which came
first and the pessimistic reading is the only honest one.

DECISION yes if some cell has positive net-stress, a stop-out rate under 50%, and positive
neighbours in the grid; an isolated positive cell is the multiple-comparison problem wearing
a hat. The preregistration originally asked a cell to **beat** hold-to-horizon, which was a
drafting error, since a stop is insurance and cannot beat no-stop in expectation on a
reverting sample. Stop rule 2 governs. Amended 2026-09-05 after the numbers were seen; see
docs/DECISIONS.md.

    uv run python scripts/research/q8_path.py --start 2025-09-01 --end 2026-08-31
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).parent))

import _lib as lib

from liqfade.config.loader import load_settings

K_STOPS = (1.0, 1.5, 2.5, math.inf)
K_TARGETS = (0.3, 0.5, 0.8)
TIME_STOP_S = 900
MAX_STOP_RATE = 0.5


def levels(episodes: pl.DataFrame, k_stop: float, k_target: float) -> pl.DataFrame:
    """Target and stop prices for one cell of the grid, per episode.

    A long liquidation is faded by buying, so its target is above the extreme and its stop
    below; a short liquidation is the mirror. `direction` is +1 when the trade is long.
    """
    direction = pl.when(pl.col("side") == "long").then(1.0).otherwise(-1.0)
    span = pl.col("anchor_px") - pl.col("extreme_px")
    stop_distance = (
        pl.lit(float("inf"))
        if math.isinf(k_stop)
        else k_stop * pl.col("rv_5m") * pl.col("extreme_px")
    )
    return episodes.with_columns(
        direction.alias("direction"),
        (pl.col("extreme_px") + k_target * span).alias("target_px"),
        (pl.col("extreme_px") - direction * stop_distance).alias("stop_px"),
    )


def outcomes(episodes: pl.DataFrame, tape: pl.DataFrame) -> pl.DataFrame:
    """First touch of target or stop per episode, and the exit price that implies."""
    marked = tape.join(
        episodes.select("key", "direction", "target_px", "stop_px"), on="key", how="inner"
    )
    signed = marked.with_columns((pl.col("direction") * pl.col("px")).alias("signed_px"))
    touches = signed.group_by("key").agg(
        pl.col("ts_ms")
        .filter(pl.col("signed_px") >= pl.col("direction") * pl.col("target_px"))
        .min()
        .alias("target_ts"),
        pl.col("ts_ms")
        .filter(pl.col("signed_px") <= pl.col("direction") * pl.col("stop_px"))
        .min()
        .alias("stop_ts"),
    )
    joined = episodes.join(touches, on="key", how="left")
    # A tie inside one second resolves to the stop: the tape cannot order two prints in the
    # same millisecond bucket, and assuming the win would flatter every cell in the grid.
    stopped = pl.col("stop_ts").is_not_null() & (
        pl.col("target_ts").is_null() | (pl.col("stop_ts") <= pl.col("target_ts"))
    )
    hit = pl.col("target_ts").is_not_null() & ~stopped
    return joined.with_columns(
        pl.when(stopped)
        .then(pl.lit("stop"))
        .when(hit)
        .then(pl.lit("target"))
        .otherwise(pl.lit("timeout"))
        .alias("outcome"),
        pl.when(stopped)
        .then(pl.col("stop_px"))
        .when(hit)
        .then(pl.col("target_px"))
        .otherwise(pl.col("mid_plus_15m"))
        .alias("exit_px"),
    )


def excursions(episodes: pl.DataFrame, tape: pl.DataFrame) -> pl.DataFrame:
    """MAE from entry over the time stop window, in bps and in units of rv_5m.

    Floored at zero: a trade whose worst print was still better than its entry had no
    adverse excursion, and reporting that as a negative MAE would make the quantiles read
    as if a stop there were safe.
    """
    direction = pl.when(pl.col("side") == "long").then(1.0).otherwise(-1.0)
    worst = (
        tape.join(
            episodes.select("key", direction.alias("direction"), "entry_px_delayed", "rv_5m"),
            on="key",
        )
        .with_columns((pl.col("direction") * pl.col("px")).alias("signed_px"))
        .group_by("key")
        .agg(
            pl.col("signed_px").min().alias("_worst"),
            pl.col("direction").first(),
            pl.col("entry_px_delayed").first(),
            pl.col("rv_5m").first(),
        )
        .with_columns(
            (
                1e4
                * (pl.col("direction") * pl.col("entry_px_delayed") - pl.col("_worst"))
                / pl.col("entry_px_delayed")
            )
            .clip(lower_bound=0.0)
            .alias("mae_bps")
        )
        .with_columns(
            pl.when(pl.col("rv_5m") > 0)
            .then(pl.col("mae_bps") / (1e4 * pl.col("rv_5m")))
            .otherwise(None)
            .alias("mae_rv")
        )
    )
    return worst


def excursion_table(worst: pl.DataFrame) -> str:
    """MAE quantiles over every episode, however many chunks they were measured in."""
    rows = []
    for quantile in (0.25, 0.5, 0.75, 0.9, 0.95):
        bps = worst["mae_bps"].quantile(quantile) if not worst.is_empty() else None
        rv = worst["mae_rv"].quantile(quantile) if not worst.is_empty() else None
        rows.append(
            [
                f"p{quantile * 100:.0f}",
                f"{bps:.1f}" if bps is not None else "n/a",
                f"{rv:.2f}" if rv is not None else "n/a",
            ]
        )
    return lib.table(["quantile", "MAE bps", "MAE in rv_5m"], rows)


def resolve_cells(
    episodes: pl.DataFrame, tape: pl.DataFrame
) -> dict[tuple[float, float], pl.DataFrame]:
    """Every cell's per-episode outcome for one chunk of episodes.

    Returned rather than tabled so the caller can walk the year a chunk at a time: the tape
    behind 5,000 episodes is tens of millions of prints and does not fit, while the outcome
    of each episode in each of the twelve cells is one small row.
    """
    return {
        (k_stop, k_target): lib.with_cost_columns(
            outcomes(levels(episodes, k_stop, k_target), tape)
        )
        for k_stop in K_STOPS
        for k_target in K_TARGETS
    }


def grid_table(
    resolved: dict[tuple[float, float], pl.DataFrame],
) -> tuple[str, dict[tuple[float, float], dict[str, float]]]:
    """One row per cell: net-stress mean, stop-out rate and the hit/stop/timeout split."""
    rows = []
    cells: dict[tuple[float, float], dict[str, float]] = {}
    for k_stop in K_STOPS:
        for k_target in K_TARGETS:
            priced = resolved.get((k_stop, k_target), pl.DataFrame())
            if priced.is_empty():
                continue
            counts = {
                name: priced.filter(pl.col("outcome") == name).height
                for name in ("target", "stop", "timeout")
            }
            total = max(priced.height, 1)
            estimate = lib.cluster_bootstrap(
                priced.drop_nulls("net_stress_bps")["net_stress_bps"].to_numpy(),
                priced.drop_nulls("net_stress_bps")["cluster"].to_numpy(),
            )
            cells[(k_stop, k_target)] = {
                "net_stress": estimate.mean,
                "stop_rate": counts["stop"] / total,
                "lo": estimate.lo,
            }
            rows.append(
                [
                    "none" if math.isinf(k_stop) else f"{k_stop:g}",
                    f"{k_target:g}",
                    f"{priced['gross_bps'].mean():+.1f}",
                    f"{priced['net_9bps'].mean():+.1f}",
                    estimate.render(),
                    f"{counts['stop'] / total:.0%}",
                    f"{counts['target']}/{counts['stop']}/{counts['timeout']}",
                ]
            )
    header = [
        "k_stop",
        "k_target",
        "gross bps",
        "net-9bps",
        "net-stress bps",
        "stop rate",
        "hit/stop/timeout",
    ]
    return lib.table(header, rows), cells


def neighbours(cell: tuple[float, float]) -> list[tuple[float, float]]:
    """The cells adjacent in the grid, which must also be positive for a yes."""
    stop_index, target_index = K_STOPS.index(cell[0]), K_TARGETS.index(cell[1])
    out = []
    for step in (-1, 1):
        if 0 <= stop_index + step < len(K_STOPS):
            out.append((K_STOPS[stop_index + step], cell[1]))
        if 0 <= target_index + step < len(K_TARGETS):
            out.append((cell[0], K_TARGETS[target_index + step]))
    return out


def verdict(
    cells: dict[tuple[float, float], dict[str, float]], hold: float, qualifying: bool, facts: str
) -> str:
    """The preregistered bar, as amended on 2026-09-05.

    The original wording asked a cell to **beat** hold-to-horizon. That was a drafting
    error: a stop is insurance and cannot beat no-stop in expectation on a sample that
    reverts, so no grid could ever have satisfied it and the question could only ever have
    answered no. Stop rule 2 governs instead, which is the condition the project actually
    cares about: is there a cell that is positive, does not stop out of half its trades, and
    sits in a positive neighbourhood rather than alone. Decided by the owner after seeing
    the numbers; recorded in docs/DECISIONS.md, 2026-09-05.
    """
    if not qualifying:
        return lib.decision(
            "inconclusive",
            f"The sample does not clear the preregistered bar: {facts}. A stop-out rate "
            f"measured on a sample this size is a rate of nothing.",
        )
    harvestable = [
        (cell, stats)
        for cell, stats in cells.items()
        if stats["net_stress"] > 0
        and stats["stop_rate"] < MAX_STOP_RATE
        and all(
            cells.get(other, {"net_stress": -1.0})["net_stress"] > 0 for other in neighbours(cell)
        )
    ]
    if not harvestable:
        best = max(cells.values(), key=lambda stats: stats["net_stress"], default=None)
        detail = f"best cell {best['net_stress']:+.1f} bps" if best else "no cells"
        return lib.decision(
            "no",
            f"No cell is positive with a stop-out rate under {MAX_STOP_RATE:.0%} and positive "
            f"neighbours ({detail}). The reversion may be real and still not be reachable "
            f"through a stop. {facts}.",
            stop_rule=2,
        )
    cell, stats = max(harvestable, key=lambda item: item[1]["net_stress"])
    stop_label = "none" if math.isinf(cell[0]) else f"{cell[0]:g}"
    kept = f"{stats['net_stress']:+.1f} of {hold:+.1f}"
    return lib.decision(
        "yes",
        f"Yes with a stop. `k_stop={stop_label}`, `k_target={cell[1]:g}` keeps {kept} bps at "
        f"{stats['stop_rate']:.0%} stop-outs, with every neighbouring cell positive. A stop "
        f"is insurance: it is not expected to beat holding on a reverting sample, it is "
        f"expected to cost little while bounding the loss on the ones that do not revert. "
        f"These are the values the config writeback takes. {facts}.",
    )


def main() -> None:
    args = lib.parse_args(__doc__ or "")
    settings = load_settings()
    out_dir = Path(args.out_dir)
    episodes = lib.build_episodes(
        args.data_dir, args.start, args.end, settings, cache=out_dir / "episodes.parquet"
    )
    title = "Q8: path, maximum adverse excursion and the stop/target grid"
    if episodes.is_empty():
        lib.write_report(
            out_dir / "q8_path.md",
            title,
            [
                f"No liquidations in the lake between {args.start} and {args.end}.",
                lib.decision("inconclusive", "There is no data to answer this question."),
            ],
        )
        return
    episodes = lib.with_btc_regime(episodes.filter(pl.col("qualified")))
    # A week at a time. The tape behind a year of episodes is tens of millions of prints and
    # will not fit on the owner's machine; the outcome of each episode is one row, so the
    # tape is read, reduced and released a chunk at a time and only the rows survive. A week
    # rather than a month because the cost here is the tape, not the episode count.
    chunks: list[dict[tuple[float, float], pl.DataFrame]] = []
    maes: list[pl.DataFrame] = []
    prints = 0
    for first, last in lib.week_chunks(args.start, args.end):
        window = (pl.col("start_ts") >= lib.ms(first)) & (
            pl.col("start_ts") < lib.ms(last) + 86_400_000
        )
        part = episodes.filter(window)
        if part.is_empty():
            continue
        tape = lib.episode_tape(args.data_dir, part, horizon_s=TIME_STOP_S)
        if tape.is_empty():
            continue
        prints += tape.height
        chunks.append(resolve_cells(part, tape))
        maes.append(excursions(part, tape))
        del tape
    resolved = {
        cell: pl.concat([chunk[cell] for chunk in chunks if not chunk[cell].is_empty()])
        for cell in {cell for chunk in chunks for cell in chunk}
    }
    worst = pl.concat(maes) if maes else pl.DataFrame()
    qualifying, facts = lib.qualifies(episodes)
    hold = (
        lib.with_cost_columns(episodes.with_columns(pl.col("mid_plus_15m").alias("exit_px")))[
            "net_stress_bps"
        ].mean()
        or 0.0
    )
    excursion = excursion_table(worst)
    grid, cells = grid_table(resolved)
    blocks = [
        f"Window: {args.start} to {args.end}. Qualifying episodes: **{episodes.height}**, "
        f"clusters: **{episodes['cluster'].n_unique()}**. Prints read: **{prints}**. "
        f"Entry is the first print after `extreme_ts + 1s`; the time stop is "
        f"{TIME_STOP_S // 60} minutes.",
        "",
        "## Maximum adverse excursion from entry",
        "",
        "How far the trade goes against you before the time stop, whatever the exit rule. A "
        "stop inside the p50 is a stop that fires on half the winners.",
        "",
        excursion,
        "",
        "## The stop and target grid",
        "",
        f"Hold to the {TIME_STOP_S // 60} minute horizon with no stop and no target is "
        f"**{hold:+.1f} bps** net-stress, and is reported as the reference rather than as a "
        f"bar: a stop cannot beat no-stop in expectation on a reverting sample. A cell has "
        f"to be positive, stop out less than {MAX_STOP_RATE:.0%} of the time, and have "
        f"positive neighbours.",
        "",
        grid,
        verdict(cells, hold, qualifying, facts),
    ]
    path = lib.write_report(out_dir / "q8_path.md", title, blocks)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
