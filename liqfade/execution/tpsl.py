"""Take profit and stop loss attached to a position as one `positionTpsl` group.

Every fact here was measured on testnet 2026-09-08, not read from a doc. See
`docs/HYPERLIQUID.md` probe 3.

**Grouping.** `bulk_orders(requests, grouping="positionTpsl")` answers
`{"statuses": ["waitingForTrigger", "waitingForTrigger"]}`. There is no oid to hold on to,
which is the first reason the cloid is the identity.

**`isPositionTpsl` is false on both legs**, despite the grouping that created them. It
reads like the flag that finds a bracket and it does not, so anything filtering on it
finds nothing and concludes the bracket is gone. The cloid is the second reason, and the
only one that works.

**The legs live in `frontendOpenOrders`, not `openOrders`.** A reconciliation that reads
only `openOrders` sees a position with no protection on it and would be right to panic.

**They die with the position.** Closing it removed both within 2 seconds with no cancel of
ours. So the manager reconciles them away rather than cancelling: sending a cancel for an
order the venue has already removed spends an action from the budget to be told it is
gone, and on a busy exit that budget is what the kill path needs.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = ["TpslLegs", "tpsl_requests"]


def tpsl_requests(
    *,
    coin: str,
    is_long: bool,
    size: float,
    tp_px: float,
    sl_px: float,
    tp_cloid: str,
    sl_cloid: str,
) -> list[dict[str, Any]]:
    """The two order requests for one position's bracket, ready for `bulk_orders`.

    Args:
        is_long: the position's direction. Both legs face it: a long exits by selling,
            so a leg that could increase the position is not an exit and is refused by
            the venue as reduce-only anyway.
        size: the whole position. A bracket for part of one leaves the rest unprotected.
        tp_px: the take profit trigger.
        sl_px: the stop trigger.

    Note:
        `isMarket` is true on both. A trigger that rests as a limit can be jumped by a gap
        and leave a stop that never fills, which is the failure mode a stop exists to
        prevent.
    """
    exit_is_buy = not is_long
    return [
        {
            "coin": coin,
            "is_buy": exit_is_buy,
            "sz": size,
            "limit_px": px,
            "order_type": {"trigger": {"triggerPx": px, "isMarket": True, "tpsl": kind}},
            "reduce_only": True,
            "cloid": cloid,
        }
        for kind, px, cloid in (("tp", tp_px, tp_cloid), ("sl", sl_px, sl_cloid))
    ]


@dataclass(frozen=True, slots=True)
class TpslLegs:
    """The two cloids of one position's bracket. The only handle that works."""

    tp_cloid: str
    sl_cloid: str

    def resting_in(self, trigger_orders: Iterable[Any]) -> set[str]:
        """Which legs the venue still holds, as `{"tp"}`, `{"sl"}`, both or neither."""
        by_cloid = {order.cloid for order in trigger_orders if order.cloid}
        found = set()
        if self.tp_cloid in by_cloid:
            found.add("tp")
        if self.sl_cloid in by_cloid:
            found.add("sl")
        return found

    def gone(self, trigger_orders: Iterable[Any]) -> bool:
        """True when neither leg is resting, which is what a closed position looks like."""
        return not self.resting_in(trigger_orders)

    def half_open(self, trigger_orders: Sequence[Any]) -> bool:
        """One leg left. The position is not protected the way it was meant to be.

        The venue cancels both together when the position closes, so exactly one leg
        resting means the other one fired or was refused, and the caller has to decide
        rather than assume.
        """
        return len(self.resting_in(trigger_orders)) == 1
