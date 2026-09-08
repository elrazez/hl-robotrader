"""The WebSocket user channel: fills and order updates pushed, not polled.

Until this existed a fill was learned about on the next reconciliation, up to
`execution.reconcile_s` late, and `OrderManager.record_fill` and `record_reject` were
called by the paper session and by tests only. Every payload below is the shape testnet
actually sent on 2026-09-08; see docs/HYPERLIQUID.md probe 1.
"""

from __future__ import annotations

from pathlib import Path

from liqfade.execution.orders import (
    OrderIntent,
    OrderManager,
    OrderStatus,
    OrderStore,
    new_cloid,
)
from liqfade.execution.userstream import UserEvent, parse_user_frame, subscribe_frames

ADDRESS = "0x000000000000000000000000000000000000beef"


def an_order_update(cloid: str, status: str, *, oid: int = 59610337202) -> dict:
    """Verbatim frame shape from probe 1, with the cloid swapped in."""
    return {
        "channel": "orderUpdates",
        "data": [
            {
                "order": {
                    "coin": "ETH",
                    "side": "B",
                    "limitPx": "1239.3",
                    "sz": "0.0096",
                    "oid": oid,
                    "timestamp": 1788859194749,
                    "origSz": "0.0096",
                    "cloid": cloid,
                },
                "status": status,
                "statusTimestamp": 1788859195525,
            }
        ],
    }


def test_both_channels_are_subscribed_for_the_account() -> None:
    frames = subscribe_frames(ADDRESS)
    kinds = {frame["subscription"]["type"] for frame in frames}
    assert kinds == {"orderUpdates", "userFills"}
    assert all(frame["subscription"]["user"] == ADDRESS for frame in frames)
    assert all(frame["method"] == "subscribe" for frame in frames)


def test_a_subscription_response_carries_no_events() -> None:
    """The venue echoes the subscription back. It is not an order doing anything."""
    assert parse_user_frame({"channel": "subscriptionResponse", "data": {}}) == []


def test_an_order_update_is_keyed_on_the_cloid_not_the_oid() -> None:
    """Every orderUpdates frame carries the cloid, measured. So the oid is never needed.

    It matters because a reprice answers with a new oid: a client keyed on oid would lose
    an order the moment the entry policy moved it.
    """
    (event,) = parse_user_frame(an_order_update("0xabc", "open"))
    assert event.cloid == "0xabc"
    assert event.kind == "open"
    assert event.oid == 59610337202
    assert event.coin == "ETH"
    assert event.px == 1239.3
    assert event.ts_ms == 1788859195525


def test_a_cancel_and_a_fill_are_distinct_kinds() -> None:
    assert parse_user_frame(an_order_update("0xabc", "canceled"))[0].kind == "canceled"
    assert parse_user_frame(an_order_update("0xabc", "filled"))[0].kind == "filled"


def test_an_order_update_without_a_cloid_is_dropped() -> None:
    """Somebody else's order, or one placed before this process existed. Not ours to move."""
    frame = an_order_update("0xabc", "open")
    del frame["data"][0]["order"]["cloid"]
    assert parse_user_frame(frame) == []


def test_a_fill_frame_carries_what_the_registry_books() -> None:
    frame = {
        "channel": "userFills",
        "data": {
            "isSnapshot": False,
            "fills": [
                {
                    "coin": "ETH",
                    "px": "2479.7",
                    "sz": "0.006",
                    "side": "B",
                    "time": 1788859270950,
                    "oid": 59610407449,
                    "fee": "0.0669",
                    "crossed": True,
                    "cloid": "0xdef",
                }
            ],
        },
    }
    (event,) = parse_user_frame(frame)
    assert event.kind == "fill"
    assert event.cloid == "0xdef"
    assert event.px == 2479.7
    assert event.size == 0.006
    assert event.fee == 0.0669
    assert event.maker is False, "crossed means we were the taker"


def test_the_opening_snapshot_is_not_replayed_as_new_fills() -> None:
    """`userFills` pushes a snapshot on subscribe, and on every reconnect.

    Booking those again would double every fill the registry already has, once per
    reconnect. Reconciliation is what settles history; the stream carries what is new.
    """
    frame = {
        "channel": "userFills",
        "data": {
            "isSnapshot": True,
            "fills": [
                {
                    "coin": "ETH",
                    "px": "1",
                    "sz": "1",
                    "side": "B",
                    "time": 1,
                    "oid": 1,
                    "cloid": "0xdef",
                }
            ],
        },
    }
    assert parse_user_frame(frame) == []


async def test_a_pushed_fill_reaches_the_registry(tmp_path: Path) -> None:
    """The point of the whole thing: a fill is known when it happens, not on the interval."""
    from liqfade.execution.userstream import apply_event

    class Transport:
        async def place(self, intent: OrderIntent) -> int:
            return 1

        async def cancel(self, coin: str, cloid: str) -> None: ...
        async def modify(self, intent: OrderIntent) -> int:
            return 2

    store = await OrderStore.open(tmp_path / "orders.sqlite")
    try:
        manager = OrderManager(store, Transport(), sz_decimals={"ETH": 4}, killed=lambda: False)
        cloid = new_cloid()
        await manager.submit(
            OrderIntent(cloid=cloid, coin="ETH", is_buy=True, px=2000.0, size=0.006, tif="Alo")
        )

        await apply_event(
            manager,
            UserEvent(
                kind="fill",
                cloid=cloid,
                oid=1,
                coin="ETH",
                px=2000.0,
                size=0.006,
                fee=0.02,
                maker=True,
                ts_ms=1,
            ),
        )

        order = manager.get(cloid)
        assert order.status is OrderStatus.FILLED
        assert order.filled == 0.006
    finally:
        await store.close()


