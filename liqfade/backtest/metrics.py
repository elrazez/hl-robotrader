"""Every metric `docs/STRATEGY_AND_RISK.md` section 6 names, all net of costs.

There is no gross headline anywhere in this module. `gross_pnl` appears once, beside the
fees and funding it paid, so a reader can see what the costs took; every ratio, every
Sharpe and every bps figure is computed from `net_pnl`. CLAUDE.md rule 2.

Two of these deserve their own note because they are the ones that decide whether a result
is real.

**PnL concentration.** The share of net PnL coming from the top five trades. Phase 3's own
stop rule 3 is that a headline which dies when the best five clusters leave the sample is
those five clusters, not an edge. The same test belongs on the backtest.

**The no-book share.** What fraction of fills were priced without a book snapshot. On the
historical year this is most of them, and it is reported per split precisely because the
`l2` cadence drops tenfold from 2026-06-21: a holdout whose fills are degraded differently
from the training window would show a difference that is about the archive rather than
about the strategy. `NO_BOOK_GAP_FLAG_PTS` is the gap at which the report says so.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from liqfade.backtest.portfolio import Trade

__all__ = ["Metrics", "Split", "compute_metrics", "split_by_episode_count"]

# Percentage points of difference in the no-book fill share between two splits at which the
# report stops treating them as comparable. Ten is a judgement rather than a measurement:
# it is wide enough not to fire on ordinary variation and narrow enough to catch the
# 2026-06-21 cadence step, which moves the share far further than that.
NO_BOOK_GAP_FLAG_PTS = 10.0

# Trading days a year, for annualising a per-day Sharpe. Crypto does not close, so it is
# 365 rather than the equities convention of 252.
DAYS_PER_YEAR = 365.0

CONCENTRATION_TOP_N = 5


@dataclass
class Metrics:
    """One split's numbers. Every field is net of fees and funding."""

    trades: int = 0
    net_pnl: float = 0.0
    gross_pnl: float = 0.0
    fees_usd: float = 0.0
    funding_usd: float = 0.0
    hit_rate: float = 0.0
    avg_win_bps: float = 0.0
    avg_loss_bps: float = 0.0
    avg_net_bps: float = 0.0
    profit_factor: float = 0.0
    sharpe_per_trade: float = 0.0
    sortino_per_trade: float = 0.0
    sharpe_daily: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_frac: float = 0.0
    avg_hold_s: float = 0.0
    max_hold_s: float = 0.0
    exposure_frac: float = 0.0
    turnover_usd: float = 0.0
    top5_pnl_share: float = 0.0
    no_book_share: float = 0.0
    stop_rate: float = 0.0
    avg_entry_slippage_bps: float = 0.0
    avg_stop_slippage_bps: float = 0.0
    by_coin: dict[str, float] = field(default_factory=dict)
    by_hour: dict[int, float] = field(default_factory=dict)
    by_cascade_size: dict[str, float] = field(default_factory=dict)
    by_urgency: dict[str, float] = field(default_factory=dict)
    by_exit_reason: dict[str, int] = field(default_factory=dict)


@dataclass
class Split:
    """A named slice of the trade list, with its own metrics."""

    name: str
    trades: list[Trade]
    metrics: Metrics


# Cascade notional buckets, in USD. The lowest boundary is
# `features.episodes.min_notional_usd`, so the first bucket holds anything the research
# definition would not have counted as an episode at all.
CASCADE_BUCKETS: tuple[tuple[str, float], ...] = (
    ("<250k", 250_000.0),
    ("250k-1m", 1_000_000.0),
    ("1m-5m", 5_000_000.0),
    ("5m-20m", 20_000_000.0),
    (">20m", math.inf),
)


