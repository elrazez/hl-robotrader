"""Q2: does waiting for exhaustion beat entering a fixed number of seconds after the start?

The spec's exhaustion rule (`docs/STRATEGY_AND_RISK.md` section 3) evaluated tick by tick
on the research grid, against entries at a flat 30, 60 and 120 seconds after the episode
starts. Same exit for all four: the mid 15 minutes after entry. Same cost model.

The rule's conditions, all of which must hold for `exhaustion.consecutive_ticks` in a row:

- `liq_15s <= liq_decay_frac * peak_liq_15s`
- the CVD slope has flipped against the cascade direction. The book-imbalance alternative
  is unavailable for the historical year, which has no bbo at all, so this arm of the
  "or" is simply absent and the report says so.
- `abs(mark - mid) / mid` back under its trailing 80th percentile
- no new extreme mid for `no_new_extreme_s`
- cascade age between `min_age_s` and `max_age_s`

DECISION yes if exhaustion beats every fixed delay on mean net-stress PnL.

    uv run python scripts/research/q2_exhaustion.py --start 2025-09-01 --end 2026-08-31
"""

from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).parent))

import _lib as lib

from liqfade.config.loader import load_settings

FIXED_DELAYS_S = (30, 60, 120)
EXIT_HORIZON_S = 900


def exhaustion_ticks(grid: pl.DataFrame, episodes: pl.DataFrame, settings: object) -> pl.DataFrame:
    """The first tick inside each episode where every exhaustion condition holds.

    One join of the grid onto the episode windows, then the conditions as columns, then the
    first row per episode where all of them have held for the required run. No Python loop
    over ticks: a year of the 1s grid is not something to iterate.
    """
    cfg = settings.exhaustion  # type: ignore[attr-defined]
    windows = episodes.select(
        "key",
        "coin",
        "side",
        "start_ts",
        "extreme_px",
        "peak_liq_15s",
        (pl.col("start_ts") + int(cfg.min_age_s * 1000)).alias("_from"),
        (pl.col("start_ts") + int(cfg.max_age_s * 1000)).alias("_to"),
    )
    ticks = windows.join(grid, on="coin").filter(
        (pl.col("ts_ms") >= pl.col("_from")) & (pl.col("ts_ms") <= pl.col("_to"))
    )
    if ticks.is_empty():
        return pl.DataFrame(schema={"key": pl.String, "entry_ts": pl.Int64})
    direction = pl.when(pl.col("side") == "long").then(-1.0).otherwise(1.0)
    decayed = pl.col("liq_long_usd_15s") + pl.col(
        "liq_short_usd_15s"
    ) <= cfg.liq_decay_frac * pl.col("peak_liq_15s")
    # A long cascade is forced selling, so the CVD slope is negative while it runs and
    # flipping means it has turned positive. The book-imbalance alternative needs bbo,
    # which the historical year does not have.
    flipped = (direction * pl.col(f"cvd_slope_{int(settings.features.cvd_slope_window_s)}s")) > 0  # type: ignore[attr-defined]
    calm = ~pl.col("mark_mid_over_p95").fill_null(True)
    ticks = ticks.sort("key", "ts_ms").with_columns(
        pl.when(pl.col("side") == "long")
        .then(pl.col("mid").cum_min().over("key"))
        .otherwise(pl.col("mid").cum_max().over("key"))
        .alias("_running_extreme")
    )
    fresh_extreme = pl.col("_running_extreme") != pl.col("_running_extreme").shift(1).over("key")
    ticks = ticks.with_columns(
        pl.when(fresh_extreme)
        .then(pl.col("ts_ms"))
        .otherwise(None)
        .forward_fill()
        .over("key")
        .alias("_last_extreme_ts")
    )
    settled = (pl.col("ts_ms") - pl.col("_last_extreme_ts")) >= int(cfg.no_new_extreme_s * 1000)
    holding = (decayed & flipped & calm & settled).fill_null(False)
    run = (
        holding.cast(pl.Int32)
        .rolling_sum(window_size=cfg.consecutive_ticks, min_samples=cfg.consecutive_ticks)
        .over("key")
    )
    confirmed = ticks.with_columns(run.alias("_run")).filter(
        pl.col("_run") == cfg.consecutive_ticks
    )
    return confirmed.group_by("key").agg(pl.col("ts_ms").min().alias("entry_ts"))


