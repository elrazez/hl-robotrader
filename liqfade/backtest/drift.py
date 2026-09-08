"""Live versus replay: the Phase 5 DoD sentence, as a command with a verdict.

`docs/ROADMAP.md` Phase 5 asks whether paper results land in a plausible band of the
backtest's for the same days. `live/paper.py` argues they must, because paper *is* the
backtest engine driven by a live clock rather than a second implementation of it. This
module is where that argument gets a number instead of a docstring.

If the claim holds the diff is small, and what is left over has names: the online feature
engine against the vectorized grid on real tape rather than synthetic, wall-clock ticks
against grid ticks, the live process's own sockets against the recorder's (two
connections, two sets of holes), and risk state carried across days that a replay starts
clean. Each of those shows up as one dimension with one verdict.

**Every dimension answers `MATCH`, `DRIFT` or `NOT_COMPARABLE`.** The third is the
discipline `docs/research/external_bots_scan.md` section 3 asked to be copied verbatim: a
field the live side genuinely does not persist is reported as not comparable, never
defaulted to zero and diffed against the default. A comparator that quietly invents the
missing half argues for changes nobody can check.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from statistics import median
from typing import Protocol, cast

import polars as pl

from liqfade.backtest.costs import fee_for
from liqfade.backtest.data import load_day, utc_days
from liqfade.backtest.engine import BacktestResult
from liqfade.backtest.portfolio import Trade
from liqfade.backtest.report import config_hash
from liqfade.backtest.validate import bootstrap_mean_bps
from liqfade.config.models import Settings
from liqfade.execution.orders import Order, OrderStore
from liqfade.features.grid import effective_ts

__all__ = [
    "Dimension",
    "DriftReport",
    "Hole",
    "IntentRef",
    "Match",
    "Metric",
    "RoundTrip",
    "Side",
    "Verdict",
    "compare",
    "find_holes",
    "match_intents",
    "read_live",
    "read_replay",
    "render_report",
    "report_payload",
]


class Keyed(Protocol):
    """Anything the matcher can pair: an entry attempt, or a completed round trip."""

    @property
    def coin(self) -> str: ...

    @property
    def side(self) -> str: ...

    @property
    def ts_ms(self) -> int: ...


@dataclass(frozen=True)
class IntentRef:
    """One entry attempt, on either side of the comparison.

    Args:
        coin: the asset.
        side: `long` or `short`, as the strategy names it.
        ts_ms: when the intent was emitted. The matcher's only clock.
        entry_px: the price the entry actually filled at, or None when the intent never
            became a filled order. None is not zero: an unmatched-on-price intent is
            excluded from the entries verdict rather than diffed against a made-up price.
        stage: for a refused intent, which gate refused it. Empty for one that was placed.
        reason: for a refused intent, why. Empty for one that was placed.
    """

    coin: str
    side: str
    ts_ms: int
    entry_px: float | None = None
    stage: str = ""
    reason: str = ""


@dataclass(frozen=True)
class Match[K: Keyed]:
    """What pairing one side against the other produced. Leftovers in timestamp order."""

    matched: list[tuple[K, K]] = field(default_factory=list)
    live_only: list[K] = field(default_factory=list)
    replay_only: list[K] = field(default_factory=list)


def match_intents[K: Keyed](live: list[K], replay: list[K], *, tol_ms: int) -> Match[K]:
    """Pair each live entry attempt with at most one replay attempt, and vice versa.

    Two attempts pair when they share a coin and a side and their timestamps are within
    `tol_ms` of each other, inclusive. Wall-clock ticks and grid ticks do not land on the
    same millisecond even when they make the same decision, which is the whole reason for
    a tolerance rather than an equality.

    Closest pair first, not live order. Walking live in timestamp order and taking the
    first candidate inside the tolerance pairs an early live intent with a replay intent
    that belongs to a later one, and then reports both of the true pair as unmatched: one
    real difference read as two. Ties break on the replay side's timestamp and then its
    position, so a rerun of the same command reports the same drift.

    Args:
        live: entry attempts read off the running loop's registry.
        replay: entry attempts the backtest produced over the same window.
        tol_ms: how far apart two attempts may be and still be the same decision.

    Returns:
        The pairs, then whatever each side had left over, each in timestamp order.
    """
    # ponytail: O(n*m) over every candidate pair. A week of cascades is hundreds of
    # intents, so this is microseconds; bucket by (coin, side, ts_ms // tol_ms) if a
    # month-long window ever makes it show up.
    candidates = sorted(
        (
            (abs(left.ts_ms - right.ts_ms), right.ts_ms, j, i)
            for i, left in enumerate(live)
            for j, right in enumerate(replay)
            if left.coin == right.coin and left.side == right.side
            if abs(left.ts_ms - right.ts_ms) <= tol_ms
        )
    )
    taken_live: set[int] = set()
    taken_replay: set[int] = set()
    pairs: list[tuple[int, K, K]] = []
    for _, _, j, i in candidates:
        if i in taken_live or j in taken_replay:
            continue
        taken_live.add(i)
        taken_replay.add(j)
        pairs.append((live[i].ts_ms, live[i], replay[j]))
    return Match(
        matched=[(left, right) for _, left, right in sorted(pairs, key=lambda p: p[0])],
        live_only=sorted(
            (item for i, item in enumerate(live) if i not in taken_live),
            key=lambda item: item.ts_ms,
        ),
        replay_only=sorted(
            (item for j, item in enumerate(replay) if j not in taken_replay),
            key=lambda item: item.ts_ms,
        ),
    )


# --- the two sides --------------------------------------------------------------------


@dataclass(frozen=True)
class RoundTrip:
    """One completed entry-to-exit, on either side. `portfolio.Trade`, narrowed.

    Only the fields the diff reads, so a test can build one by hand and so `read_live`
    does not have to invent the slippage and funding columns a `Trade` carries and the
    live registry does not record.
    """

    coin: str
    side: str
    ts_ms: int
    exit_ms: int
    exit_reason: str
    entry_px: float
    exit_px: float
    net_pnl: float
    notional: float

    @property
    def net_bps(self) -> float:
        """Net PnL as basis points of entry notional. The name `bootstrap_mean_bps` reads."""
        return 1e4 * self.net_pnl / self.notional if self.notional else 0.0


@dataclass(frozen=True)
class Side:
    """One side of the comparison: what the live loop did, or what the replay did.

    Args:
        intents: every entry attempt, placed or refused.
        round_trips: every completed entry-to-exit.
        refusals: `(stage, reason)` to count. **Empty means the side recorded none at
            all**, which is reported as not comparable rather than as agreement: the
            soak that predates the refusals table has an empty table, not an empty week.
        config_hash: `report.config_hash` of the settings the side ran under, or None
            when the side does not record it.
        abandoned_entries: entry orders that were placed and never filled. A tally rather
            than a list, because the replay keeps a counter of these and not a log.
    """

    intents: list[IntentRef]
    round_trips: list[RoundTrip]
    refusals: dict[tuple[str, str], int]
    config_hash: str | None = None
    abandoned_entries: int = 0


@dataclass(frozen=True)
class Hole:
    """A stretch of one coin's tape the lake does not have, `[start_ms, end_ms)`."""

    coin: str
    start_ms: int
    end_ms: int

    def holds(self, coin: str, ts_ms: int) -> bool:
        return coin == self.coin and self.start_ms <= ts_ms < self.end_ms


