"""What an intent we refused would have made, priced as a taker against recorded tape.

**This is a bound, not an answer, and the report says so.** The exact question for a filter
or a rule is a second backtest with it switched off (`filters.*`, `strategy.rules.*`),
which `liqfade lab` already runs over months. This exists for the week that just happened:
after a paper soak, "what did `spread_too_wide` cost me" should be answerable from the run
that produced the refusals rather than only from a replay.

The fill model is deliberately the crude one. The entry crosses at the first recorded print
at or after `ts_ms + backtest.latency_ms`; there is no queue, no book walk and no partial.
Two rules from braindeadpt's `shadow_outcome_evaluator.py` are copied because they bias the
counterfactual the right way, and a counterfactual that flatters the trades you did not
take is worse than not measuring at all, because it argues for removing filters:

- when the same second touches both the target and the stop, **it resolves as the stop**;
- a refusal whose window the lake cannot cover is `no_tape`, **never a zero**.

Prints are read in `effective_ts` order (`features/grid.py`), which is `rx_ms` for recorded
tape and `ts_ms` for the backfilled year, so a print that had not reached us by the fill
time cannot fill the order.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import polars as pl

from liqfade.backtest.costs import fee_for
from liqfade.backtest.data import load_day, utc_days
from liqfade.backtest.fills import stop_triggered
from liqfade.config.models import Settings
from liqfade.features.grid import effective_ts
from liqfade.strategy.engine import Rejection

__all__ = [
    "Group",
    "Outcome",
    "group_outcomes",
    "outcomes_frame",
    "render_report",
    "score_all",
    "score_refusal",
]

# Pinned so an empty window still writes a readable Parquet rather than a schemaless one.
OUTCOME_SCHEMA = {
    "ts_ms": pl.Int64,
    "coin": pl.String,
    "side": pl.String,
    "stage": pl.String,
    "reason": pl.String,
    "intent_entry_px": pl.Float64,
    "target_px": pl.Float64,
    "stop_px": pl.Float64,
    "outcome": pl.String,
    "entry_ms": pl.Int64,
    "entry_px": pl.Float64,
    "exit_ms": pl.Int64,
    "exit_px": pl.Float64,
    "notional_usd": pl.Float64,
    "gross_pnl": pl.Float64,
    "fees": pl.Float64,
    "net_pnl": pl.Float64,
    "net_bps": pl.Float64,
}

# The grid convention: one second is one tick, and inside it the stop is first.
BUCKET_MS = 1000

ExitReason = Literal["target", "stop", "timeout", "no_tape"]


@dataclass(frozen=True, slots=True)
class Outcome:
    """One refused intent, replayed. `no_tape` fills nothing and carries zeros.

    Args:
        reason: how it would have ended. `no_tape` means the lake could not answer,
            either because no print followed the fill time or because the window ran out
            before an exit. It is reported as its own count and never summed as a zero.
        entry_px: the print it would have crossed at, 0 when `no_tape`.
        exit_px: the print it would have left at, 0 when `no_tape`.
        entry_ms: when that fill happened.
        exit_ms: when the exit happened.
        notional_usd: sizing's risk-budget rule at the given equity, before any cap.
        gross_pnl: USD before fees.
        fees: USD, taker both sides. Funding is ignored; holds are minutes.
        net_pnl: `gross_pnl - fees`.
        net_bps: `net_pnl` over `notional_usd`, in basis points.
    """

    reason: ExitReason
    entry_px: float = 0.0
    exit_px: float = 0.0
    entry_ms: int = 0
    exit_ms: int = 0
    notional_usd: float = 0.0
    gross_pnl: float = 0.0
    fees: float = 0.0
    net_pnl: float = 0.0
    net_bps: float = 0.0


def notional_for(refusal: Rejection, *, equity: float, settings: Settings) -> float:
    """Sizing's first rule at this equity, before any cap.

    `risk_frac * equity / stop_distance_frac`, which is `sizing/__init__.py`'s
    `risk_budget` cap. The liquidity and exposure caps are deliberately not applied: the
    account's equity at that millisecond is not persisted and neither is the coin's depth,
    so every one of them would be guessed. This is the risk-budget bound and the report
    says so.
    """
    stop_distance = abs(refusal.entry_px - refusal.stop_px)
    if equity <= 0 or stop_distance <= 0 or refusal.entry_px <= 0:
        return 0.0
    return equity * settings.sizing.risk_frac / stop_distance * refusal.entry_px


def score_refusal(
    refusal: Rejection,
    prints: Sequence[tuple[int, float]],
    settings: Settings,
    *,
    equity: float,
) -> Outcome:
    """Replay one refused intent against the tape that followed it.

    Args:
        refusal: the intent a gate threw away, with the prices it was built with.
        prints: `(effective_ts_ms, px)` for that coin, ascending. Only the refusal's own
            window is ever passed; the caller reads the lake a window at a time.
        settings: resolved config. `backtest.latency_ms` sets the fill time and
            `backtest.taker_fee_frac` the cost of both sides.
        equity: the account equity to size against.

    Returns:
        An `Outcome`. Every number is net of a taker fee per side.
    """
    fill_ms = refusal.ts_ms + settings.backtest.latency_ms
    entry = next(((ts, px) for ts, px in prints if ts >= fill_ms and px > 0), None)
    if entry is None:
        return Outcome(reason="no_tape")
    entry_ms, entry_px = entry

    deadline_ms = entry_ms + int(refusal.time_stop_s * 1000)
    exit_at = _exit(refusal, prints, entry_ms=entry_ms, deadline_ms=deadline_ms)
    if exit_at is None:
        return Outcome(reason="no_tape")
    exit_ms, exit_px, reason = exit_at

    notional = notional_for(refusal, equity=equity, settings=settings)
    size = notional / entry_px
    direction = 1.0 if refusal.side == "long" else -1.0
    gross = size * (exit_px - entry_px) * direction
    fees = fee_for(notional, maker=False, settings=settings) + fee_for(
        size * exit_px, maker=False, settings=settings
    )
    net = gross - fees
    return Outcome(
        reason=reason,
        entry_px=entry_px,
        exit_px=exit_px,
        entry_ms=entry_ms,
        exit_ms=exit_ms,
        notional_usd=notional,
        gross_pnl=gross,
        fees=fees,
        net_pnl=net,
        net_bps=1e4 * net / notional if notional > 0 else 0.0,
    )


def _exit(
    refusal: Rejection,
    prints: Sequence[tuple[int, float]],
    *,
    entry_ms: int,
    deadline_ms: int,
) -> tuple[int, float, ExitReason] | None:
    """The first print that ends the position, or None when the tape runs out first."""
    after = [(ts, px) for ts, px in prints if ts >= entry_ms and px > 0]
    for index, (ts, px) in enumerate(after):
        if ts >= deadline_ms:
            return ts, px, "timeout"
        hit_stop = stop_triggered(side=refusal.side, stop_px=refusal.stop_px, mid=px)
        hit_target = px >= refusal.target_px if refusal.side == "long" else px <= refusal.target_px
        if not (hit_stop or hit_target):
            continue
        if hit_target and not hit_stop:
            # The same second may also touch the stop. Recorded tape has no bar to read
            # the order off, and a counterfactual that takes the good half of an ambiguous
            # second is the exact error that argues for deleting a filter that was earning.
            stopped = _stop_in_bucket(refusal, after[index:], ts)
            if stopped is not None:
                return stopped[0], stopped[1], "stop"
        return ts, px, "stop" if hit_stop else "target"
    return None


def _stop_in_bucket(
    refusal: Rejection, rest: Sequence[tuple[int, float]], ts: int
) -> tuple[int, float] | None:
    """The stop print inside the same one-second bucket as `ts`, if there is one."""
    bucket = ts // BUCKET_MS
    for other_ts, other_px in rest:
        if other_ts // BUCKET_MS != bucket:
            return None
        if stop_triggered(side=refusal.side, stop_px=refusal.stop_px, mid=other_px):
            return other_ts, other_px
    return None


# --- reading the tape a window at a time ----------------------------------------

# Milliseconds of tape read past a refusal's time stop, so the exit walk has a print to
# resolve the timeout against. Without it every long-horizon refusal reads as `no_tape`.
TAIL_MS = 60_000


def window_of(refusal: Rejection, settings: Settings) -> tuple[int, int]:
    """The `[from, to)` on `effective_ts` that scoring this refusal can possibly need."""
    start = refusal.ts_ms + settings.backtest.latency_ms
    return start, start + int(refusal.time_stop_s * 1000) + TAIL_MS


def score_all(
    refusals: Sequence[Rejection], data_dir: Path, settings: Settings, *, equity: float
) -> list[tuple[Rejection, Outcome]]:
    """Score every refusal, reading the lake one UTC day at a time.

    The spans are pushed into the Parquet scan, so a day is read only inside the windows
    the refusals name: a week of a few hundred refusals reads seconds of tape rather than
    days of it. `backtest/source.py` reads the lake the same way and for the same reason,
    which is that the laptop has 8 GB and the lake is on a USB disk.
    """
    windows = {id(r): window_of(r, settings) for r in refusals}
    per_day: dict[str, dict[str, list[tuple[int, int]]]] = {}
    for refusal in refusals:
        from_ms, to_ms = windows[id(refusal)]
        for day in utc_days(from_ms, to_ms):
            per_day.setdefault(day, {}).setdefault(refusal.coin, []).append((from_ms, to_ms))

    prints: dict[str, list[tuple[int, float]]] = {}
    for day, spans in sorted(per_day.items()):
        for coin, rows in _day_prints(data_dir, day, spans).items():
            prints.setdefault(coin, []).extend(rows)
    for rows in prints.values():
        rows.sort()

    scored = []
    for refusal in refusals:
        from_ms, to_ms = windows[id(refusal)]
        coin_prints = prints.get(refusal.coin, [])
        lo = bisect_left(coin_prints, (from_ms, float("-inf")))
        hi = bisect_left(coin_prints, (to_ms, float("-inf")))
        scored.append(
            (refusal, score_refusal(refusal, coin_prints[lo:hi], settings, equity=equity))
        )
    return scored


def _day_prints(
    data_dir: Path, day: str, spans: Mapping[str, list[tuple[int, int]]]
) -> dict[str, list[tuple[int, float]]]:
    """One day's prints inside the given spans, per coin, on `effective_ts`."""
    frame = load_day(data_dir, "trades", day, coins=list(spans), spans=spans)
    if frame.is_empty():
        return {}
    marked = frame.with_columns(effective_ts()).sort("known")
    return {
        str(coin): list(zip(rows["known"].to_list(), rows["px"].to_list(), strict=True))
        for (coin,), rows in marked.group_by("coin")
    }


