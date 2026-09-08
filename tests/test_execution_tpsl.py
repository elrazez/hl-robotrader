"""TP/SL attached to a position as one `positionTpsl` group.

Measured on testnet 2026-09-08 (docs/HYPERLIQUID.md probe 3), and every fact this file
asserts came from that run rather than from the docs:

- `bulk_orders(..., grouping="positionTpsl")` answers `["waitingForTrigger", ...]`.
- The legs appear in `frontendOpenOrders` and **not** in `openOrders`.
- `isPositionTpsl` reads **false** on both legs despite the grouping, so it cannot be
  used to find them. The cloid is the only identity that works.
- Closing the position removed both legs within 2 seconds with no cancel of ours.
"""

from __future__ import annotations

import pytest

from liqfade.execution.hl import classify_group_response, parse_trigger_orders
from liqfade.execution.tpsl import TpslLegs, tpsl_requests

FRONTEND_ROWS = [
    {
        "coin": "ETH",
        "side": "A",
        "limitPx": "2466.1",
        "sz": "0.006",
        "oid": 59610408262,
        "timestamp": 1788859270950,
        "triggerCondition": "Price below 2466.1",
        "isTrigger": True,
        "triggerPx": "2466.1",
        "children": [],
        "isPositionTpsl": False,
        "reduceOnly": True,
        "orderType": "Stop Market",
        "origSz": "0.006",
        "tif": None,
        "cloid": "0x5dabce5d9dca40f6ab59d40e1d359ced",
    },
    {
        "coin": "ETH",
        "side": "A",
        "limitPx": "2490.8",
        "sz": "0.006",
        "oid": 59610408261,
        "timestamp": 1788859270950,
        "triggerCondition": "Price above 2490.8",
        "isTrigger": True,
        "triggerPx": "2490.8",
        "children": [],
        "isPositionTpsl": False,
        "reduceOnly": True,
        "orderType": "Take Profit Market",
        "origSz": "0.006",
        "tif": None,
        "cloid": "0x961c9f63d9c148e5ac3ccb0b4fc5ade6",
    },
]


def test_both_legs_are_reduce_only_and_face_the_position() -> None:
    """A long exits by selling. A leg that could increase the position is not an exit."""
    legs = tpsl_requests(
        coin="ETH",
        is_long=True,
        size=0.006,
        tp_px=2490.8,
        sl_px=2466.1,
        tp_cloid="0xtp",
        sl_cloid="0xsl",
    )
    assert len(legs) == 2
    assert all(leg["reduce_only"] for leg in legs)
    assert all(leg["is_buy"] is False for leg in legs), "a long exits by selling"
    assert {leg["order_type"]["trigger"]["tpsl"] for leg in legs} == {"tp", "sl"}


def test_a_short_exits_by_buying_and_its_triggers_invert() -> None:
    legs = tpsl_requests(
        coin="ETH",
        is_long=False,
        size=0.006,
        tp_px=2400.0,
        sl_px=2520.0,
        tp_cloid="0xtp",
        sl_cloid="0xsl",
    )
    assert all(leg["is_buy"] is True for leg in legs)
    by_kind = {leg["order_type"]["trigger"]["tpsl"]: leg for leg in legs}
    assert by_kind["tp"]["order_type"]["trigger"]["triggerPx"] == 2400.0
    assert by_kind["sl"]["order_type"]["trigger"]["triggerPx"] == 2520.0


def test_the_group_response_is_waiting_for_trigger_not_an_oid() -> None:
    """A trigger order rests without an oid to hold on to, which is why cloid is the key."""
    response = {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {"statuses": ["waitingForTrigger", "waitingForTrigger"]},
        },
    }
    assert classify_group_response(response) == ["waitingForTrigger", "waitingForTrigger"]


def test_a_refused_leg_is_raised_not_swallowed() -> None:
    """One leg refused means the position has half a bracket, which is worse than none."""
    from liqfade.execution.orders import RejectError

    response = {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {
                "statuses": ["waitingForTrigger", {"error": "Order must have minimum value of $10"}]
            },
        },
    }
    with pytest.raises(RejectError, match="minimum value"):
        classify_group_response(response)


def test_the_legs_are_found_by_cloid_because_is_position_tpsl_lies() -> None:
    """Measured: `isPositionTpsl` is false on both legs of a positionTpsl group.

    Anything filtering on that flag finds nothing and concludes the bracket is gone.
    """
    assert all(row["isPositionTpsl"] is False for row in FRONTEND_ROWS)

    legs = TpslLegs(
        tp_cloid="0x961c9f63d9c148e5ac3ccb0b4fc5ade6", sl_cloid="0x5dabce5d9dca40f6ab59d40e1d359ced"
    )
    resting = parse_trigger_orders(FRONTEND_ROWS)

    assert legs.resting_in(resting) == {"tp", "sl"}