def compute_metrics(
    trades: Sequence[Trade],
    *,
    equity_curve: Sequence[tuple[int, float]] = (),
    start_ms: int = 0,
    end_ms: int = 0,
    no_book_share: float = 0.0,
) -> Metrics:
    """Every metric for one set of trades.

    Args:
        trades: completed round trips.
        equity_curve: `(ts_ms, equity)` samples, for the drawdown and the daily Sharpe.
        start_ms: window start, for the exposure fraction.
        end_ms: window end.
        no_book_share: fraction of this split's fills priced without a book.

    Returns:
        A `Metrics`. An empty trade list gives an all-zero `Metrics` rather than raising:
        a split with no trades is a result, and often the interesting one.
    """
    metrics = Metrics(no_book_share=no_book_share)
    if not trades:
        return metrics

    nets = [trade.net_pnl for trade in trades]
    bps = [trade.net_bps for trade in trades]
    wins = [value for value in nets if value > 0]
    losses = [value for value in nets if value <= 0]

    metrics.trades = len(trades)
    metrics.net_pnl = sum(nets)
    metrics.gross_pnl = sum(trade.gross_pnl for trade in trades)
    metrics.fees_usd = sum(trade.fees_usd for trade in trades)
    metrics.funding_usd = sum(trade.funding_usd for trade in trades)
    metrics.hit_rate = len(wins) / len(trades)
    metrics.avg_net_bps = _mean(bps)
    metrics.avg_win_bps = _mean([t.net_bps for t in trades if t.net_pnl > 0])
    metrics.avg_loss_bps = _mean([t.net_bps for t in trades if t.net_pnl <= 0])
    # Infinite when nothing lost, which is honest and which the report renders as such
    # rather than as a very large number that looks measured.
    gross_loss = abs(sum(losses))
    metrics.profit_factor = sum(wins) / gross_loss if gross_loss > 0 else math.inf

    metrics.sharpe_per_trade = _sharpe(bps)
    metrics.sortino_per_trade = _sortino(bps)
    metrics.sharpe_daily = _daily_sharpe(trades)

    metrics.avg_hold_s = _mean([trade.hold_s for trade in trades])
    metrics.max_hold_s = max(trade.hold_s for trade in trades)
    metrics.turnover_usd = sum(trade.size * trade.entry_px for trade in trades)
    metrics.exposure_frac = _exposure(trades, start_ms, end_ms)
    metrics.top5_pnl_share = _concentration(nets)
    metrics.stop_rate = sum(1 for t in trades if t.exit_reason == "stop") / len(trades)
    metrics.avg_entry_slippage_bps = _mean([t.entry_slippage_bps for t in trades])
    metrics.avg_stop_slippage_bps = _mean(
        [t.exit_slippage_bps for t in trades if t.exit_reason == "stop"]
    )

    drawdown, drawdown_frac = _drawdown(equity_curve)
    metrics.max_drawdown = drawdown
    metrics.max_drawdown_frac = drawdown_frac

    metrics.by_coin = _sum_by(trades, lambda t: t.coin)
    metrics.by_hour = _sum_by(trades, lambda t: datetime.fromtimestamp(t.entry_ms / 1000, UTC).hour)
    metrics.by_cascade_size = _sum_by(trades, _cascade_bucket)
    metrics.by_urgency = _sum_by(trades, lambda t: t.urgency)
    metrics.by_exit_reason = _count_by(trades, lambda t: t.exit_reason)
    return metrics


def split_by_episode_count(
    trades: Sequence[Trade], holdout_frac: float
) -> tuple[list[Trade], list[Trade]]:
    """Chronological train/holdout split, by **trade count** rather than by days.

    The prompt's requirement, and it is not cosmetic. Splitting a year by date puts the
    boundary wherever the calendar falls, and cascades are not uniform in time: a quiet
    quarter and a violent one would contribute wildly different numbers of episodes to a
    split that looked even. Splitting by count puts the boundary where the sample is, so
    both sides carry comparable evidence.

    Trades are ordered by entry time first, so the split is still chronological: the
    holdout is the last `holdout_frac` of trades as they happened, not a random subset.
    """
    ordered = sorted(trades, key=lambda trade: (trade.entry_ms, trade.coin))
    if not ordered:
        return [], []
    cut = len(ordered) - round(len(ordered) * holdout_frac)
    cut = max(0, min(cut, len(ordered)))
    return ordered[:cut], ordered[cut:]


# --- internals ----------------------------------------------------------------


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stdev(values: Sequence[float]) -> float:
    """Sample standard deviation. Zero for fewer than two values, which is not a spread."""
    if len(values) < 2:
        return 0.0
    average = _mean(values)
    return math.sqrt(sum((value - average) ** 2 for value in values) / (len(values) - 1))


