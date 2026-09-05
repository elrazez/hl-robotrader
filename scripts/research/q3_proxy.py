"""Q3: how well does the HL-native forced flow proxy recover exact S3 liquidations?

The live bot cannot see liquidations. This question decides whether it can infer them, and
with them **which trigger Phase 4 is built on**, which is written into this report and
carried into `SUMMARY.md`:

- proxy precision >= 0.6 AND recall >= 0.4 on core+major at some threshold: the v0 trigger
  is **HL-native**, and Q4 stays inconclusive and non-blocking until Phase 7.
- otherwise: the v0 trigger is **cross-venue** (Bybit is the only complete feed) and Q4
  becomes a Phase 4 entry requirement at a reduced bar of 15 clusters and 60 recorder days.

The headline is the full three-gate proxy over the whole window, with the OI and mark gates
read from the archive-backed `asset_ctx`. The trades-only variant runs on the same window
and is reported beside it; the difference is the gate lift and carries no DECISION of its
own. A trigger counts as a true positive when a qualifying episode on the same coin and
side starts within 120s of it.

Writes `reports/proxy_calibration.md`, at that exact path, because other docs reference it.

    uv run python scripts/research/q3_proxy.py --start 2025-09-01 --end 2026-08-31
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).parent))

import _lib as lib

from liqfade.config.loader import load_settings
from liqfade.features.context import with_ctx
from liqfade.features.proxy import hl_forced_proxy

THRESHOLDS = (2.0, 3.0, 4.0, 6.0)
MATCH_WINDOW_MS = 120_000
# Coin batches the year is swept in. Memory scales with the coins held at 1s resolution.
COIN_BATCHES = 3
PRECISION_BAR = 0.6
RECALL_BAR = 0.4
# Below this, and with Q4 unanswerable, the preregistration parks the project (stop rule 5).
PARK_PRECISION = 0.5
PROXY_WINDOW_S = 60
# A run of consecutive ticks over the threshold is one trigger, not one per tick, or the
# precision denominator becomes "how many seconds did the cascade last".
TRIGGER_GAP_MS = 120_000


class Score:
    """Precision, recall and F1 for one threshold on one tier."""

    def __init__(self, matched: int, triggers: int, episodes: int) -> None:
        self.matched, self.triggers, self.episodes = matched, triggers, episodes
        self.precision = matched / triggers if triggers else 0.0
        self.recall = matched / episodes if episodes else 0.0
        total = self.precision + self.recall
        self.f1 = 2 * self.precision * self.recall / total if total else 0.0

    def cells(self) -> list[str]:
        return [
            f"{self.precision:.2f}",
            f"{self.recall:.2f}",
            f"{self.f1:.2f}",
            f"{self.matched}/{self.triggers}",
            str(self.episodes),
        ]


def triggers_of(scored: pl.DataFrame, threshold: float, *, presorted: bool = False) -> pl.DataFrame:
    """Collapse each run of ticks over the threshold into one trigger at its first tick.

    `presorted` says the frame is already ordered by (coin, ts_ms); filtering keeps that
    order, so the sort is skipped. The sort was 8 of the day's seconds when run per
    threshold on the year sweep.
    """
    rows = []
    for side in ("long", "short"):
        column = f"hl_forced_proxy_{side}_{PROXY_WINDOW_S}s"
        over = scored.filter(pl.col(column) >= threshold).select("coin", "ts_ms")
        if not presorted:
            over = over.sort("coin", "ts_ms")
        if over.is_empty():
            continue
        fresh = pl.col("ts_ms").diff().over("coin").fill_null(TRIGGER_GAP_MS + 1) > TRIGGER_GAP_MS
        rows.append(over.filter(fresh).with_columns(pl.lit(side).alias("side")))
    return pl.concat(rows) if rows else pl.DataFrame(schema=scored.select("coin", "ts_ms").schema)


def score(triggers: pl.DataFrame, episodes: pl.DataFrame) -> Score:
    """Match triggers to episodes within the window, each side of the match counted once."""
    if episodes.is_empty():
        return Score(0, triggers.height, 0)
    if triggers.is_empty():
        return Score(0, 0, episodes.height)
    pairs = (
        triggers.join(episodes.select("key", "coin", "side", "start_ts"), on=["coin", "side"])
        .filter((pl.col("start_ts") - pl.col("ts_ms")).abs() <= MATCH_WINDOW_MS)
        .select("ts_ms", "coin", "side", "key")
    )
    matched_triggers = pairs.select("coin", "side", "ts_ms").unique().height
    matched_episodes = pairs.select("key").unique().height
    scored = Score(matched_triggers, triggers.height, episodes.height)
    # Recall counts episodes found, not triggers matched: one trigger can cover two
    # episodes and one episode can attract two triggers.
    scored.recall = matched_episodes / episodes.height
    total = scored.precision + scored.recall
    scored.f1 = 2 * scored.precision * scored.recall / total if total else 0.0
    return scored


def calibration_table(
    triggers_all: pl.DataFrame, episodes: pl.DataFrame, *, gated: bool
) -> tuple[str, dict[float, Score]]:
    """One row per threshold per tier group, plus the core+major row the bar is read from."""
    rows = []
    headline: dict[float, Score] = {}
    groups = {
        "core+major": episodes.filter(pl.col("tier").is_in(["core", "major"])),
        "core": episodes.filter(pl.col("tier") == "core"),
        "major": episodes.filter(pl.col("tier") == "major"),
        "minor": episodes.filter(pl.col("tier") == "minor"),
    }
    for threshold in THRESHOLDS:
        triggers = triggers_all.filter(
            (pl.col("threshold") == threshold) & (pl.col("gated") == gated)
        ).select("coin", "ts_ms", "side")
        for label, part in groups.items():
            coins = part.select("coin").unique()
            tier_triggers = (
                triggers.join(coins, on="coin") if not coins.is_empty() else triggers.head(0)
            )
            result = score(tier_triggers, part)
            if label == "core+major":
                headline[threshold] = result
            rows.append([f"{threshold:g}", label, *result.cells()])
    header = ["threshold", "tier", "precision", "recall", "F1", "matched/triggers", "episodes"]
    return lib.table(header, rows), headline


def trigger_branch(headline: dict[float, Score]) -> tuple[str, str]:
    """The branch this report decides for Phase 4, and the sentence explaining it."""
    passing = [
        (threshold, result)
        for threshold, result in headline.items()
        if result.precision >= PRECISION_BAR and result.recall >= RECALL_BAR
    ]
    if passing:
        threshold, result = max(passing, key=lambda item: item[1].f1)
        return "HL-native", (
            f"At threshold {threshold:g} the three-gate proxy reaches precision "
            f"{result.precision:.2f} and recall {result.recall:.2f} on core+major, clearing "
            f"{PRECISION_BAR} and {RECALL_BAR}. **The v0 trigger is HL-native.** Q4 stays "
            f"inconclusive and non-blocking and is revisited in Phase 7. The config "
            f"writeback takes {threshold:g} as `cascade.proxy_min`, from this F1 curve."
        )
    best = max(headline.values(), key=lambda result: result.f1, default=None)
    detail = (
        f"best precision {best.precision:.2f} at recall {best.recall:.2f}"
        if best
        else "no thresholds scored"
    )
    # The parking rule is only named when its own condition holds. It asks for precision
    # under 0.5 as well as a Q4 that cannot be answered, and a report that names a rule the
    # data did not trigger is worse than one that names none.
    parked = best is not None and best.precision < PARK_PRECISION
    tail = (
        f" Precision is also under {PARK_PRECISION}, so if Q4 cannot be answered for months "
        f"the project parks until the recorder has the data."
        if parked
        else ""
    )
    return "cross-venue", (
        f"No threshold clears precision {PRECISION_BAR} and recall {RECALL_BAR} on core+major "
        f"({detail}). **The v0 trigger is cross-venue**, on Bybit, the only complete feed, "
        f"and Q4 becomes a Phase 4 entry requirement at the reduced bar of 15 clusters and 60 "
        f"recorder days.{tail}"
    )


def build_scores(args: object, settings: object, episodes: pl.DataFrame) -> pl.DataFrame:
    """The proxy over the whole window, gated and ungated, one UTC day at a time.

    This is the one question that cannot restrict itself to where the answer already is:
    precision counts the triggers that found nothing, so the sweep has to cover the tape
    rather than the episodes. It still never holds a day of trades. `lib.daily_grid`
    aggregates each day to a 1s grid inside the Parquet scan, the proxy runs on that, and
    only the triggers survive the day, which are a few hundred rows. The result is one row
    per trigger: coin, ts_ms, side, threshold, gated.

    Restricted to the coins that carry an episode in the window. Those are the tradeable
    universe by definition, and a coin the strategy would never trade contributes only
    false positives to a precision it is not measured on.

    Each day is computed with the previous day in front of it, so the hourly volume median
    and the 24h mark-divergence percentile are full before the day being scored begins;
    only the scored day's rows are kept.
    """
    universe = sorted(episodes["coin"].unique().to_list()) if not episodes.is_empty() else []
    if not universe:
        return pl.DataFrame()
    # Split the universe into batches and sweep each separately. A trigger belongs to one
    # coin and is matched against episodes on that same coin, so batching by coin is exact
    # and not an approximation: the two halves' triggers simply concatenate. It costs a
    # second pass over the drive and halves the resident grid, which is what a 42 coin
    # universe over two days at 1s resolution needs.
    batches = [universe[start::COIN_BATCHES] for start in range(COIN_BATCHES)]
    return (
        pl.concat(
            [part for batch in batches if not (part := _sweep(args, settings, batch)).is_empty()]
        )
        if any(batches)
        else pl.DataFrame()
    )


def _sweep(args: object, settings: object, coins: list[str]) -> pl.DataFrame:
    """One coin batch's year of proxy triggers, a UTC day at a time."""
    features = settings.features  # type: ignore[attr-defined]
    proxy_cfg = features.proxy
    if not coins:
        return pl.DataFrame()
    common = {
        "window_s": PROXY_WINDOW_S,
        "median_lookback_s": int(proxy_cfg.vol_median_lookback_s),
        "step_ms": lib.GRID_STEP_MS,
        "oi_window_s": int(proxy_cfg.oi_delta_window_s),
    }
    columns = [f"hl_forced_proxy_{side}_{PROXY_WINDOW_S}s" for side in ("long", "short")]
    parts: list[pl.DataFrame] = []
    previous: pl.DataFrame | None = None
    # One small file per finished day, so a three-hour sweep on a USB drive that has
    # dropped twice today resumes instead of restarting. A day with no triggers writes an
    # empty file, which is still "done".
    checkpoints = Path(args.out_dir) / "q3_days"  # type: ignore[attr-defined]
    checkpoints.mkdir(parents=True, exist_ok=True)
    schema = {
        "coin": pl.Utf8,
        "ts_ms": pl.Int64,
        "side": pl.Utf8,
        "threshold": pl.Float64,
        "gated": pl.Boolean,
    }
    for day in lib.dates(args.start, args.end):  # type: ignore[attr-defined]
        done = checkpoints / f"{day}.parquet"
        if done.exists():
            saved = pl.read_parquet(done)
            if not saved.is_empty():
                parts.append(saved)
            previous = None  # the next day rebuilds its own lookback
            continue
        today = lib.daily_grid(args.data_dir, day, coins)  # type: ignore[attr-defined]
        if today.is_empty():
            previous = None
            continue
        if previous is None and day != args.start:  # type: ignore[attr-defined]
            # Resuming after a checkpoint: rebuild yesterday so the lookbacks are full.
            yesterday = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
            previous = lib.daily_grid(args.data_dir, yesterday, coins)  # type: ignore[attr-defined]
            if previous.is_empty():
                previous = None
        grid = pl.concat([previous, today]) if previous is not None else today
        ctx = lib._ctx_for(
            args.data_dir,  # type: ignore[attr-defined]
            (date.fromisoformat(day) - timedelta(days=1)).isoformat(),
            day,
            coins,
        )
        grid = with_ctx(
            grid.sort("coin", "ts_ms"),
            ctx,
            windows_s=[int(proxy_cfg.oi_delta_window_s)],
            step_ms=lib.GRID_STEP_MS,
            quantile=proxy_cfg.mark_mid_pct_threshold / 100.0,
        )
        scored_day = pl.col("ts_ms") >= lib.ms(day)  # type: ignore[attr-defined]
        # Only triggers survive the day. Keeping every tick at or over the lowest
        # threshold was 2.4M rows a day on 67 coins, which is what ran the year out of
        # memory at day ~230. Runs are collapsed on the two-day frame, so a run that began
        # before midnight already triggered yesterday and does not trigger again today.
        day_parts: list[pl.DataFrame] = []
        for gated_flag in (True, False):
            # One pass over the grid keeps the ticks at or over the lowest threshold; the
            # per-threshold run collapse then works on that slice, not on the whole grid.
            frame = hl_forced_proxy(grid, gated=gated_flag, **common)
            candidates = (
                frame.filter(
                    pl.any_horizontal([pl.col(column) >= min(THRESHOLDS) for column in columns])
                )
                .select("coin", "ts_ms", *columns)
                .sort("coin", "ts_ms")
            )
            del frame
            for threshold in THRESHOLDS:
                found = triggers_of(candidates, threshold, presorted=True).filter(scored_day)
                if not found.is_empty():
                    day_parts.append(
                        found.with_columns(
                            pl.lit(threshold).alias("threshold"), pl.lit(gated_flag).alias("gated")
                        )
                    )
        day_frame = pl.concat(day_parts) if day_parts else pl.DataFrame(schema=schema)
        day_frame.write_parquet(done)
        if not day_frame.is_empty():
            parts.append(day_frame)
        previous = today
        del today, grid
    return pl.concat(parts) if parts else pl.DataFrame()


