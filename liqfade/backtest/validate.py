"""Holdout, walk-forward, bootstrap intervals and the deflated Sharpe.

The part of the backtest whose job is to disbelieve the rest of it.

**Holdout** is the last `backtest.holdout_frac` of trades by count, not by days. See
`metrics.split_by_episode_count` for why.

**Walk-forward** on v0 reduces to reporting metric stability per window, because v0 fits
nothing: every parameter came from Phase 3 and is frozen in config. The scaffolding is
built anyway, so that the first parameter anyone does fit has somewhere to be validated
rather than being fitted on everything.

**Bootstrap** resamples trades with replacement to put an interval on the mean. Trade level
rather than cluster level, unlike Phase 3's `cluster_bootstrap`: the per-cluster exposure
cap means the backtest has already limited how much of one sweep it can be in, so the
trades that survive it are much closer to independent than the raw episodes were.

**Deflated Sharpe** discounts the observed Sharpe by how many configurations were tried.
`n_trials` comes from `reports/.n_trials`, a counter incremented on every backtest run, so
it is an honest tally rather than a number chosen when the report is written. Trying twenty
configurations and reporting the best one without this is how a backtest finds an edge in
its own noise.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from liqfade.backtest.metrics import Metrics, compute_metrics
from liqfade.backtest.portfolio import Trade

__all__ = [
    "BootstrapCI",
    "WalkForwardWindow",
    "bootstrap_mean_bps",
    "bump_n_trials",
    "deflated_sharpe",
    "read_n_trials",
    "walk_forward",
]

N_TRIALS_FILE = ".n_trials"
# Euler-Mascheroni, for the expected maximum of n independent Sharpe draws.
_EULER = 0.5772156649015329


@dataclass(frozen=True, slots=True)
class BootstrapCI:
    """A resampled interval on the mean net bps per trade."""

    mean: float
    lo: float
    hi: float
    draws: int
    n: int

    @property
    def excludes_zero(self) -> bool:
        """True when the whole interval is one side of zero, which is the bar that matters."""
        return self.lo > 0 or self.hi < 0


@dataclass
class WalkForwardWindow:
    """One walk-forward window and what the strategy did in it."""

    index: int
    start_ms: int
    end_ms: int
    metrics: Metrics


def bootstrap_mean_bps(
    trades: Sequence[Trade], *, draws: int, seed: int = 0, confidence: float = 0.95
) -> BootstrapCI:
    """Resample trades with replacement and interval the mean net bps.

    Seeded, so a rerun of the same command produces the same interval. An unseeded
    bootstrap would make two identical runs disagree in the report's headline.
    """
    values = [trade.net_bps for trade in trades]
    if len(values) < 2:
        mean = values[0] if values else 0.0
        return BootstrapCI(mean=mean, lo=mean, hi=mean, draws=0, n=len(values))

    # Seeded, and not a cryptographic use: an unseeded bootstrap would make two
    # identical runs disagree in the report's headline interval.
    rng = random.Random(seed)  # noqa: S311
    n = len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(draws))
    tail = (1.0 - confidence) / 2.0
    lo = means[max(int(tail * draws) - 1, 0)]
    hi = means[min(int((1.0 - tail) * draws), draws - 1)]
    return BootstrapCI(mean=sum(values) / n, lo=lo, hi=hi, draws=draws, n=n)


def walk_forward(
    trades: Sequence[Trade], *, start_ms: int, end_ms: int, refit_days: int
) -> list[WalkForwardWindow]:
    """Cut the window into `refit_days` slices and compute each one's metrics.

    v0 fits nothing, so this reports stability rather than validating a fit: if the metrics
    swing wildly window to window, the pooled number is an average over regimes rather than
    a description of any of them. The scaffolding is what the first fitted parameter will
    need.
    """
    span_ms = refit_days * 86_400_000
    if span_ms <= 0 or end_ms <= start_ms:
        return []
    windows: list[WalkForwardWindow] = []
    index, cursor = 0, start_ms
    while cursor < end_ms:
        stop = min(cursor + span_ms, end_ms)
        inside = [trade for trade in trades if cursor <= trade.entry_ms < stop]
        windows.append(
            WalkForwardWindow(
                index=index,
                start_ms=cursor,
                end_ms=stop,
                metrics=compute_metrics(inside, start_ms=cursor, end_ms=stop),
            )
        )
        index += 1
        cursor = stop
    return windows


def deflated_sharpe(observed: float, *, n_trials: int, n_obs: int) -> float:
    """Probability the observed Sharpe beats what the best of `n_trials` would show anyway.

    Bailey and Lopez de Prado's deflated Sharpe, in its simplest form: the expected maximum
    Sharpe of `n_trials` independent strategies with no edge is not zero, it grows with the
    number tried, and the observed Sharpe has to clear that before it means anything.

    Returns a probability in `[0, 1]`. Under 0.95 means the result is inside what trying
    this many configurations would produce from noise alone.
    """
    if n_obs < 2 or n_trials < 1:
        return 0.0
    if n_trials == 1:
        expected_max = 0.0
    else:
        # Expected maximum of n standard normal draws, to the usual two term approximation.
        quantile = _inv_norm(1.0 - 1.0 / n_trials)
        second = _inv_norm(1.0 - 1.0 / (n_trials * math.e))
        expected_max = (1.0 - _EULER) * quantile + _EULER * second
    spread = math.sqrt(1.0 / (n_obs - 1))
    if spread <= 0:
        return 0.0
    return _norm_cdf((observed - expected_max) / spread)


def read_n_trials(reports_dir: Path) -> int:
    """How many backtests have been run. Zero when the counter does not exist yet."""
    path = Path(reports_dir) / N_TRIALS_FILE
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def bump_n_trials(reports_dir: Path) -> int:
    """Increment the counter and return the new value.

    Every run counts, including the ones whose reports get deleted. That is the point: the
    deflated Sharpe is only honest if the tally counts configurations tried rather than
    configurations kept, and a counter that only advanced on results worth keeping would
    understate exactly when it mattered most.
    """
    directory = Path(reports_dir)
    directory.mkdir(parents=True, exist_ok=True)
    total = read_n_trials(directory) + 1
    (directory / N_TRIALS_FILE).write_text(f"{total}\n")
    return total


def utc_stamp(ms: int) -> str:
    """`YYYY-MM-DD HH:MM` UTC, for report headings."""
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M")


def _norm_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _inv_norm(probability: float) -> float:
    """Inverse standard normal CDF, Acklam's rational approximation.

    Hand rolled because CLAUDE.md forbids adding a dependency for what a few lines can do,
    and scipy is a research-group extra that the package must not import.
    """
    if not 0.0 < probability < 1.0:
        return 0.0
    a = (
        -39.69683028665376,
        220.9460984245205,
        -275.9285104469687,
        138.3577518672690,
        -30.66479806614716,
        2.506628277459239,
    )
    b = (
        -54.47609879822406,
        161.5858368580409,
        -155.6989798598866,
        66.80131188771972,
        -13.28068155288572,
    )
    c = (
        -0.007784894002430293,
        -0.3223964580411365,
        -2.400758277161838,
        -2.549732539343734,
        4.374664141464968,
        2.938163982698783,
    )
    d = (0.007784695709041462, 0.3224671290700398, 2.445134137142996, 3.754408661907416)
    low, high = 0.02425, 1.0 - 0.02425
    if probability < low:
        q = math.sqrt(-2.0 * math.log(probability))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if probability > high:
        q = math.sqrt(-2.0 * math.log(1.0 - probability))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    q = probability - 0.5
    r = q * q
    return (
        (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
        * q
        / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
    )