def priced(episodes: pl.DataFrame, entries: pl.DataFrame, grid: pl.DataFrame) -> pl.DataFrame:
    """Net-stress PnL of entering at `entry_ts` and exiting 15 minutes later."""
    mids = grid.select("coin", "ts_ms", "mid").sort("ts_ms")
    joined = episodes.join(entries, on="key", how="inner", suffix="_rule")
    if joined.is_empty():
        return joined
    at_entry = joined.rename(
        {"entry_ts_rule": "rule_entry_ts"} if "entry_ts_rule" in joined.columns else {}
    )
    column = "rule_entry_ts" if "rule_entry_ts" in at_entry.columns else "entry_ts"
    at_entry = at_entry.with_columns((pl.col(column) + EXIT_HORIZON_S * 1000).alias("_exit_ts"))
    for when, alias in ((column, "rule_entry_px"), ("_exit_ts", "rule_exit_px")):
        at_entry = (
            at_entry.sort(when)
            .join_asof(
                mids.rename({"ts_ms": f"_{alias}_ts", "mid": alias}),
                left_on=when,
                right_on=f"_{alias}_ts",
                by="coin",
                strategy="forward",
            )
            .drop(f"_{alias}_ts")
        )
    return lib.with_cost_columns(
        at_entry.with_columns(
            pl.col("rule_entry_px").alias("entry_px"),
            pl.col("rule_entry_px").alias("entry_px_delayed"),
            pl.col("rule_exit_px").alias("exit_px"),
        )
    )


def fixed_entries(episodes: pl.DataFrame, delay_s: int) -> pl.DataFrame:
    return episodes.select("key", (pl.col("start_ts") + delay_s * 1000).alias("entry_ts"))


def main() -> None:
    args = lib.parse_args(__doc__ or "")
    settings = load_settings()
    out_dir = Path(args.out_dir)
    title = "Q2: exhaustion timing against a fixed delay"
    episodes = lib.build_episodes(
        args.data_dir, args.start, args.end, settings, cache=out_dir / "episodes.parquet"
    )
    if episodes.is_empty():
        lib.write_report(
            out_dir / "q2_exhaustion.md",
            title,
            [
                f"No liquidations between {args.start} and {args.end}.",
                lib.decision("inconclusive", "There is no data to answer this question."),
            ],
        )
        return
    episodes = episodes.filter(pl.col("qualified"))
    rows: list[list[str]] = []
    means: dict[str, float] = {}
    fired = 0
    for first, last in lib.month_chunks(args.start, args.end):
        grid = lib.feature_grid(args.data_dir, first, last, settings)
        if grid.is_empty():
            continue
        window = (pl.col("start_ts") >= lib.ms(first)) & (
            pl.col("start_ts") < lib.ms(last) + 86_400_000
        )
        month = episodes.filter(window)
        if month.is_empty():
            continue
        entries = exhaustion_ticks(grid, month, settings)
        fired += entries.height
        for label, table in [("exhaustion", entries)] + [
            (f"fixed {delay}s", fixed_entries(month, delay)) for delay in FIXED_DELAYS_S
        ]:
            result = priced(month, table, grid)
            if result.is_empty():
                continue
            values = result.drop_nulls("net_stress_bps")
            if values.is_empty():
                continue
            means.setdefault(label, 0.0)
            means[label] += float(values["net_stress_bps"].sum())
            rows.append([label, str(values.height), f"{values['net_stress_bps'].mean():+.1f}"])
    summary = dict(means)
    qualifying, facts = lib.qualifies(episodes)
    best = max(summary, key=lambda label: summary[label]) if summary else None
    if not qualifying or best is None:
        verdict = lib.decision(
            "inconclusive",
            f"The sample does not clear the preregistered bar: {facts}. "
            f"The exhaustion rule fired on {fired} episodes.",
        )
    elif best == "exhaustion":
        verdict = lib.decision(
            "yes",
            f"Exhaustion beats every fixed delay on summed net-stress PnL. It fired on "
            f"{fired} of {episodes.height} qualifying episodes; the ones it never fired on "
            f"are not traded at all, which is the cost of waiting. {facts}.",
        )
    else:
        verdict = lib.decision(
            "no",
            f"A flat delay beats the rule: {best} carries the most net-stress PnL. The rule "
            f"fired on {fired} of {episodes.height} episodes. Phase 4 should enter on the "
            f"delay rather than on the rule, or the rule needs rewriting before it earns "
            f"its complexity. {facts}.",
        )
    blocks = [
        f"Window: {args.start} to {args.end}. Qualifying episodes: **{episodes.height}**. "
        f"The exhaustion rule fired on **{fired}** of them. Every row exits 15 minutes after "
        f"its own entry, so the comparison is of entry timing and nothing else.",
        "",
        "The book-imbalance arm of the flow-flip condition is unavailable: the historical "
        "year has no bbo, so only the CVD slope arm is evaluated. That makes the rule "
        "strictly harder to satisfy here than it will be live.",
        "",
        "## Net-stress PnL by entry rule, per month",
        "",
        lib.table(["entry rule", "episodes", "net-stress bps"], rows),
        verdict,
    ]
    path = lib.write_report(out_dir / "q2_exhaustion.md", title, blocks)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