async def test_an_event_for_an_unknown_cloid_is_ignored(tmp_path: Path) -> None:
    """An order from a previous process, or another tool on the same account.

    Raising here would take the receive loop down over somebody else's order.
    """
    from liqfade.execution.userstream import apply_event

    class Transport:
        async def place(self, intent: OrderIntent) -> int:
            return 1

        async def cancel(self, coin: str, cloid: str) -> None: ...
        async def modify(self, intent: OrderIntent) -> int:
            return 2

    store = await OrderStore.open(tmp_path / "orders.sqlite")
    try:
        manager = OrderManager(store, Transport(), sz_decimals={"ETH": 4}, killed=lambda: False)
        await apply_event(manager, UserEvent(kind="canceled", cloid="0xnope", oid=9, coin="ETH"))
    finally:
        await store.close()


async def test_the_stream_subscribes_on_connect_and_reconciles_on_reconnect(
    tmp_path: Path,
) -> None:
    """CLAUDE.md rule 4: every reconnect compares the books, because a socket that was
    away missed fills. The stream carries what is new; it never replaces that check.
    """
    from liqfade.config import load_settings
    from liqfade.execution.userstream import UserStream

    class Transport:
        async def place(self, intent: OrderIntent) -> int:
            return 1

        async def cancel(self, coin: str, cloid: str) -> None: ...
        async def modify(self, intent: OrderIntent) -> int:
            return 2

    reconnects: list[str] = []
    store = await OrderStore.open(tmp_path / "orders.sqlite")
    try:
        manager = OrderManager(store, Transport(), sz_decimals={"ETH": 4}, killed=lambda: False)
        stream = UserStream(
            url="wss://api.hyperliquid-testnet.xyz/ws",
            address=ADDRESS,
            manager=manager,
            cfg=load_settings("paper", environ={}).recorder,
            on_reconnect=reconnects.append,
        )

        # What the socket sends the moment it is up, and what it does when it drops.
        assert stream.stream.on_connect([]) == subscribe_frames(ADDRESS)
        stream.stream.on_reconnect("hyperliquid_user")
        assert reconnects == ["hyperliquid_user"]
    finally:
        await store.close()


async def test_a_frame_off_the_socket_reaches_the_registry(tmp_path: Path) -> None:
    """End to end through the real parse and sink, without a socket."""
    import asyncio

    import orjson

    from liqfade.config import load_settings
    from liqfade.execution.userstream import UserStream

    class Transport:
        async def place(self, intent: OrderIntent) -> int:
            return 1

        async def cancel(self, coin: str, cloid: str) -> None: ...
        async def modify(self, intent: OrderIntent) -> int:
            return 2

    store = await OrderStore.open(tmp_path / "orders.sqlite")
    try:
        manager = OrderManager(store, Transport(), sz_decimals={"ETH": 4}, killed=lambda: False)
        cloid = new_cloid()
        await manager.submit(
            OrderIntent(cloid=cloid, coin="ETH", is_buy=True, px=2000.0, size=0.0096, tif="Alo")
        )
        stream = UserStream(
            url="wss://example.invalid/ws",
            address=ADDRESS,
            manager=manager,
            cfg=load_settings("paper", environ={}).recorder,
        )

        for table, row in stream._parse(orjson.dumps(an_order_update(cloid, "canceled")), 0):
            stream._sink(table, "hyperliquid_user", row)

        stop = asyncio.Event()
        drain = asyncio.create_task(stream.drain(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await drain

        assert manager.get(cloid).status is OrderStatus.CANCELED
        assert stream.applied == 1
    finally:
        await store.close()


async def test_an_open_frame_never_resurrects_a_filled_order(tmp_path: Path) -> None:
    """Drilled on testnet: an order went FILLED and then back to ACKED.

    Hyperliquid sends `open` before the fill, and on a fast Ioc the two can be applied out
    of order. `record_ack` on a terminal order puts a filled entry back in the live set,
    where reconciliation reads it as an order the venue has lost and the entry policy may
    try to reprice it. A fill is terminal; an `open` that arrives afterwards is old news.
    """
    from liqfade.execution.userstream import apply_event

    class Transport:
        async def place(self, intent: OrderIntent) -> int:
            return 1

        async def cancel(self, coin: str, cloid: str) -> None: ...
        async def modify(self, intent: OrderIntent) -> int:
            return 2

    store = await OrderStore.open(tmp_path / "orders.sqlite")
    try:
        manager = OrderManager(store, Transport(), sz_decimals={"ETH": 4}, killed=lambda: False)
        cloid = new_cloid()
        await manager.submit(
            OrderIntent(cloid=cloid, coin="ETH", is_buy=True, px=2000.0, size=0.01, tif="Ioc")
        )
        await apply_event(
            manager,
            UserEvent(kind="fill", cloid=cloid, oid=1, coin="ETH", px=2000.0, size=0.01, ts_ms=1),
        )
        assert manager.get(cloid).status is OrderStatus.FILLED

        await apply_event(manager, UserEvent(kind="open", cloid=cloid, oid=99, coin="ETH"))

        assert manager.get(cloid).status is OrderStatus.FILLED, "a fill is terminal"
        assert await manager.open_orders() == []
    finally:
        await store.close()
