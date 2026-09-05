"""Q1: after an exact Hyperliquid liquidation cascade, does mid revert, and by how much?

The first of the two gates. Reads only the Parquet lake, writes
`reports/research/q1_reversion.md` and its figures.

Measures, per qualifying episode: the mid at the end of the episode and at +1, +5, +15,
+30 and +60 minutes from the extreme, the reversion fraction, and the PnL of fading the
cascade in gross, net-9bps and net-stress terms. The preregistered headline is the mean
net-stress PnL at **+15 minutes** with a cluster-bootstrap 95% interval; +5 minutes is the
only permitted secondary look and carries a 99% interval because it is a second look.

DECISION bar, from `reports/research/PREREGISTRATION.md`:

- yes when the mean net-stress PnL at +15 min is >= 24 bps with the 95% CI lower bound
  above zero, on a sample of >= 200 episodes, >= 40 clusters and >= 8 calendar months.
- "marginal, Q2 must supply the rest" between 15 and 24 bps on the same conditions. 24 bps
  is the strategy spec's own `min_edge_multiple` floor and nothing under it will trade.
- no when the mean is <= 0. inconclusive when the sample does not qualify.

Three robustness splits are mandatory and are judged on point estimates: by quarter, by
coin (excluding BTC and ETH, then excluding HYPE) and by regime (BTC cascading or not).
Anything that passes pooled and fails a split is a conditional yes gated on that split.

    uv run python scripts/research/q1_reversion.py --start 2025-09-01 --end 2026-08-31
"""

from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).parent))

import _lib as lib

from liqfade.config.loader import load_settings

HEADLINE_MIN = 15  # minutes: the preregistered horizon, the only one that can produce a yes
TRADEABLE_BPS = 24.0  # the spec's min_edge_multiple floor: 4x the 6 bps round trip
MARGINAL_BPS = 15.0
# Reported as a robustness slice only. Phase 4's pre-trade guard, from the core tier's p99.
SPREAD_GUARD_BPS = 30.0


def reversion_columns(episodes: pl.DataFrame) -> pl.DataFrame:
    """Add the reversion fraction and the three cost columns for each horizon."""
    span = pl.col("anchor_px") - pl.col("extreme_px")
    out = episodes
    for minutes in lib.HORIZONS_MIN:
        mid = pl.col(f"mid_plus_{minutes}m")
        out = out.with_columns(
            pl.when(span.abs() > 0)
            .then((mid - pl.col("extreme_px")) / span)
            .otherwise(None)
            .alias(f"reverted_{minutes}m"),
        )
        priced = lib.with_cost_columns(
            out.select(
                "side",
                "entry_px",
                "entry_px_delayed",
                pl.col(f"mid_plus_{minutes}m").alias("exit_px"),
                "spread_bps",
            )
        )
        out = out.with_columns(
            priced["gross_bps"].alias(f"gross_bps_{minutes}m"),
            priced["net_9bps"].alias(f"net_9bps_{minutes}m"),
            priced["net_stress_bps"].alias(f"net_stress_bps_{minutes}m"),
        )
    return out


def horizon_table(episodes: pl.DataFrame) -> str:
    """One row per horizon, three cost columns each, the headline row marked."""
    rows = []
    for minutes in lib.HORIZONS_MIN:
        confidence = 0.99 if minutes == 5 else 0.95
        estimates = [
            estimate_of(episodes, f"{basis}_{minutes}m", confidence)
            for basis in ("gross_bps", "net_9bps", "net_stress_bps")
        ]
        median = episodes[f"net_stress_bps_{minutes}m"].median()
        label = f"+{minutes}m" + (" (headline)" if minutes == HEADLINE_MIN else "")
        rows.append(
            [label, *(e.render() for e in estimates), f"{median:+.1f}" if median else "n/a"]
        )
    return lib.table(
        ["horizon", "gross bps", "net-9bps", "net-stress bps", "net-stress median"], rows
    )