def _sharpe(values: Sequence[float]) -> float:
    """Mean over standard deviation. Zero, not infinity, when there is no spread.

    A single trade has no volatility and therefore no Sharpe. Returning infinity there
    would put the best-looking number in the report on the thinnest evidence in it.
    """
    spread = _stdev(values)
    return _mean(values) / spread if spread > 0 else 0.0


def _sortino(values: Sequence[float]) -> float:
    """Mean over downside deviation, counting only the losses."""
    downside = [value for value in values if value < 0]
    if not downside:
        return 0.0
    deviation = math.sqrt(sum(value**2 for value in downside) / len(downside))
    return _mean(values) / deviation if deviation > 0 else 0.0


def _daily_sharpe(trades: Sequence[Trade]) -> float:
    """Annualised Sharpe of daily net PnL, on the days that traded.

    Days with no trade are not zeros in this series. A strategy that trades twice a week is
    not one with five flat days between; including them would divide the same PnL by a
    larger volatility and understate a real result for a reason that is about the calendar.
    """
    by_day: dict[str, float] = {}
    for trade in trades:
        day = datetime.fromtimestamp(trade.exit_ms / 1000, UTC).strftime("%Y-%m-%d")
        by_day[day] = by_day.get(day, 0.0) + trade.net_pnl
    series = list(by_day.values())
    if len(series) < 2:
        return 0.0
    return _sharpe(series) * math.sqrt(DAYS_PER_YEAR)


def _drawdown(curve: Sequence[tuple[int, float]]) -> tuple[float, float]:
    """Largest peak to trough fall in the equity curve, in USD and as a fraction."""
    peak = -math.inf
    worst_usd, worst_frac = 0.0, 0.0
    for _, equity in curve:
        peak = max(peak, equity)
        fall = peak - equity
        if fall > worst_usd:
            worst_usd = fall
        if peak > 0 and fall / peak > worst_frac:
            worst_frac = fall / peak
    return worst_usd, worst_frac


def _exposure(trades: Sequence[Trade], start_ms: int, end_ms: int) -> float:
    """Fraction of the window with at least one position open.

    Overlapping holds count once. Summing hold times would exceed the window whenever the
    strategy is in several coins at once, which is exactly what it is built to do during a
    sweep, and would report an exposure over 100%.
    """
    if end_ms <= start_ms or not trades:
        return 0.0
    spans = sorted((trade.entry_ms, trade.exit_ms) for trade in trades)
    covered, current_start, current_end = 0, spans[0][0], spans[0][1]
    for begin, finish in spans[1:]:
        if begin > current_end:
            covered += current_end - current_start
            current_start, current_end = begin, finish
        else:
            current_end = max(current_end, finish)
    covered += current_end - current_start
    return covered / (end_ms - start_ms)


def _concentration(nets: Sequence[float]) -> float:
    """Share of total net PnL from the best five trades.

    Returns 0 when the total is not positive: a losing run has no concentration of profit
    to describe, and dividing by a negative total produces a number that reads backwards.
    """
    total = sum(nets)
    if total <= 0:
        return 0.0
    top = sorted(nets, reverse=True)[:CONCENTRATION_TOP_N]
    return sum(top) / total


def _cascade_bucket(trade: Trade) -> str:
    for name, ceiling in CASCADE_BUCKETS:
        if trade.cascade_notional_usd < ceiling:
            return name
    return CASCADE_BUCKETS[-1][0]


def _sum_by[Bucket: (str, int)](
    trades: Sequence[Trade], key: Callable[[Trade], Bucket]
) -> dict[Bucket, float]:
    """Net PnL per bucket, ordered by the bucket's string form so a rerun matches."""
    out: dict[Bucket, float] = {}
    for trade in trades:
        bucket = key(trade)
        out[bucket] = out.get(bucket, 0.0) + trade.net_pnl
    return dict(sorted(out.items(), key=lambda item: str(item[0])))


def _count_by[Bucket: (str, int)](
    trades: Sequence[Trade], key: Callable[[Trade], Bucket]
) -> dict[Bucket, int]:
    """Trade count per bucket, ordered the same way."""
    out: dict[Bucket, int] = {}
    for trade in trades:
        bucket = key(trade)
        out[bucket] = out.get(bucket, 0) + 1
    return dict(sorted(out.items(), key=lambda item: str(item[0])))