def main() -> None:
    args = lib.parse_args(__doc__ or "")
    settings = load_settings()
    out_dir = Path(args.out_dir)
    title = "Q3: forced flow proxy calibration against exact S3 liquidations"
    episodes = lib.build_episodes(
        args.data_dir, args.start, args.end, settings, cache=out_dir / "episodes.parquet"
    )
    episodes = episodes.filter(pl.col("qualified")) if not episodes.is_empty() else episodes
    scored = build_scores(args, settings, episodes)
    calibration = out_dir.parent / "proxy_calibration.md"
    if scored.is_empty() or episodes.is_empty():
        blocks = [
            f"No trades or no qualifying episodes between {args.start} and {args.end}.",
            lib.decision("inconclusive", "There is no data to calibrate the proxy against."),
        ]
        lib.write_report(out_dir / "q3_proxy.md", title, blocks)
        lib.write_report(calibration, title, blocks)
        return

    table, headline = calibration_table(scored, episodes, gated=True)
    lift_table, _ = calibration_table(scored, episodes, gated=False)
    branch, sentence = trigger_branch(headline)
    verdict = "yes" if branch == "HL-native" else "no"
    blocks = [
        f"Window: {args.start} to {args.end}. Qualifying episodes: **{episodes.height}**. "
        f"A trigger is a run of ticks at or over the threshold, collapsed to its first tick, "
        f"and matches an episode on the same coin and side within "
        f"{MATCH_WINDOW_MS // 1000}s. The proxy window is {PROXY_WINDOW_S}s.",
        "",
        "## Three-gate proxy, the headline",
        "",
        "Volume ratio, gated by falling open interest and mark-versus-mid over its trailing "
        "p95, both read from the archive-backed `asset_ctx`. A null gate blocks.",
        "",
        table,
        "",
        "## Trades-only variant, the gate lift",
        "",
        "The same volume ratio with both gates off, on the same window. The difference is "
        "what the gates buy. This table carries no DECISION of its own.",
        "",
        lift_table,
        "",
        "## The trigger branch for Phase 4",
        "",
        sentence,
        lib.decision(
            verdict,
            sentence + "\n\nRerun both tables at 60 recorder days, where the gates read live "
            "block-level values instead of the archive's one-a-minute sampling.",
        ),
    ]
    lib.write_report(out_dir / "q3_proxy.md", title, blocks)
    path = lib.write_report(calibration, title, blocks)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