def estimate_of(episodes: pl.DataFrame, column: str, confidence: float) -> lib.Estimate:
    frame = episodes.drop_nulls(column)
    return lib.cluster_bootstrap(
        frame[column].to_numpy(), frame["cluster"].to_numpy(), confidence=confidence
    )


def headline(episodes: pl.DataFrame) -> str:
    return f"net_stress_bps_{HEADLINE_MIN}m"


def split_table(episodes: pl.DataFrame) -> str:
    """The three preregistered robustness splits, judged on point estimates."""
    column = headline(episodes)
    rows = []
    quarters = episodes.with_columns(
        pl.col("start_ts").cast(pl.Datetime("ms")).dt.quarter().alias("_q"),
        pl.col("start_ts").cast(pl.Datetime("ms")).dt.year().alias("_y"),
    )
    for (year, quarter), part in sorted(quarters.group_by("_y", "_q", maintain_order=True)):
        rows.append([f"{year} Q{quarter}", *slice_cells(part, column)])
    for label, kept in (
        ("excluding BTC and ETH", episodes.filter(~pl.col("coin").is_in(["BTC", "ETH"]))),
        ("excluding HYPE", episodes.filter(pl.col("coin") != "HYPE")),
        ("isolated coin (BTC quiet)", episodes.filter(~pl.col("btc_cascading"))),
        ("market-wide sweep", episodes.filter(pl.col("btc_cascading"))),
        ("top 5 clusters removed", lib.drop_top_clusters(episodes, column, count=5)),
        # Reported, not applied. The effective spread inside the filtered universe has a
        # p99 of 137 bps that lives entirely in major and minor; core's own p99 is 22.9.
        # Phase 4 takes 30 bps as a pre-trade guard, chosen from that core p99. Q1 does not
        # cap: it shows what the cap would have done. See docs/DECISIONS.md, 2026-09-05.
        (
            f"used spread <= {SPREAD_GUARD_BPS:g} bps",
            episodes.filter(pl.col("spread_bps") <= SPREAD_GUARD_BPS),
        ),
    ):
        rows.append([label, *slice_cells(kept, column)])
    return lib.table(["slice", "episodes", "clusters", "net-stress bps"], rows)


def slice_cells(part: pl.DataFrame, column: str) -> list[str]:
    if part.is_empty():
        return ["0", "0", "n/a"]
    mean = part[column].mean()
    return [str(part.height), str(part["cluster"].n_unique()), f"{mean:+.1f}"]


def capacity_paragraph(episodes: pl.DataFrame) -> str:
    column = headline(episodes)
    capacity = "notional_at_25pct_participation"
    if capacity not in episodes.columns or episodes.is_empty():
        return "_No capacity column: the episode table is empty._"
    dollars = (episodes[column] / 1e4 * episodes[capacity]).mean()
    per_year = dollars * episodes.height  # the sample's own episode rate over its own span
    return (
        f"Capacity-weighted edge: **${dollars:,.0f} per episode** at 25% participation, "
        f"**${per_year:,.0f}** over the sample's {episodes.height} episodes. The stop rule "
        f"is $2,000 a year, roughly five times infrastructure plus margin."
    )