# --- the report ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Group:
    """Every refusal that shares a `(stage, reason)`, and what they came to."""

    stage: str
    reason: str
    scored: int
    target: int
    stop: int
    timeout: int
    no_tape: int
    net_pnl: float
    mean_net_bps: float

    @property
    def total(self) -> int:
        return self.scored + self.no_tape

    def verdict(self) -> str:
        """The line a reader takes away, in plain words rather than a sign convention."""
        if self.scored == 0:
            return f"`{self.reason}` refused {self.total} entries the lake cannot price."
        earned = "the gate earned its keep" if self.net_pnl < 0 else "the gate cost money"
        return (
            f"`{self.reason}` refused {self.scored} entries that would have netted "
            f"{self.net_pnl:+,.2f} USD: {earned}."
        )


def group_outcomes(scored: Sequence[tuple[Rejection, Outcome]]) -> list[Group]:
    """Fold the scored refusals by `(stage, reason)`, worst-for-us first.

    `no_tape` is counted and never averaged in. A refusal the lake cannot answer for is
    not a refusal that would have broken even, and averaging it as one would drag every
    group towards zero in exactly the coins whose tape has holes.
    """
    buckets: dict[tuple[str, str], list[tuple[Rejection, Outcome]]] = {}
    for refusal, outcome in scored:
        buckets.setdefault((refusal.stage, refusal.reason), []).append((refusal, outcome))

    groups = []
    for (stage, reason), rows in buckets.items():
        priced = [o for _r, o in rows if o.reason != "no_tape"]
        net = sum(o.net_pnl for o in priced)
        groups.append(
            Group(
                stage=stage,
                reason=reason,
                scored=len(priced),
                target=sum(1 for o in priced if o.reason == "target"),
                stop=sum(1 for o in priced if o.reason == "stop"),
                timeout=sum(1 for o in priced if o.reason == "timeout"),
                no_tape=len(rows) - len(priced),
                net_pnl=net,
                mean_net_bps=(sum(o.net_bps for o in priced) / len(priced)) if priced else 0.0,
            )
        )
    # Most expensive gate first: the one that cost the most is the one to argue about.
    return sorted(groups, key=lambda g: (-g.net_pnl, g.stage, g.reason))


