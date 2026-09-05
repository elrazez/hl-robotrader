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
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).parent))

import _lib as lib

from liqfade.config.loader import load_settings
from liqfade.features.context import with_ctx
from liqfade.features.grid import build_grid
from liqfade.features.proxy import hl_forced_proxy

THRESHOLDS = (2.0, 3.0, 4.0, 6.0)
MATCH_WINDOW_MS = 120_000
PRECISION_BAR = 0.6
RECALL_BAR = 0.4
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


def triggers_of(scored: pl.DataFrame, threshold: float) -> pl.DataFrame:
    """Collapse each run of ticks over the threshold into one trigger at its first tick."""
    rows = []
    for side in ("long", "short"):
        column = f"hl_forced_proxy_{side}_{PROXY_WINDOW_S}s"
        over = (
            scored.filter(pl.col(column) >= threshold).select("coin", "ts_ms").sort("coin", "ts_ms")
        )
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
    scored: pl.DataFrame, episodes: pl.DataFrame
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
        triggers = triggers_of(scored, threshold)
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
    return "cross-venue", (
        f"No threshold clears precision {PRECISION_BAR} and recall {RECALL_BAR} on core+major "
        f"({detail}). **The v0 trigger is cross-venue**, on Bybit, the only complete feed, "
        f"and Q4 becomes a Phase 4 entry requirement at the reduced bar of 15 clusters and 60 "
        f"recorder days. If the proxy is also under precision 0.5 and Q4 cannot be answered "
        f"for months, stop rule 5 applies: park the project until the recorder has the data."
    )


def build_scores(args: object, settings: object, episodes: pl.DataFrame) -> pl.DataFrame:
    """The proxy over the whole window, gated and ungated, a month at a time."""
    features = settings.features  # type: ignore[attr-defined]
    proxy_cfg = features.proxy
    parts = []
    for first, last in lib.month_chunks(args.start, args.end):  # type: ignore[attr-defined]
        load_start, load_end = lib.padded(first, last)
        trades = lib.load_table(args.data_dir, "trades", load_start, load_end, venue="hyperliquid")  # type: ignore[attr-defined]
        if trades.is_empty():
            continue
        ctx = lib.load_table(args.data_dir, "asset_ctx", load_start, load_end)  # type: ignore[attr-defined]
        grid = build_grid(
            trades,
            None,
            start_ms=lib.ms(load_start),
            end_ms=lib.ms(load_end) + 86_400_000,
            step_ms=lib.GRID_STEP_MS,
        )
        grid = with_ctx(
            grid,
            ctx,
            windows_s=[int(proxy_cfg.oi_delta_window_s)],
            step_ms=lib.GRID_STEP_MS,
            quantile=proxy_cfg.mark_mid_pct_threshold / 100.0,
        )
        common = {
            "window_s": PROXY_WINDOW_S,
            "median_lookback_s": int(proxy_cfg.vol_median_lookback_s),
            "step_ms": lib.GRID_STEP_MS,
            "oi_window_s": int(proxy_cfg.oi_delta_window_s),
        }
        gated = hl_forced_proxy(grid, gated=True, **common)
        ungated = hl_forced_proxy(grid, gated=False, **common)
        window = (pl.col("ts_ms") >= lib.ms(first)) & (pl.col("ts_ms") < lib.ms(last) + 86_400_000)
        parts.append(
            gated.filter(window)
            .select(
                "coin",
                "ts_ms",
                *[f"hl_forced_proxy_{side}_{PROXY_WINDOW_S}s" for side in ("long", "short")],
            )
            .join(
                ungated.filter(window).select(
                    "coin",
                    "ts_ms",
                    *[
                        pl.col(f"hl_forced_proxy_{side}_{PROXY_WINDOW_S}s").alias(f"ungated_{side}")
                        for side in ("long", "short")
                    ],
                ),
                on=["coin", "ts_ms"],
            )
        )
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

    table, headline = calibration_table(scored, episodes)
    ungated_view = scored.rename(
        {
            f"hl_forced_proxy_{side}_{PROXY_WINDOW_S}s": f"_gated_{side}"
            for side in ("long", "short")
        }
    ).rename(
        {
            f"ungated_{side}": f"hl_forced_proxy_{side}_{PROXY_WINDOW_S}s"
            for side in ("long", "short")
        }
    )
    lift_table, _ = calibration_table(ungated_view, episodes)
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
