"""Q6: which filters remove the losing tail without removing most of the sample?

Every filter costs sample. A filter that removes a third of the trades and a third of the
losses has done nothing but shrink the book. The bar is concentration: **keep a filter only
if it removes at least twice the share of loss-tail episodes as it removes of the sample
overall.**

The loss tail is Q8's stopped-out episodes, not the worst decile of terminal reversion.
Terminal reversion cannot tell a trade that lost from a trade that was stopped out on its
way to a win, and the second is what a filter is supposed to prevent. The stop and target
come from `config/default.yaml` (`intent.k_stop`, `intent.k_target`), and the report says
which values were used, because the answer moves with them.

The candidate filters are the ones in `docs/STRATEGY_AND_RISK.md` section 3: spread cap by
tier, BTC cascading, funding at an extreme against the trade, listing age, and the session
bucket.

    uv run python scripts/research/q6_filters.py --start 2025-09-01 --end 2026-08-31
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).parent))

import _lib as lib

from liqfade.config.loader import load_settings

CONCENTRATION_BAR = 2.0
# Session buckets, UTC. The three that a coin's liquidity actually differs across.
SESSIONS = {"asia 00-08": (0, 8), "europe 08-16": (8, 16), "americas 16-24": (16, 24)}


def q8() -> object:
    """Q8 owns the stop and target arithmetic; the loss tail has to be the same one."""
    spec = importlib.util.spec_from_file_location("q8_path", Path(__file__).parent / "q8_path.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def loss_tail(args: object, episodes: pl.DataFrame, settings: object) -> pl.DataFrame:
    """Mark each episode with whether the configured stop would have taken it out."""
    path = q8()
    intent = settings.intent  # type: ignore[attr-defined]
    tape = lib.episode_tape(args.data_dir, episodes, horizon_s=int(intent.time_stop_s))  # type: ignore[attr-defined]
    if tape.is_empty():
        return episodes.with_columns(pl.lit(False).alias("stopped_out"))
    resolved = path.outcomes(path.levels(episodes, intent.k_stop, intent.k_target), tape)
    return resolved.with_columns((pl.col("outcome") == "stop").alias("stopped_out"))


def candidates(episodes: pl.DataFrame, settings: object) -> dict[str, pl.Expr]:
    """Each filter as the expression that is true when the filter **blocks** the trade."""
    filters = settings.filters  # type: ignore[attr-defined]
    caps = filters.max_spread_bps
    tier_cap = (
        pl.when(pl.col("tier") == "core")
        .then(caps.core)
        .when(pl.col("tier") == "major")
        .then(caps.major)
        .otherwise(caps.minor)
    )
    # Funding is a per-hour fraction; the config threshold is bps per 8 hours, and a
    # positive rate is longs paying shorts, which is against a fade of a long liquidation.
    funding_8h_bps = pl.col("funding_at_start") * 8 * 1e4
    against = pl.when(pl.col("side") == "long").then(funding_8h_bps).otherwise(-funding_8h_bps)
    blocked = {
        "spread over the tier cap": pl.col("spread_bps") > tier_cap,
        "BTC itself cascading": pl.col("btc_cascading") & (pl.col("coin") != "BTC"),
        "funding at an extreme against the trade": against > filters.funding_extreme_bps_8h,
        "listed under 14 days": pl.col("since_listing_days") < 14,
    }
    for label, (low, high) in SESSIONS.items():
        blocked[f"skip session {label}"] = (pl.col("session_hour") >= low) & (
            pl.col("session_hour") < high
        )
    return blocked


def assess(episodes: pl.DataFrame, blocked: pl.Expr) -> tuple[list[str], bool]:
    """Share of sample removed against share of the loss tail removed."""
    total = episodes.height
    losses = episodes.filter(pl.col("stopped_out")).height
    removed = episodes.filter(blocked.fill_null(False))
    removed_losses = removed.filter(pl.col("stopped_out")).height
    sample_share = removed.height / total if total else 0.0
    loss_share = removed_losses / losses if losses else 0.0
    concentration = loss_share / sample_share if sample_share else 0.0
    keep = concentration >= CONCENTRATION_BAR and removed.height > 0
    kept = episodes.filter(~blocked.fill_null(False))
    remaining = kept["net_stress_bps"].mean() if not kept.is_empty() else None
    cells = [
        f"{removed.height} ({sample_share:.1%})",
        f"{removed_losses} ({loss_share:.1%})",
        f"{concentration:.2f}x",
        f"{remaining:+.1f}" if remaining is not None else "n/a",
        "keep" if keep else "drop",
    ]
    return cells, keep


def main() -> None:
    args = lib.parse_args(__doc__ or "")
    settings = load_settings()
    out_dir = Path(args.out_dir)
    title = "Q6: which filters remove the losing tail"
    episodes = lib.build_episodes(
        args.data_dir, args.start, args.end, settings, cache=out_dir / "episodes.parquet"
    )
    if episodes.is_empty():
        lib.write_report(
            out_dir / "q6_filters.md",
            title,
            [
                f"No liquidations between {args.start} and {args.end}.",
                lib.decision("inconclusive", "There is no data to answer this question."),
            ],
        )
        return
    episodes = lib.with_btc_regime(episodes.filter(pl.col("qualified")))
    episodes = loss_tail(args, episodes, settings)
    episodes = lib.with_cost_columns(episodes.with_columns(pl.col("mid_plus_15m").alias("exit_px")))
    qualifying, facts = lib.qualifies(episodes)
    rows, kept = [], []
    for label, blocked in candidates(episodes, settings).items():
        cells, keep = assess(episodes, blocked)
        rows.append([label, *cells])
        if keep:
            kept.append(label)
    intent = settings.intent
    stopped = episodes.filter(pl.col("stopped_out")).height
    if not qualifying:
        verdict = lib.decision(
            "inconclusive",
            f"The sample does not clear the preregistered bar: {facts}. A filter judged on "
            f"{stopped} stopped-out episodes is a filter fitted to a handful of trades.",
        )
    elif kept:
        verdict = lib.decision(
            "yes",
            f"Keep: {', '.join(kept)}. Each removes at least {CONCENTRATION_BAR:.0f}x the "
            f"share of the loss tail as of the sample. Everything else is dropped from the "
            f"Phase 4 config, whatever the spec says, because it only shrinks the book. "
            f"{facts}.",
        )
    else:
        verdict = lib.decision(
            "no",
            f"No candidate filter concentrates on the loss tail at {CONCENTRATION_BAR:.0f}x. "
            f"Phase 4 ships with the risk gates and no signal filters, and any filter added "
            f"later needs its own evidence. {facts}.",
        )
    blocks = [
        f"Window: {args.start} to {args.end}. Qualifying episodes: **{episodes.height}**, of "
        f"which **{stopped}** are the loss tail, meaning Q8's configured stop took them out "
        f"at `k_stop={intent.k_stop:g}`, `k_target={intent.k_target:g}`, "
        f"`time_stop_s={intent.time_stop_s:g}`. The answer moves with those, so they are "
        f"stated rather than assumed.",
        "",
        "A filter earns its place by concentration, not by removing losses: removing a "
        f"third of the sample and a third of the losses is {CONCENTRATION_BAR:.0f}x nothing.",
        "",
        "## Candidate filters",
        "",
        lib.table(
            [
                "filter (blocks when true)",
                "sample removed",
                "loss tail removed",
                "concentration",
                "net-stress of what remains",
                "verdict",
            ],
            rows,
        ),
        verdict,
    ]
    path = lib.write_report(out_dir / "q6_filters.md", title, blocks)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