def render_report(
    groups: Sequence[Group],
    *,
    start: str,
    end: str,
    equity: float,
    settings: Settings,
    total: int,
) -> str:
    """The one-page markdown. Its first paragraph is the caveat, on purpose."""
    lines = [
        f"# Refusals, {start} to {end}",
        "",
        "**This is a bound, not an answer.** Every intent the live loop refused is replayed "
        "here as a taker that crosses at the first recorded print after "
        f"`{settings.backtest.latency_ms} ms` of latency, with no queue, no book walk and no "
        "partial fill. The exact question for a filter or a rule is a second backtest with it "
        "switched off (`filters.*`, `strategy.rules.*`), which `liqfade lab` already runs. "
        "This command exists for the week that just happened rather than for the year.",
        "",
        "Prints are read in `effective_ts` order (`rx_ms` for recorded tape, `ts_ms` for the "
        "backfilled year), so a print that had not reached us by the fill time cannot fill. "
        "When one second touches both the target and the stop it resolves as the **stop**. A "
        "refusal whose window the lake cannot cover is `no tape` and is never scored as zero.",
        "",
        f"Every number is **net** of a taker fee per side "
        f"({settings.backtest.taker_fee_frac * 100:.3f}% of notional each way). Funding is "
        "ignored: these holds are minutes and never cross an hour boundary often enough to "
        "matter. Notional is sizing's risk-budget rule at "
        f"`--equity {equity:,.0f}` before any liquidity or exposure cap, because neither the "
        "account's equity at that millisecond nor the coin's depth is persisted.",
        "",
        f"{total} refusals in the window.",
        "",
        "| stage | reason | scored | target | stop | timeout | no tape | net USD | mean bps |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for group in groups:
        lines.append(
            f"| {group.stage} | `{group.reason}` | {group.scored} | {group.target} | "
            f"{group.stop} | {group.timeout} | {group.no_tape} | {group.net_pnl:+,.2f} | "
            f"{group.mean_net_bps:+.1f} |"
        )
    lines += ["", "## What each gate cost", ""]
    lines += [f"- {group.verdict()}" for group in groups] or ["- Nothing was refused."]
    lines.append("")
    return "\n".join(lines)


def outcomes_frame(scored: Sequence[tuple[Rejection, Outcome]]) -> pl.DataFrame:
    """One row per refusal, for a reader who wants the trades rather than the summary."""
    return pl.DataFrame(
        [
            {
                "ts_ms": r.ts_ms,
                "coin": r.coin,
                "side": r.side,
                "stage": r.stage,
                "reason": r.reason,
                "intent_entry_px": r.entry_px,
                "target_px": r.target_px,
                "stop_px": r.stop_px,
                "outcome": o.reason,
                "entry_ms": o.entry_ms,
                "entry_px": o.entry_px,
                "exit_ms": o.exit_ms,
                "exit_px": o.exit_px,
                "notional_usd": o.notional_usd,
                "gross_pnl": o.gross_pnl,
                "fees": o.fees,
                "net_pnl": o.net_pnl,
                "net_bps": o.net_bps,
            }
            for r, o in scored
        ],
        schema=OUTCOME_SCHEMA,
    )