# --- the verdict ----------------------------------------------------------------------


class Verdict(StrEnum):
    """What one dimension concluded. `NOT_COMPARABLE` is never a quiet `MATCH`."""

    MATCH = "MATCH"
    DRIFT = "DRIFT"
    NOT_COMPARABLE = "NOT_COMPARABLE"


@dataclass(frozen=True)
class Metric:
    """One number a dimension measured, with the bar it was held to."""

    name: str
    value: float
    threshold: float
    units: str
    ok: bool


@dataclass(frozen=True)
class Dimension:
    """One thing the two sides were compared on."""

    name: str
    verdict: Verdict
    metrics: list[Metric] = field(default_factory=list)
    detail: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DriftReport:
    """Every dimension's verdict, and what the tape holes took out of the comparison."""

    dimensions: list[Dimension]
    excluded_by_hole: int = 0

    @property
    def verdict(self) -> Verdict:
        """The worst dimension. `DRIFT` anywhere is `DRIFT`; nothing hides behind absence."""
        verdicts = {d.verdict for d in self.dimensions}
        if Verdict.DRIFT in verdicts:
            return Verdict.DRIFT
        if Verdict.NOT_COMPARABLE in verdicts:
            return Verdict.NOT_COMPARABLE
        return Verdict.MATCH