def verdict(episodes: pl.DataFrame, qualifying: bool, facts: str) -> str:
    if not qualifying:
        return lib.decision(
            "inconclusive",
            f"The sample does not clear the preregistered bar: {facts}. No headline is read "
            f"from a sample this size, whatever it says.",
        )
    estimate = estimate_of(episodes, headline(episodes), 0.95)
    trimmed = lib.drop_top_clusters(episodes, headline(episodes), count=5)
    trimmed_mean = trimmed[headline(episodes)].mean() if not trimmed.is_empty() else float("nan")
    tail = (
        f"Mean net-stress at +{HEADLINE_MIN}m is {estimate.render()}; with the top five "
        f"clusters removed it is {trimmed_mean:+.1f} bps. Sample: {facts}."
    )
    if estimate.mean <= 0:
        return lib.decision("no", tail, stop_rule=1)
    if estimate.lo <= 0:
        return lib.decision(
            "inconclusive", f"The interval includes zero, so the edge is not established. {tail}"
        )
    if estimate.mean >= TRADEABLE_BPS:
        return lib.decision("yes", tail)
    if estimate.mean >= MARGINAL_BPS:
        return lib.decision(
            "inconclusive",
            f"Marginal, Q2 must supply the rest: the mean clears {MARGINAL_BPS:.0f} bps but "
            f"not the {TRADEABLE_BPS:.0f} bps floor below which nothing will ever trade. {tail}",
        )
    return lib.decision(
        "no",
        f"Positive but under the {MARGINAL_BPS:.0f} bps floor, so there is nothing for Q2 to "
        f"rescue. {tail}",
    )


def notional_histogram(episodes: pl.DataFrame) -> str:
    """Episodes by notional bucket, so capacity is visible before any reversion number."""
    if episodes.is_empty():
        return "_No episodes._"
    buckets = [(0, 5e5), (5e5, 1e6), (1e6, 5e6), (5e6, 2e7), (2e7, float("inf"))]
    rows = []
    for low, high in buckets:
        part = episodes.filter(
            (pl.col("total_notional") >= low) & (pl.col("total_notional") < high)
        )
        label = f"${low / 1e6:.1f}M to ${high / 1e6:.1f}M" if high < float("inf") else "$20M+"
        mean = part[headline(episodes)].mean() if not part.is_empty() else None
        rows.append([label, str(part.height), f"{mean:+.1f}" if mean is not None else "n/a"])
    return lib.table(["episode notional", "episodes", "net-stress bps at headline"], rows)


def tier_table(episodes: pl.DataFrame) -> str:
    rows = []
    for tier in ("core", "major", "minor"):
        part = episodes.filter(pl.col("tier") == tier)
        rows.append([tier, *slice_cells(part, headline(episodes))])
    return lib.table(["tier", "episodes", "clusters", "net-stress bps"], rows)


def main() -> None:
    args = lib.parse_args(__doc__ or "")
    settings = load_settings()
    out_dir = Path(args.out_dir)
    episodes = lib.build_episodes(
        args.data_dir, args.start, args.end, settings, cache=out_dir / "episodes.parquet"
    )
    unfiltered = episodes.height
    if episodes.is_empty():
        lib.write_report(
            out_dir / "q1_reversion.md",
            "Q1: reversion after an exact liquidation cascade",
            [
                f"No liquidations in the lake between {args.start} and {args.end}.",
                lib.decision("inconclusive", "There is no data to answer this question."),
            ],
        )
        return
    episodes = episodes.filter(pl.col("qualified"))
    episodes = reversion_columns(episodes)
    episodes = lib.with_btc_regime(episodes)
    qualifying, facts = lib.qualifies(episodes)
    blocks = [
        f"Window: {args.start} to {args.end}. Episodes detected: **{unfiltered}**, of which "
        f"**{episodes.height}** qualify (the trailing p95 or z rule, and both absolute "
        f"floors). Clusters: **{episodes['cluster'].n_unique()}**.",
        "",
        "## Episodes by notional, before any reversion number is read",
        "",
        notional_histogram(episodes),
        "",
        "## Reversion by horizon",
        "",
        horizon_table(episodes),
        "",
        "## By coin tier, at the headline horizon",
        "",
        tier_table(episodes),
        "",
        "## Robustness, judged on point estimates",
        "",
        split_table(episodes),
        "",
        "## Capacity",
        "",
        capacity_paragraph(episodes),
        verdict(episodes, qualifying, facts),
    ]
    path = lib.write_report(
        out_dir / "q1_reversion.md", "Q1: reversion after an exact liquidation cascade", blocks
    )
    episodes.write_parquet(out_dir / "q1_episodes.parquet")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
