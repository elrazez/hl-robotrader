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

from dataclasses import dataclass, field
from typing import Protocol

__all__ = ["IntentRef", "Match", "match_intents"]


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