# The bars. Fixed rather than configurable: a threshold the caller can move is a threshold
# that moves until the report says MATCH.
TOL_MS = 2_000
MATCHED_SHARE_MIN = 0.90
PRICE_BPS_MAX = 5.0
EXIT_REASON_SHARE_MIN = 0.90
REFUSAL_FRAC = 0.10
REFUSAL_FLOOR = 2.0


def _median(values: list[float]) -> float:
    return float(median(values)) if values else 0.0


def _bps(left: float, right: float) -> float:
    """Absolute difference between two prices, in basis points of the replay's."""
    return abs(left - right) / right * 1e4 if right else 0.0


def compare(
    live: Side,
    replay: Side,
    *,
    settings: Settings,
    holes: Sequence[Hole] = (),
    tol_ms: int = TOL_MS,
) -> DriftReport:
    """Diff what the live loop did against what the replay did over the same window.

    Args:
        live: read off the running loop's order registry.
        replay: what `run_backtest` produced over the same days and config.
        settings: the resolved config, for the fee rate and the bootstrap draw count.
        holes: stretches of tape the lake is missing. Intents and round trips inside one
            are dropped from both sides before matching and counted separately: the
            recorder's socket and the loop's socket are two connections, and an intent the
            replay could not have seen is a gap in the data rather than a disagreement
            about it. The holes dimension then reports `NOT_COMPARABLE`, because a stretch
            of window with no tape was not compared, which is not the same as compared and
            found to differ.
        tol_ms: how far apart two entry attempts may be and still be one decision.

    Returns:
        One dimension per thing compared. When the two config hashes disagree the report
        holds only the config dimension: every number below it would be a comparison
        between two different strategies, which is worse than no number.
    """
    config = _config_dimension(live, replay)
    if config.verdict is Verdict.DRIFT:
        return DriftReport(dimensions=[config])

    live_items, replay_items = _itemised(live.intents), _itemised(replay.intents)
    kept_live = [i for i in live_items if not any(h.holds(i.coin, i.ts_ms) for h in holes)]
    kept_replay = [i for i in replay_items if not any(h.holds(i.coin, i.ts_ms) for h in holes)]
    excluded = (len(live_items) - len(kept_live)) + (len(replay_items) - len(kept_replay))
    matched = match_intents(kept_live, kept_replay, tol_ms=tol_ms)
    trips = match_intents(
        [t for t in live.round_trips if not any(h.holds(t.coin, t.ts_ms) for h in holes)],
        [t for t in replay.round_trips if not any(h.holds(t.coin, t.ts_ms) for h in holes)],
        tol_ms=tol_ms,
    )

    return DriftReport(
        dimensions=[
            config,
            _intents_dimension(matched, live, replay),
            _entries_dimension(matched),
            _exits_dimension(trips),
            _pnl_dimension(live, replay, settings=settings),
            _refusals_dimension(live, replay),
            _holes_dimension(holes, excluded),
        ],
        excluded_by_hole=excluded,
    )


def _config_dimension(live: Side, replay: Side) -> Dimension:
    if live.config_hash is None:
        return Dimension(
            name="config",
            verdict=Verdict.NOT_COMPARABLE,
            detail=[
                "The live run does not report a config hash. A soak started before "
                "`paper_started` carried one cannot be shown to have run this config, and "
                "an absent hash is not a matching hash.",
                f"replay config_hash: {replay.config_hash}",
            ],
        )
    if live.config_hash != replay.config_hash:
        return Dimension(
            name="config",
            verdict=Verdict.DRIFT,
            detail=[
                "The two sides ran different config. Nothing below this line is a "
                "comparison between the same strategy, so nothing below it is reported.",
                f"live   config_hash: {live.config_hash}",
                f"replay config_hash: {replay.config_hash}",
            ],
        )
    return Dimension(
        name="config",
        verdict=Verdict.MATCH,
        detail=[f"both sides: {replay.config_hash}"],
    )


def _share(part: int, whole: int) -> float:
    """The share of `whole` that `part` covers. An empty whole is fully covered."""
    return part / whole if whole else 1.0


def _itemised(intents: Sequence[IntentRef]) -> list[IntentRef]:
    """The intents both sides can list one by one, which is what the matcher may see.

    A refusal is always listed. A placed entry is listed once it filled; one that was
    placed and abandoned is not, because the replay keeps a tally of those and not a log,
    and matching a list against a tally reports every entry on the listed side as
    unmatched. The two tallies are compared as numbers instead.
    """
    return [i for i in intents if i.stage or i.entry_px is not None]


