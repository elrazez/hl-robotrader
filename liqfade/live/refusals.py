"""The one drain: what a live tick refused, into the order registry.

`StrategyEngine.take_rejections()` exists so a loop that runs for weeks does not grow a
list forever, and until now nothing in `live/` called it: `_rejections` grew for the life
of the paper process and was written nowhere. The refusals from sizing and risk fared no
better, counted into `recent_blocks` and dropped with the intent that carried the prices.

Both live sessions call this, once per tick, so `pause`, `flat` and `kill` are not the only
things with a single implementation. A session with no store still drains: the point of the
drain is that the list is bounded, and persisting it is the second thing it does.
"""

from __future__ import annotations

from collections import deque

from liqfade.execution.orders import OrderStore
from liqfade.strategy.engine import Rejection, StrategyEngine

__all__ = ["drain"]


async def drain(
    strategy: StrategyEngine, refused: deque[Rejection], store: OrderStore | None
) -> int:
    """Drain this tick's refusals into the registry and return how many there were.

    Args:
        strategy: the engine whose rule and filter rejections are taken and forgotten.
        refused: the session's sizing and risk refusals, emptied here. `BacktestResult`
            and `VenueCounters` both hold one under this name, which is what lets the two
            sessions share this function instead of owning a copy each.
        store: the sqlite registry, or None for a session that persists nothing.

    Almost every tick refuses nothing, so the common path is one list allocation and no
    write at all.
    """
    refusals = strategy.take_rejections()
    refusals.extend(refused)
    refused.clear()
    if store is not None and refusals:
        await store.add_refusals(refusals)
    return len(refusals)
