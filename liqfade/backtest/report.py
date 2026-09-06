"""Write a run down: `report.md`, `summary.json` and `trades.parquet`.

`run_id` is `{utc timestamp}_{config hash[:8]}`, where the hash is the sha256 of the
resolved config as canonical JSON. Two runs of the same config on the same data therefore
differ only in their timestamp, and a run whose numbers changed without its config changing
is a bug rather than a tuning result.

**The first page names the trigger**, always. A report that does not say whether it ran on
`exact` (the historical ceiling, invisible in real time), `crossvenue` or `proxy` is a
report whose numbers cannot be read: the first of those measures an edge nobody can reach
live. `docs/DECISIONS.md`, 2026-09-06.

`summary.json` is a sidecar so the panel's report list does not have to parse markdown to
show a metrics table. Markdown is for the owner; the sidecar is for the machine; both come
from the same `Metrics`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import polars as pl

from liqfade.backtest.engine import BacktestResult
from liqfade.backtest.metrics import (
    NO_BOOK_GAP_FLAG_PTS,
    Metrics,
    compute_metrics,
    split_by_episode_count,
)
from liqfade.backtest.portfolio import Trade
from liqfade.backtest.validate import (
    BootstrapCI,
    bootstrap_mean_bps,
    deflated_sharpe,
    read_n_trials,
    utc_stamp,
    walk_forward,
)
from liqfade.config.models import Settings

__all__ = ["config_hash", "run_id_for", "write_report"]

TRIGGER_NOTE = {
    "exact": (
        "the lake's exact Hyperliquid liquidations, from the S3 pull. **These are not "
        "visible in real time.** Every number below is a ceiling on what the strategy "
        "could have made with a trigger nobody can see live, not a forecast."
    ),
    "crossvenue": (
        "aggregated Bybit, OKX and Binance liquidations, weighted by `venues.*.liq_weight`. "
        "Live visible. On the historical year these exist on the twelve Tardis days only."
    ),
    "proxy": (
        "the Hyperliquid-native forced flow proxy. Live visible. Q3 measured precision 0.01 "
        "at the preregistered thresholds, so treat a positive result here with suspicion "
        "until Q3's re-cut says otherwise."
    ),
}


def config_hash(settings: Settings) -> str:
    """sha256 of the resolved config, so a run is identified by what produced it."""
    payload = orjson.dumps(settings.model_dump(mode="json"), option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(payload).hexdigest()


def run_id_for(settings: Settings, *, now: datetime | None = None) -> str:
    """`{utc timestamp}_{config hash[:8]}`."""
    moment = now or datetime.now(UTC)
    return f"{moment.strftime('%Y%m%dT%H%M%SZ')}_{config_hash(settings)[:8]}"


def write_report(
    result: BacktestResult,
    settings: Settings,
    *,
    reports_dir: Path,
    run_id: str,
    n_trials: int | None = None,
    lagged_ms: int | None = None,
    latency_fragile: bool | None = None,
) -> Path:
    """Write the run's three files and return the directory holding them.

    Args:
        result: what the replay produced.
        settings: the resolved config the run used.
        reports_dir: `reports/`, the parent of the run directory.
        run_id: from `run_id_for`.
        n_trials: the honest tally for the deflated Sharpe. Read from the counter when None.
        lagged_ms: the latency this run was degraded to, when it is a lagged rerun.
        latency_fragile: set by the caller when the lagged rerun lost the edge. Stamps the
            first page, per the roadmap's Phase 4 definition of done.
    """
    directory = Path(reports_dir) / run_id
    directory.mkdir(parents=True, exist_ok=True)
    trades = list(result.trades)

    train, holdout = split_by_episode_count(trades, settings.backtest.holdout_frac)
    overall = compute_metrics(
        trades,
        equity_curve=result.equity_curve,
        start_ms=result.start_ms,
        end_ms=result.end_ms,
        no_book_share=result.portfolio.no_book_share,
    )
    train_metrics = compute_metrics(train, start_ms=result.start_ms, end_ms=result.end_ms)
    holdout_metrics = compute_metrics(holdout, start_ms=result.start_ms, end_ms=result.end_ms)
    ci = bootstrap_mean_bps(trades, draws=settings.backtest.bootstrap_samples, seed=0)
    trials = read_n_trials(reports_dir) if n_trials is None else n_trials
    deflated = deflated_sharpe(
        overall.sharpe_per_trade, n_trials=max(trials, 1), n_obs=max(overall.trades, 2)
    )
    windows = walk_forward(
        trades,
        start_ms=result.start_ms,
        end_ms=result.end_ms,
        refit_days=settings.backtest.walk_forward_refit_days,
    )

    _write_trades(directory / "trades.parquet", trades)
    summary = _summary(
        result,
        settings,
        run_id=run_id,
        overall=overall,
        train=train_metrics,
        holdout=holdout_metrics,
        ci=ci,
        deflated=deflated,
        n_trials=trials,
        lagged_ms=lagged_ms,
        latency_fragile=latency_fragile,
    )
    # OPT_NON_STR_KEYS because the by-hour split is keyed on an integer UTC hour. Sorting
    # keys as well, so two runs of the same config produce the same bytes here too.
    (directory / "summary.json").write_bytes(
        orjson.dumps(
            summary,
            option=orjson.OPT_INDENT_2 | orjson.OPT_NON_STR_KEYS | orjson.OPT_SORT_KEYS,
        )
    )
    (directory / "report.md").write_text(
        _markdown(
            result,
            settings,
            run_id=run_id,
            overall=overall,
            train=train_metrics,
            holdout=holdout_metrics,
            ci=ci,
            deflated=deflated,
            n_trials=trials,
            windows=windows,
            lagged_ms=lagged_ms,
            latency_fragile=latency_fragile,
        )
    )
    return directory


# --- trades.parquet -----------------------------------------------------------


def _write_trades(path: Path, trades: list[Trade]) -> None:
    """Every simulated round trip, with a fixed column order.

    Written with an explicit schema even when empty, so a run with no trades produces a
    readable file rather than a zero-column one the panel cannot open. The column order is
    fixed here so two runs of the same config produce byte-identical files, which is what
    the determinism test compares.
    """
    schema = {
        "coin": pl.String,
        "side": pl.String,
        "size": pl.Float64,
        "entry_px": pl.Float64,
        "exit_px": pl.Float64,
        "entry_ms": pl.Int64,
        "exit_ms": pl.Int64,
        "hold_s": pl.Float64,
        "exit_reason": pl.String,
        "gross_pnl": pl.Float64,
        "fees_usd": pl.Float64,
        "funding_usd": pl.Float64,
        "net_pnl": pl.Float64,
        "net_bps": pl.Float64,
        "entry_slippage_bps": pl.Float64,
        "exit_slippage_bps": pl.Float64,
        "no_book_entry": pl.Boolean,
        "no_book_exit": pl.Boolean,
        "urgency": pl.String,
        "cascade_notional_usd": pl.Float64,
    }
    rows = [
        {
            **{key: getattr(trade, key) for key in schema if key not in ("hold_s", "net_bps")},
            "hold_s": trade.hold_s,
            "net_bps": trade.net_bps,
        }
        for trade in trades
    ]
    frame = pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)
    frame.write_parquet(path)


# --- summary.json -------------------------------------------------------------


def _summary(
    result: BacktestResult,
    settings: Settings,
    *,
    run_id: str,
    overall: Metrics,
    train: Metrics,
    holdout: Metrics,
    ci: BootstrapCI,
    deflated: float,
    n_trials: int,
    lagged_ms: int | None,
    latency_fragile: bool | None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "config_hash": config_hash(settings),
        "env": settings.env,
        "trigger": settings.cascade.trigger,
        "exhaustion_mode": settings.exhaustion.mode,
        "latency_ms": settings.backtest.latency_ms,
        "lagged_ms": lagged_ms,
        "latency_fragile": latency_fragile,
        "start_ms": result.start_ms,
        "end_ms": result.end_ms,
        "coins": list(result.coins),
        "starting_equity": result.portfolio.starting_equity,
        "n_trials": n_trials,
        "deflated_sharpe": deflated,
        "bootstrap": {"mean_bps": ci.mean, "lo": ci.lo, "hi": ci.hi, "draws": ci.draws},
        "metrics": asdict(overall),
        "train": asdict(train),
        "holdout": asdict(holdout),
        "funnel": {
            "intents": result.intents,
            "entries_placed": result.entries_placed,
            "trades": len(result.trades),
            "abandoned_entries": result.portfolio.abandoned_entries,
            "sizing_blocks": result.sizing_blocks,
            "risk_blocks": result.risk_blocks,
            "filter_blocks": dict(result.strategy.stats.filters),
            "rule_blocks": {
                name: s.killed for name, s in result.strategy.stats.rules.items() if s.killed
            },
            "confluence_blocks": result.strategy.stats.confluence_blocks,
        },
        "min_confluence": settings.strategy.min_confluence,
        "rules": {
            name: {
                "evaluated": s.evaluated,
                "passed": s.passed,
                "killed": s.killed,
                "required": s.required,
            }
            for name, s in result.strategy.stats.rules.items()
        },
        "funding": {
            "applied_hours": result.portfolio.funding.applied_hours,
            "missing_hours": result.portfolio.funding.missing_hours,
        },
        "coverage": {
            "windows": result.windows,
            "replayed_ms": result.replayed_ms,
            "skipped_ms": max(result.end_ms - result.start_ms - result.replayed_ms, 0),
            "replayed_frac": _replayed_frac(result),
        },
    }


def _replayed_frac(result: BacktestResult) -> float:
    span = result.end_ms - result.start_ms
    return result.replayed_ms / span if span > 0 else 0.0


def _coverage(result: BacktestResult) -> str:
    """How much of the clock the run actually read, and in how many pieces.

    The engine replays raw tape only inside the windows a cascade candidate opens; outside
    them the portfolio is flat by construction and there is nothing to simulate. This line
    says how much that saved, and it is also the sanity check: a run reporting close to
    100% is reading the whole tape again and something has gone wrong with the windows.
    """
    skipped_s = max(result.end_ms - result.start_ms - result.replayed_ms, 0) // 1000
    return (
        f"{100 * _replayed_frac(result):.1f}% of the tape in {result.windows:,} "
        f"replay windows, skipping {skipped_s:,}s"
    )


# --- report.md ----------------------------------------------------------------


def _markdown(
    result: BacktestResult,
    settings: Settings,
    *,
    run_id: str,
    overall: Metrics,
    train: Metrics,
    holdout: Metrics,
    ci: BootstrapCI,
    deflated: float,
    n_trials: int,
    windows: list[Any],
    lagged_ms: int | None,
    latency_fragile: bool | None,
) -> str:
    trigger = settings.cascade.trigger
    lines: list[str] = [
        f"# Backtest {run_id}",
        "",
    ]
    if latency_fragile:
        lines += [
            "> **LATENCY FRAGILE.** The rerun at degraded latency did not keep a positive "
            "expectancy. Per `docs/ROADMAP.md` Phase 4, this strategy is marked latency "
            "fragile and must not proceed to Phase 5 on these numbers.",
            "",
        ]
    lines += [
        f"**Trigger: `{trigger}`** - {TRIGGER_NOTE[trigger]}",
        "",
        f"- config hash: `{config_hash(settings)}`",
        f"- window: {utc_stamp(result.start_ms)} to {utc_stamp(result.end_ms)} UTC",
        f"- coins: {', '.join(result.coins) if result.coins else 'none'}",
        f"- exhaustion mode: `{settings.exhaustion.mode}`"
        + (
            f" ({settings.exhaustion.fixed_delay_s:.0f}s)"
            if settings.exhaustion.mode == "fixed_delay"
            else ""
        ),
        f"- latency: {settings.backtest.latency_ms} ms"
        + (f" (lagged rerun, requested {lagged_ms} ms)" if lagged_ms else ""),
        f"- starting equity: {result.portfolio.starting_equity:,.2f} USD",
        f"- replayed: {_coverage(result)}",
        "",
        "Every number below is net of fees and funding. There is no gross headline; "
        "`gross_pnl` appears once, beside the costs it paid.",
        "",
        "## Headline",
        "",
        _table(
            ["metric", "value"],
            [
                ["trades", f"{overall.trades}"],
                ["net PnL", f"{overall.net_pnl:,.2f} USD"],
                ["mean net", f"{overall.avg_net_bps:.1f} bps"],
                [
                    "bootstrap 95% CI",
                    f"[{ci.lo:.1f}, {ci.hi:.1f}] bps over {ci.draws} draws"
                    + ("" if ci.draws else " (too few trades)"),
                ],
                ["hit rate", f"{100 * overall.hit_rate:.1f}%"],
                ["profit factor", _profit_factor(overall.profit_factor)],
                ["Sharpe per trade", f"{overall.sharpe_per_trade:.2f}"],
                ["Sortino per trade", f"{overall.sortino_per_trade:.2f}"],
                ["Sharpe daily, annualised", f"{overall.sharpe_daily:.2f}"],
                [
                    "max drawdown",
                    f"{overall.max_drawdown:,.2f} USD ({100 * overall.max_drawdown_frac:.1f}%)",
                ],
                ["deflated Sharpe", f"{deflated:.3f} over {n_trials} configurations tried"],
            ],
        ),
        "",
        "## Costs",
        "",
        _table(
            ["item", "USD"],
            [
                ["gross PnL", f"{overall.gross_pnl:,.2f}"],
                ["fees", f"-{overall.fees_usd:,.2f}"],
                ["funding", f"-{overall.funding_usd:,.2f}"],
                ["**net PnL**", f"**{overall.net_pnl:,.2f}**"],
            ],
        ),
        "",
        f"Funding was applied on {result.portfolio.funding.applied_hours} hour boundaries "
        f"and unavailable on {result.portfolio.funding.missing_hours}. An unavailable hour "
        "costs zero and is counted rather than guessed.",
        "",
        "## Concentration",
        "",
        f"The best 5 trades carry **{100 * overall.top5_pnl_share:.1f}%** of net PnL. "
        "Phase 3's stop rule 3 is that a headline which dies when the best five leave the "
        "sample is those five, not an edge.",
        "",
        "## Holdout",
        "",
        "Split by trade count, not by days, so both sides carry comparable evidence "
        "regardless of where the calendar fell.",
        "",
        _table(
            ["split", "trades", "net PnL", "mean bps", "hit rate", "no-book fills"],
            [
                _split_row("train", train),
                _split_row("holdout", holdout),
            ],
        ),
        "",
        _no_book_note(overall),
        "",
        "## Fill quality",
        "",
        _table(
            ["metric", "value"],
            [
                ["fills priced without a book", f"{100 * overall.no_book_share:.1f}%"],
                ["mean entry slippage", f"{overall.avg_entry_slippage_bps:.1f} bps"],
                ["mean stop slippage", f"{overall.avg_stop_slippage_bps:.1f} bps"],
                ["stop-out rate", f"{100 * overall.stop_rate:.1f}%"],
                ["mean hold", f"{overall.avg_hold_s:,.0f} s"],
                ["max hold", f"{overall.max_hold_s:,.0f} s"],
                ["exposure", f"{100 * overall.exposure_frac:.1f}% of the window"],
                ["turnover", f"{overall.turnover_usd:,.0f} USD"],
            ],
        ),
        "",
        "## Where the intents went",
        "",
        _table(
            ["stage", "count"],
            [
                ["intents emitted", f"{result.intents}"],
                ["entries placed", f"{result.entries_placed}"],
                ["entries abandoned unfilled", f"{result.portfolio.abandoned_entries}"],
                ["completed trades", f"{len(result.trades)}"],
            ],
        ),
        "",
        _blocks_table("Blocked by sizing", result.sizing_blocks),
        "",
        _blocks_table("Blocked by risk", result.risk_blocks),
        "",
        _blocks_table("Blocked by filters", dict(result.strategy.stats.filters)),
        "",
        _blocks_table("Blocked by rules", _rule_blocks(result)),
        "",
        *_rules_section(result, settings),
        "",
        "## Splits",
        "",
        _pnl_table("By coin", overall.by_coin),
        "",
        _pnl_table("By UTC hour of entry", overall.by_hour),
        "",
        _pnl_table("By cascade size", overall.by_cascade_size),
        "",
        _pnl_table("By urgency", overall.by_urgency),
        "",
        _count_table("Exits, by reason", overall.by_exit_reason),
        "",
        "## Stability",
        "",
        f"Walk-forward over {settings.backtest.walk_forward_refit_days} day windows. v0 "
        "fits nothing, so this reports whether the result is steady rather than validating "
        "a fit: metrics that swing window to window mean the pooled number is an average "
        "over regimes rather than a description of any of them.",
        "",
        _table(
            ["window", "from", "trades", "net PnL", "mean bps"],
            [
                [
                    f"{window.index}",
                    utc_stamp(window.start_ms),
                    f"{window.metrics.trades}",
                    f"{window.metrics.net_pnl:,.2f}",
                    f"{window.metrics.avg_net_bps:.1f}",
                ]
                for window in windows
            ],
        ),
        "",
    ]
    return "\n".join(lines) + "\n"


def _split_row(name: str, metrics: Metrics) -> list[str]:
    return [
        name,
        f"{metrics.trades}",
        f"{metrics.net_pnl:,.2f}",
        f"{metrics.avg_net_bps:.1f}",
        f"{100 * metrics.hit_rate:.1f}%",
        f"{100 * metrics.no_book_share:.1f}%",
    ]


def _no_book_note(overall: Metrics) -> str:
    """Flag the case `docs/DATA_SOURCES.md` warns about: a split with a different book.

    The archive's `l2` cadence drops tenfold from 2026-06-21, and the held-out last 25% of
    the historical range begins inside that transition. A holdout whose fills were priced
    differently from the training window would look worse for a reason that has nothing to
    do with the strategy.
    """
    return (
        f"The share of fills priced without a book snapshot is reported per split above. "
        f"A gap over {NO_BOOK_GAP_FLAG_PTS:.0f} points between splits means the two were "
        f"priced by different models and are not directly comparable: see "
        f"`docs/DATA_SOURCES.md` on the 2026-06-21 l2 cadence step."
    )


def _profit_factor(value: float) -> str:
    return "no losing trades" if value == float("inf") else f"{value:.2f}"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_none_"
    head = "| " + " | ".join(headers) + " |"
    rule = "|" + "|".join("---" for _ in headers) + "|"
    body = ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join([head, rule, *body])


def _pnl_table(title: str, values: Mapping[Any, float]) -> str:
    rows = [[str(key), f"{value:,.2f}"] for key, value in values.items()]
    return f"**{title}**\n\n" + _table(["bucket", "net PnL"], rows)


def _count_table(title: str, values: Mapping[Any, int]) -> str:
    rows = [[str(key), f"{value}"] for key, value in values.items()]
    return f"**{title}**\n\n" + _table(["bucket", "trades"], rows)


def _rule_blocks(result: BacktestResult) -> dict[str, int]:
    """Which rule killed how many intents, plus the count no single rule killed.

    A required rule that fails names itself. Too few confluence rules agreeing is a
    property of the set, so it gets its own row rather than being charged to whichever
    optional rule happened to sort first.
    """
    stats = result.strategy.stats
    blocks = {name: s.killed for name, s in stats.rules.items() if s.killed}
    if stats.confluence_blocks:
        blocks["confluence count"] = stats.confluence_blocks
    return blocks


def _rules_section(result: BacktestResult, settings: Settings) -> list[str]:
    """The two rule tables: what could veto, and what merely voted.

    They are separated because they answer different questions. A required rule's number
    that matters is what it cost; a confluence rule's is how often it agreed, because that
    pass rate is the evidence for or against promoting it to required later.
    """
    stats = sorted(result.strategy.stats.rules.items())
    required = [(name, s) for name, s in stats if s.required]
    optional = [(name, s) for name, s in stats if not s.required]
    needed = settings.strategy.min_confluence
    lines = [
        "## Rules",
        "",
        "A rule with zero evaluations was switched off for this run. Required rules block "
        f"on their own; of the optional ones, {needed} had to agree.",
        "",
        "**Required**",
        "",
        _table(
            ["rule", "evaluated", "passed", "intents killed"],
            [[name, f"{s.evaluated}", f"{s.passed}", f"{s.killed}"] for name, s in required],
        ),
    ]
    if optional:
        lines += [
            "",
            "**Confluence**",
            "",
            _table(
                ["rule", "evaluated", "passed", "pass rate"],
                [
                    [
                        name,
                        f"{s.evaluated}",
                        f"{s.passed}",
                        f"{100 * s.passed / s.evaluated:.1f}%" if s.evaluated else "n/a",
                    ]
                    for name, s in optional
                ],
            ),
        ]
    return lines


def _blocks_table(title: str, counts: dict[str, int]) -> str:
    rows = [[key, f"{value}"] for key, value in sorted(counts.items())]
    return f"**{title}**\n\n" + _table(["reason", "count"], rows)