def _intents_dimension(matched: Match[IntentRef], live: Side, replay: Side) -> Dimension:
    live_total = len(matched.matched) + len(matched.live_only)
    replay_total = len(matched.matched) + len(matched.replay_only)
    live_share = _share(len(matched.matched), live_total)
    replay_share = _share(len(matched.matched), replay_total)
    metrics = [
        Metric(
            name="live intents matched",
            value=live_share,
            threshold=MATCHED_SHARE_MIN,
            units="share",
            ok=live_share >= MATCHED_SHARE_MIN,
        ),
        Metric(
            name="replay intents matched",
            value=replay_share,
            threshold=MATCHED_SHARE_MIN,
            units="share",
            ok=replay_share >= MATCHED_SHARE_MIN,
        ),
    ]
    # A matched pair where one side placed the order and the other refused it is the two
    # sides disagreeing about whether to trade at all, which is the most consequential
    # drift there is. It used to fall between the dimensions: the pair matched, so intents
    # agreed, and the refused half had no fill price, so entries reported NOT_COMPARABLE
    # and nothing said anything. As a share rather than a count, because one disagreement
    # out of one is a different fact from two out of fifty.
    agreed = sum(1 for left, right in matched.matched if bool(left.stage) == bool(right.stage))
    traded_share = _share(agreed, len(matched.matched))
    metrics.append(
        Metric(
            name="matched intents agreeing on whether to trade",
            value=traded_share,
            threshold=MATCHED_SHARE_MIN,
            units="share",
            ok=traded_share >= MATCHED_SHARE_MIN,
        )
    )
    abandoned_gap = abs(live.abandoned_entries - replay.abandoned_entries)
    abandoned_allowance = max(
        REFUSAL_FLOOR, REFUSAL_FRAC * max(live.abandoned_entries, replay.abandoned_entries)
    )
    metrics.append(
        Metric(
            name="abandoned entries gap",
            value=float(abandoned_gap),
            threshold=abandoned_allowance,
            units="entries",
            ok=abandoned_gap <= abandoned_allowance,
        )
    )
    disagreed = [
        f"{left.coin} {left.side} at {left.ts_ms}: "
        f"live {left.reason or 'traded'}, replay {right.reason or 'traded'}"
        for left, right in matched.matched
        if bool(left.stage) != bool(right.stage)
    ]
    detail = [
        f"{len(matched.matched)} matched, {live_total} live, {replay_total} replay",
        *disagreed[:20],
        f"entries placed and never filled: live {live.abandoned_entries}, "
        f"replay {replay.abandoned_entries}. Counted rather than matched: the engine "
        f"tallies an abandoned entry and does not log one.",
    ]
    if not live_total and not replay_total:
        detail.append("no intents on either side in this window; nothing disagreed")
    detail += [f"live only:   {i.coin} {i.side} at {i.ts_ms}" for i in matched.live_only[:20]]
    detail += [f"replay only: {i.coin} {i.side} at {i.ts_ms}" for i in matched.replay_only[:20]]
    return Dimension(
        name="intents",
        verdict=Verdict.MATCH if all(m.ok for m in metrics) else Verdict.DRIFT,
        metrics=metrics,
        detail=detail,
    )


def _entries_dimension(matched: Match[IntentRef]) -> Dimension:
    both = [
        (left.entry_px, right.entry_px)
        for left, right in matched.matched
        if left.entry_px is not None and right.entry_px is not None
    ]
    if not both:
        return Dimension(
            name="entries",
            verdict=Verdict.NOT_COMPARABLE,
            detail=[
                "No matched intent became a filled order on both sides. An entry price "
                "the live side never recorded is absent, not zero, and is not diffed "
                "against a default."
            ],
        )
    gaps = [_bps(left, right) for left, right in both]
    value = _median(gaps)
    return Dimension(
        name="entries",
        verdict=Verdict.MATCH if value <= PRICE_BPS_MAX else Verdict.DRIFT,
        metrics=[
            Metric(
                name="median entry gap",
                value=value,
                threshold=PRICE_BPS_MAX,
                units="bps",
                ok=value <= PRICE_BPS_MAX,
            )
        ],
        detail=[f"{len(both)} matched intents filled on both sides"],
    )


