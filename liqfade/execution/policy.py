"""The entry policy, as one decision table both the backtest and the live path read.

docs/STRATEGY_AND_RISK.md section 3: rest `Alo` at the touch, give it `entry_ttl_s`, and
if it has not filled, reprice it to the new touch up to `max_reprices` times before
abandoning it. `backtest/engine.py` has run that policy since Phase 4; the live order
manager did not have it at all.

**Why a shared function rather than two implementations and a test.** CLAUDE.md's single
most important design constraint is that strategy code runs against a replay with zero
changes, and the reason is that a divergence here is invisible: the backtest reports a
maker fill rate, the bot achieves a different one, and nothing fails. A test pinning two
implementations together only catches the drift somebody remembered to test for. One
function cannot drift.

Pure, and deliberately so: no clock, no book, no venue. The caller supplies `now_ms` and
the touch, which is what lets the backtest drive it from replayed time and the live loop
from the wall clock.
"""

from __future__ import annotations

from enum import StrEnum

from liqfade.config.models import Settings

__all__ = ["EntryAction", "entry_action", "touch_price"]


class EntryAction(StrEnum):
    """What to do with a resting entry that has been looked at."""

    HOLD = "hold"
    REPRICE = "reprice"
    ABANDON = "abandon"


def entry_action(*, now_ms: int, visible_ms: int, reprices: int, settings: Settings) -> EntryAction:
    """Whether a resting entry should be left alone, moved to the touch, or given up on.

    Args:
        now_ms: decision time. Replayed time in the backtest, wall clock in the live loop.
        visible_ms: when this order became visible at its current price. Reset on every
            reprice, so the TTL is per price and not per intent: an order that has been
            moved is a new order to the queue and gets a fresh look.
        reprices: how many times this entry has already been moved.
        settings: `execution.entry_ttl_s` and `execution.max_reprices`.

    Returns:
        HOLD while the order is younger than its TTL, REPRICE when it has expired and has
        moves left, ABANDON when it has expired and has not.

    Note:
        The TTL boundary is inclusive of expiry: at exactly `visible_ms + ttl` the order
        acts. `max_reprices` counts moves already made, so `max_reprices: 0` abandons at
        the first expiry and never moves an order at all.
    """
    if now_ms < visible_ms + int(settings.execution.entry_ttl_s * 1000):
        return EntryAction.HOLD
    if reprices >= settings.execution.max_reprices:
        return EntryAction.ABANDON
    return EntryAction.REPRICE


def touch_price(*, is_buy: bool, bid: float | None, ask: float | None) -> float | None:
    """The price a post-only order rests at without crossing, or None with no book.

    A buy rests on the bid and a sell on the offer. Crossing the spread is exactly what
    `Alo` refuses, and the venue's refusal is measured in docs/HYPERLIQUID.md: "Post only
    order would have immediately matched". None means hold the order where it is rather
    than reprice it onto an invented price.
    """
    return bid if is_buy else ask