def test_a_position_that_closed_leaves_no_legs_to_reconcile() -> None:
    """The venue cancels both within 2s of the position closing, measured.

    So the manager reconciles them away rather than cancelling: sending a cancel for an
    order the venue has already removed spends an action from the budget to be told the
    order is gone.
    """
    legs = TpslLegs(tp_cloid="0xtp", sl_cloid="0xsl")
    assert legs.resting_in(parse_trigger_orders([])) == set()
    assert legs.gone(parse_trigger_orders([])) is True


def test_trigger_orders_carry_their_trigger_price_and_direction() -> None:
    parsed = {order.cloid: order for order in parse_trigger_orders(FRONTEND_ROWS)}
    stop = parsed["0x5dabce5d9dca40f6ab59d40e1d359ced"]
    assert stop.trigger_px == 2466.1
    assert stop.is_stop is True
    take = parsed["0x961c9f63d9c148e5ac3ccb0b4fc5ade6"]
    assert take.trigger_px == 2490.8
    assert take.is_stop is False


async def test_a_bracket_is_registered_under_both_cloids_before_it_is_placed(
    tmp_path,
) -> None:
    """Both legs are real orders in the registry, so a restart knows they are out there.

    A trigger order the venue holds and the registry does not is a stop nobody is tracking
    and reconciliation would report as a stranger's order forever.
    """
    from liqfade.execution.orders import OrderIntent, OrderManager, OrderStatus, OrderStore

    placed: list[list[dict]] = []

    class Transport:
        async def place(self, intent: OrderIntent) -> int:
            raise AssertionError("a bracket goes as a group, not as two orders")

        async def cancel(self, coin: str, cloid: str) -> None: ...

        async def modify(self, intent: OrderIntent) -> int:
            raise AssertionError("not reached")

        async def place_group(self, requests: list[dict]) -> list[str]:
            placed.append(requests)
            return ["waitingForTrigger", "waitingForTrigger"]

    store = await OrderStore.open(tmp_path / "orders.sqlite")
    try:
        manager = OrderManager(store, Transport(), sz_decimals={"ETH": 4}, killed=lambda: False)
        legs = await manager.submit_bracket(
            coin="ETH", is_long=True, size=0.006, tp_px=2490.8, sl_px=2466.1
        )

        assert len(placed) == 1 and len(placed[0]) == 2
        tp = manager.get(legs.tp_cloid)
        sl = manager.get(legs.sl_cloid)
        assert tp.status is OrderStatus.ACKED and sl.status is OrderStatus.ACKED
        assert tp.intent.tag == "target" and sl.intent.tag == "stop"
        assert tp.intent.reduce_only and sl.intent.reduce_only
        # Persisted, so a restart finds them.
        assert {order.cloid for order in await store.open_orders()} == {
            legs.tp_cloid,
            legs.sl_cloid,
        }
    finally:
        await store.close()


async def test_a_refused_bracket_leaves_no_half_tracked_orders(tmp_path) -> None:
    """If the venue refuses the group, neither leg is out there and neither is live here.

    And the caller is told: `None` rather than two cloids, so nothing can record a bracket
    the venue never took.
    """
    from liqfade.execution.orders import OrderIntent, OrderManager, OrderStatus, OrderStore

    class Transport:
        async def place(self, intent: OrderIntent) -> int:
            raise AssertionError("not reached")

        async def cancel(self, coin: str, cloid: str) -> None: ...

        async def modify(self, intent: OrderIntent) -> int:
            raise AssertionError("not reached")

        async def place_group(self, requests: list[dict]) -> list[str]:
            from liqfade.execution.orders import RejectError

            raise RejectError("Order must have minimum value of $10")

    store = await OrderStore.open(tmp_path / "orders.sqlite")
    try:
        manager = OrderManager(store, Transport(), sz_decimals={"ETH": 4}, killed=lambda: False)
        legs = await manager.submit_bracket(
            coin="ETH", is_long=True, size=0.006, tp_px=2490.8, sl_px=2466.1
        )

        assert legs is None
        registered = await store.all_orders()
        assert len(registered) == 2
        assert {order.status for order in registered} == {OrderStatus.REJECTED}
        assert await manager.open_orders() == []
    finally:
        await store.close()