def _exits_dimension(trips: Match[RoundTrip]) -> Dimension:
    if not trips.matched:
        return Dimension(
            name="exits",
            verdict=Verdict.NOT_COMPARABLE,
            detail=["No round trip matched on both sides."],
        )
    agreed = sum(1 for left, right in trips.matched if left.exit_reason == right.exit_reason)
    share = _share(agreed, len(trips.matched))
    gap = _median([_bps(left.exit_px, right.exit_px) for left, right in trips.matched])
    metrics = [
        Metric(
            name="exit reasons agreeing",
            value=share,
            threshold=EXIT_REASON_SHARE_MIN,
            units="share",
            ok=share >= EXIT_REASON_SHARE_MIN,
        ),
        Metric(
            name="median exit gap",
            value=gap,
            threshold=PRICE_BPS_MAX,
            units="bps",
            ok=gap <= PRICE_BPS_MAX,
        ),
    ]
    disagreed = [
        f"{left.coin} {left.side} at {left.ts_ms}: live {left.exit_reason}, "
        f"replay {right.exit_reason}"
        for left, right in trips.matched
        if left.exit_reason != right.exit_reason
    ]
    return Dimension(
        name="exits",
        verdict=Verdict.MATCH if all(m.ok for m in metrics) else Verdict.DRIFT,
        metrics=metrics,
        detail=[f"{len(trips.matched)} matched round trips", *disagreed[:20]],
    )


def _pnl_dimension(live: Side, replay: Side, *, settings: Settings) -> Dimension:
    if not replay.round_trips:
        return Dimension(
            name="pnl",
            verdict=Verdict.NOT_COMPARABLE,
            detail=["The replay completed no round trip, so there is nothing to interval."],
        )
    gap = abs(sum(t.net_pnl for t in live.round_trips) - sum(t.net_pnl for t in replay.round_trips))
    count = max(len(live.round_trips), len(replay.round_trips))
    notional = _median([t.notional for t in replay.round_trips])
    # Two allowances, and the wider wins. The first is what a round trip of taker fees
    # costs: two sides filling at different prices inside one spread cannot differ by less
    # than the costs of getting in and out. The second is the sampling noise in the
    # replay's own mean, converted from bps of notional to dollars over the same trades,
    # because a difference smaller than the replay's own confidence interval is not a
    # difference the data can see.
    fee_allowance = count * 2 * fee_for(notional, maker=False, settings=settings)
    interval = bootstrap_mean_bps(
        cast("Sequence[Trade]", replay.round_trips),
        draws=settings.backtest.bootstrap_samples,
        seed=0,
    )
    noise_allowance = count * (interval.hi - interval.lo) / 1e4 * notional
    allowance = max(fee_allowance, noise_allowance)
    return Dimension(
        name="pnl",
        verdict=Verdict.MATCH if gap <= allowance else Verdict.DRIFT,
        metrics=[
            Metric(
                name="net PnL gap",
                value=gap,
                threshold=allowance,
                units="USD",
                ok=gap <= allowance,
            )
        ],
        detail=[
            f"live {sum(t.net_pnl for t in live.round_trips):,.2f} against "
            f"replay {sum(t.net_pnl for t in replay.round_trips):,.2f} over {count} trades",
            f"allowance is the wider of {fee_allowance:,.2f} (a taker round trip each on "
            f"the median notional {notional:,.0f}) and {noise_allowance:,.2f} (the "
            f"replay's own bootstrap interval, {interval.lo:.1f} to {interval.hi:.1f} bps)",
        ],
    )


def _refusals_dimension(live: Side, replay: Side) -> Dimension:
    if not live.refusals:
        return Dimension(
            name="refusals",
            verdict=Verdict.NOT_COMPARABLE,
            detail=[
                "The live refusals table is empty across this window. A soak started "
                "before the table existed recorded none, which is not the same fact as a "
                "week in which nothing was refused, and the two are not told apart here."
            ],
        )
    off: list[str] = []
    for key in sorted(live.refusals.keys() | replay.refusals.keys()):
        left, right = live.refusals.get(key, 0), replay.refusals.get(key, 0)
        allowance = max(REFUSAL_FLOOR, REFUSAL_FRAC * max(left, right))
        if abs(left - right) > allowance:
            off.append(f"{key[0]}/{key[1]}: live {left}, replay {right} (allowed ±{allowance:.0f})")
    return Dimension(
        name="refusals",
        verdict=Verdict.MATCH if not off else Verdict.DRIFT,
        metrics=[
            Metric(
                name="reasons outside their allowance",
                value=float(len(off)),
                threshold=0.0,
                units="reasons",
                ok=not off,
            )
        ],
        detail=[
            f"{len(live.refusals.keys() | replay.refusals.keys())} reasons compared, "
            f"each allowed {REFUSAL_FRAC:.0%} or ±{REFUSAL_FLOOR:.0f}, whichever is larger",
            "Day one risk refusals may legitimately differ: the replay opens a clean "
            "RiskState and the soak carries the one its previous days left it.",
            *off,
        ],
    )


