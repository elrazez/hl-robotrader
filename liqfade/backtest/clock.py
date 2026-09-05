"""Interleave decision ticks with market events, in one ordered stream.

The replay loop has two clocks. Events arrive when they arrive; decisions happen on a fixed
cadence, `features.decision_cadence_ms`. `merge` walks both and yields them in time order,
so the engine sees every event that preceded a decision before it makes it, and never one
that followed.

That ordering rule is the whole no-lookahead guarantee at the loop level. An event stamped
exactly on a tick is delivered **before** the tick, matching the half-open convention in
`features/grid.py`, where a row stamped `T` summarises `[T - step, T)` and was knowable at
`T`. Getting this backwards would let a decision at `T` see the print that happened at `T`,
which live never could.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from liqfade.backtest.data import Event

__all__ = ["Tick", "merge", "tick_times"]


@dataclass(frozen=True, slots=True)
class Tick:
    """A decision point. Carries nothing but its time; the features come from the grid."""

    ts_ms: int


def tick_times(start_ms: int, end_ms: int, cadence_ms: int) -> Iterator[int]:
    """Decision timestamps over `[start_ms, end_ms)`, aligned to the cadence.

    Aligned to an absolute multiple of the cadence rather than to `start_ms`, so two runs
    over overlapping windows put their ticks in the same places. A walk-forward split whose
    ticks moved by a few milliseconds between folds would report differences that were
    about the split rather than about the strategy.
    """
    if cadence_ms <= 0:
        raise ValueError("decision cadence must be positive")
    first = ((start_ms + cadence_ms - 1) // cadence_ms) * cadence_ms
    return iter(range(first, end_ms, cadence_ms))


def merge(events: Iterable[Event], ticks: Iterable[int]) -> Iterator[Event | Tick]:
    """Yield events and decision ticks in time order, events first on a tie.

    Args:
        events: the merged market event stream, already in `effective_ts` order.
        ticks: decision timestamps, ascending.

    Yields:
        `Event` and `Tick` objects. Every event with a timestamp at or before a tick is
        yielded before that tick.
    """
    event_iter = iter(events)
    tick_iter = iter(ticks)
    pending_event = next(event_iter, None)
    pending_tick = next(tick_iter, None)

    while pending_event is not None or pending_tick is not None:
        take_event = pending_tick is None or (
            pending_event is not None and pending_event.ts_ms <= pending_tick
        )
        if take_event and pending_event is not None:
            yield pending_event
            pending_event = next(event_iter, None)
        elif pending_tick is not None:
            yield Tick(pending_tick)
            pending_tick = next(tick_iter, None)