def _holes_dimension(holes: Sequence[Hole], excluded: int) -> Dimension:
    if not holes:
        return Dimension(
            name="holes",
            verdict=Verdict.MATCH,
            metrics=[Metric(name="tape holes", value=0.0, threshold=0.0, units="holes", ok=True)],
            detail=["No gap over the recorder's own bar on any coin-day in the window."],
        )
    return Dimension(
        name="holes",
        verdict=Verdict.NOT_COMPARABLE,
        metrics=[
            Metric(
                name="tape holes",
                value=float(len(holes)),
                threshold=0.0,
                units="holes",
                ok=False,
            )
        ],
        detail=[
            f"{excluded} intents fell inside a hole and were left out of the intents "
            f"verdict rather than counted against it.",
            *(
                f"{h.coin}: {datetime.fromtimestamp(h.start_ms / 1000, UTC):%Y-%m-%d %H:%M:%S}"
                f" for {(h.end_ms - h.start_ms) / 1000:.0f}s"
                for h in sorted(holes, key=lambda h: (h.coin, h.start_ms))[:40]
            ),
        ],
    )


# --- the live side --------------------------------------------------------------------


async def read_live(
    store: OrderStore, start_ms: int, end_ms: int, *, config_hash: str | None = None
) -> Side:
    """What the running loop did over `[start_ms, end_ms)`, out of its own registry.

    An entry attempt is an `orders` row tagged `entry` **plus** every `refusals` row: the
    replay counts an intent the moment the strategy emits one, and the live side only has
    an order for the ones that survived sizing and risk. Leaving the refusals out would
    make every gate the loop fired look like an intent the live side never had.

    Round trips are reconstructed by pairing each filled entry with the first filled
    reduce-only order on the same coin at or after it. The engine holds at most one
    position per coin, so that pairing is the one that happened. A resting bracket leg
    that never filled closes nothing, which is why the pairing is on fills rather than on
    submissions: both legs are submitted, one of them fills.

    Args:
        store: the order registry, already open. `liqfade sync` pulls the soak's copy.
        start_ms: window start, inclusive. An order is placed in the window by when it
            filled, or by its submission when it never did; a refusal by its own `ts_ms`.
        end_ms: window end, exclusive.
        config_hash: what the live run reported, when it reported one. None means the run
            did not say, which the config dimension reports as not comparable rather than
            as agreement.

    Returns:
        The live half of the comparison.
    """
    filled_at: dict[str, int] = {}
    for row in await store.fills():
        cloid = row["cloid"]
        filled_at[cloid] = max(filled_at.get(cloid, 0), int(row["ts_ms"]))

    def when(order: Order) -> int:
        """The clock the replay is on: an order's fill time, or its submission if it never
        filled.

        Not `submitted_ms` for a filled order. The replay times an entry by when it
        filled (`Trade.entry_ms`), and an `Alo` rests at the touch before it trades, so
        matching submission against fill would put every maker entry its own
        time-to-fill away from its replay twin and report drift on all of them. The
        registry's fills carry the tick clock, which is the clock the replay is on.
        """
        return filled_at.get(order.cloid, order.submitted_ms)

    orders = [o for o in await store.all_orders() if start_ms <= when(o) < end_ms]
    intents = [
        IntentRef(
            coin=order.coin,
            side="long" if order.intent.is_buy else "short",
            ts_ms=when(order),
            entry_px=order.avg_px if order.filled > 0 else None,
        )
        for order in orders
        if order.intent.tag == "entry"
    ]
    refusals: dict[tuple[str, str], int] = {}
    for row in await store.refusals(start_ms, end_ms):
        key = (row["stage"], row["reason"])
        refusals[key] = refusals.get(key, 0) + 1
        intents.append(
            IntentRef(
                coin=row["coin"],
                side=row["side"],
                ts_ms=row["ts_ms"],
                stage=row["stage"],
                reason=row["reason"],
            )
        )
    intents.sort(key=lambda i: (i.ts_ms, i.coin))
    return Side(
        intents=intents,
        round_trips=_round_trips(orders, filled_at),
        refusals=refusals,
        config_hash=config_hash,
        abandoned_entries=sum(1 for o in orders if o.intent.tag == "entry" and o.filled == 0),
    )


def _round_trips(orders: Sequence[Order], filled_at: Mapping[str, int]) -> list[RoundTrip]:
    """Pair each filled entry with the filled exit that closed it, per coin, in order."""
    trips: list[RoundTrip] = []
    by_coin: dict[str, list[Order]] = {}
    for order in orders:
        if order.filled > 0:
            by_coin.setdefault(order.coin, []).append(order)
    for coin, coin_orders in by_coin.items():
        coin_orders.sort(key=lambda o: filled_at.get(o.cloid, o.submitted_ms))
        exits = [o for o in coin_orders if o.intent.reduce_only]
        used: set[str] = set()
        for entry in (o for o in coin_orders if o.intent.tag == "entry"):
            opened = filled_at.get(entry.cloid, entry.submitted_ms)
            closer = next(
                (
                    o
                    for o in exits
                    if o.cloid not in used and filled_at.get(o.cloid, o.submitted_ms) >= opened
                ),
                None,
            )
            if closer is None:
                continue
            used.add(closer.cloid)
            # The closed size is whichever leg filled less: a partial exit leaves the rest
            # of the position open, and its PnL is not realised yet. Fees are prorated onto
            # the closed part for the same reason.
            size = min(entry.filled, closer.filled)
            direction = 1.0 if entry.intent.is_buy else -1.0
            gross = (closer.avg_px - entry.avg_px) * size * direction
            fees = entry.fees * size / entry.filled + closer.fees * size / closer.filled
            trips.append(
                RoundTrip(
                    coin=coin,
                    side="long" if entry.intent.is_buy else "short",
                    ts_ms=opened,
                    exit_ms=filled_at.get(closer.cloid, closer.submitted_ms),
                    exit_reason=closer.intent.tag,
                    entry_px=entry.avg_px,
                    exit_px=closer.avg_px,
                    net_pnl=gross - fees,
                    notional=entry.avg_px * size,
                )
            )
    return sorted(trips, key=lambda t: (t.ts_ms, t.coin))


# --- the replay side ------------------------------------------------------------------


def read_replay(result: BacktestResult, *, settings: Settings) -> Side:
    """What the replay did, out of what `run_backtest` returned.

    The refusal counts come from the run's **counters** rather than from `result.refused`,
    which is a bounded deque a week of replay would overflow. The counters are exact, and
    counts are all the refusals dimension compares.

    The itemised intents are the entries that filled plus the refusals that are itemised.
    An entry the engine placed and later abandoned past `max_reprices` is counted rather
    than listed, because the engine keeps a tally of those and not a log; `compare` holds
    the two tallies against each other instead of listing one side's and reporting every
    one of them as unmatched.
    """
    intents = [
        IntentRef(
            coin=trade.coin,
            side=trade.side,
            ts_ms=trade.entry_ms,
            entry_px=trade.entry_px,
        )
        for trade in result.trades
    ]
    intents += [
        IntentRef(
            coin=r.coin,
            side=r.side,
            ts_ms=r.ts_ms,
            stage=r.stage,
            reason=r.reason,
        )
        for r in (*result.rejections, *result.refused)
    ]
    intents.sort(key=lambda i: (i.ts_ms, i.coin))

    refusals: dict[tuple[str, str], int] = {}
    for stage, counts in (
        ("sizing", result.sizing_blocks),
        ("risk", result.risk_blocks),
        ("filter", result.strategy.stats.filters),
    ):
        for reason, count in counts.items():
            if count:
                refusals[stage, reason] = count
    for name, rule in result.strategy.stats.rules.items():
        if rule.killed:
            refusals["rule", name] = rule.killed

    return Side(
        intents=intents,
        round_trips=[
            RoundTrip(
                coin=trade.coin,
                side=trade.side,
                ts_ms=trade.entry_ms,
                exit_ms=trade.exit_ms,
                exit_reason=trade.exit_reason,
                entry_px=trade.entry_px,
                exit_px=trade.exit_px,
                net_pnl=trade.net_pnl,
                notional=trade.size * trade.entry_px,
            )
            for trade in result.trades
        ],
        refusals=refusals,
        config_hash=config_hash(settings),
        abandoned_entries=result.portfolio.abandoned_entries,
    )


# --- the tape the comparison stands on ------------------------------------------------


def find_holes(
    data_dir: Path, *, coins: Sequence[str], start_ms: int, end_ms: int, settings: Settings
) -> list[Hole]:
    """Every stretch of one coin's Hyperliquid trade tape the lake does not have.

    A gap longer than `recorder.trades_gap_s` is the recorder's own bar for a stream
    having gone quiet, so it is the bar used here: below it the coin was merely not
    trading, and above it the replay is deciding on tape the live loop had and it does not.

    One UTC day of one table at a time, through `load_day`, because that is the only
    reader in this package and the reason it is the only one is that a range read of a
    year killed the machine twice.
    """
    gap_ms = int(settings.recorder.trades_gap_s * 1000)
    holes: list[Hole] = []
    for day in utc_days(start_ms, end_ms):
        frame = load_day(data_dir, "trades", day, coins=list(coins), venue="hyperliquid")
        if frame.is_empty():
            continue
        for coin in coins:
            stamps = (
                frame.filter(pl.col("coin") == coin)
                .select(effective_ts())
                .to_series()
                .sort()
                .to_list()
            )
            window = [t for t in stamps if start_ms <= t < end_ms]
            if not window:
                continue
            for earlier, later in pairwise(window):
                if later - earlier > gap_ms:
                    holes.append(Hole(coin=coin, start_ms=earlier, end_ms=later))
    return sorted(holes, key=lambda h: (h.coin, h.start_ms))


# --- the report -----------------------------------------------------------------------


def _fmt(metric: Metric) -> str:
    if metric.units == "share":
        return f"{metric.value:.0%} (bar: {metric.threshold:.0%})"
    if metric.units == "USD":
        return f"${metric.value:,.2f} (allowance: ${metric.threshold:,.2f})"
    return f"{metric.value:,.2f} {metric.units} (bar: {metric.threshold:,.2f})"


def report_payload(
    report: DriftReport, *, start: str, end: str, coins: Sequence[str]
) -> dict[str, object]:
    """The same verdicts as JSON, for anything that wants to read them back."""
    return {
        "start": start,
        "end": end,
        "coins": list(coins),
        "verdict": str(report.verdict),
        "excluded_by_hole": report.excluded_by_hole,
        "dimensions": [
            {
                "name": d.name,
                "verdict": str(d.verdict),
                "metrics": [
                    {
                        "name": m.name,
                        "value": m.value,
                        "threshold": m.threshold,
                        "units": m.units,
                        "ok": m.ok,
                    }
                    for m in d.metrics
                ],
                "detail": d.detail,
            }
            for d in report.dimensions
        ],
    }


def render_report(
    report: DriftReport,
    *,
    start: str,
    end: str,
    coins: Sequence[str],
    equity: float,
    db_path: Path,
    settings: Settings,
) -> str:
    """One page: the verdict per dimension, then what each one measured."""
    lines = [
        f"# Live versus replay, {start} to {end}",
        "",
        f"**{report.verdict}**",
        "",
        "The Phase 5 definition of done asks whether paper lands in a plausible band of "
        "the backtest for the same days. This is that question with a number per "
        "dimension. The live side is the paper loop's own order registry; the replay side "
        "is `run_backtest` over the recorder's lake for the same days, under the same "
        "resolved config and the same starting equity.",
        "",
        "`NOT_COMPARABLE` is not a near miss and not a quiet pass. It means the live side "
        "does not persist what the replay computes, so there is nothing to diff. A "
        "comparator that defaults the missing half to zero and diffs against the default "
        "reports agreement it has not measured.",
        "",
        f"- coins: {', '.join(coins)}. The soak's universe is dynamic and is not persisted "
        "per day, so coverage beyond these coins is unknown rather than assumed equal.",
        f"- equity: {equity:,.0f}",
        f"- registry: `{db_path}`",
        f"- replay config_hash: `{config_hash(settings)}`",
        "",
        "| dimension | verdict | measured |",
        "| --- | --- | --- |",
    ]
    for dim in report.dimensions:
        measured = "; ".join(_fmt(m) for m in dim.metrics) or "-"
        lines.append(f"| {dim.name} | {dim.verdict} | {measured} |")
    lines.append("")
    for dim in report.dimensions:
        lines += [f"## {dim.name}: {dim.verdict}", ""]
        for metric in dim.metrics:
            lines.append(f"- {metric.name}: {_fmt(metric)}")
        if dim.metrics:
            lines.append("")
        lines += list(dim.detail)
        lines.append("")
    return "\n".join(lines) + "\n"
