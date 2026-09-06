"""The order manager: the state machine, the cloid registry, retries and persistence.

The seam is `OrderManager` plus `OrderStore`. Every test drives them through the public
interface with a fake transport, because the point of the transport protocol is that the
same manager runs against Hyperliquid and against the paper fill simulator.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import structlog

from liqfade.execution.orders import (
    OrderIntent,
    OrderManager,
    OrderStatus,
    OrderStore,
    RejectError,
    TransientTransportError,
    new_cloid,
)


class FakeTransport:
    """Records what it was asked to do and answers with whatever the test queued."""

    def __init__(self, *, acks: list[Any] | None = None) -> None:
        self.placed: list[OrderIntent] = []
        self.cancelled: list[tuple[str, str]] = []
        self.acks = acks or []
        self.next_oid = 1000

    async def place(self, intent: OrderIntent) -> int:
        self.placed.append(intent)
        if self.acks:
            outcome = self.acks.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return int(outcome)
        self.next_oid += 1
        return self.next_oid

    async def cancel(self, coin: str, cloid: str) -> None:
        self.cancelled.append((coin, cloid))


@pytest.fixture
async def store(tmp_path: Path):
    store = await OrderStore.open(tmp_path / "orders.sqlite")
    try:
        yield store
    finally:
        await store.close()


def an_intent(**kwargs: Any) -> OrderIntent:
    defaults: dict[str, Any] = {
        "cloid": new_cloid(),
        "coin": "ETH",
        "is_buy": True,
        "px": 3000.0,
        "size": 0.01,
        "tif": "Alo",
        "reduce_only": False,
        "tag": "entry",
    }
    return OrderIntent(**{**defaults, **kwargs})


async def test_a_cloid_is_sixteen_bytes_of_hex() -> None:
    """docs/HYPERLIQUID.md: the `c` field is an optional 16 byte hex, and we always set it."""
    cloid = new_cloid()
    assert cloid.startswith("0x")
    assert len(cloid) == 34
    int(cloid, 16)
    assert new_cloid() != cloid


async def test_submit_places_once_and_lands_acked(store: OrderStore) -> None:
    manager = OrderManager(store, FakeTransport())
    order = await manager.submit(an_intent())

    assert order.status is OrderStatus.ACKED
    assert order.oid is not None
    assert (await store.get(order.cloid)).status is OrderStatus.ACKED


async def test_the_same_cloid_is_never_placed_twice(store: OrderStore) -> None:
    """Idempotency by cloid. A retry at a higher layer must not double the position."""
    transport = FakeTransport()
    manager = OrderManager(store, transport)
    intent = an_intent()

    first = await manager.submit(intent)
    second = await manager.submit(intent)

    assert len(transport.placed) == 1
    assert first.oid == second.oid


async def test_a_transient_error_retries_and_then_succeeds(store: OrderStore) -> None:
    transport = FakeTransport(acks=[TransientTransportError("connection reset"), 4242])
    manager = OrderManager(store, transport, retries=3, backoff_s=0.0)

    order = await manager.submit(an_intent())

    assert len(transport.placed) == 2
    assert order.oid == 4242
    assert order.status is OrderStatus.ACKED


async def test_transient_errors_give_up_after_the_configured_retries(store: OrderStore) -> None:
    failures = [TransientTransportError("timeout") for _ in range(9)]
    transport = FakeTransport(acks=failures)
    manager = OrderManager(store, transport, retries=3, backoff_s=0.0)

    with pytest.raises(TransientTransportError):
        await manager.submit(an_intent())

    # Three retries after the first attempt, and no more.
    assert len(transport.placed) == 4


async def test_a_reject_is_never_retried(store: OrderStore) -> None:
    """docs/HYPERLIQUID.md: rejects are never retried. Retrying one is how a bug repeats."""
    transport = FakeTransport(acks=[RejectError("Post only order would have immediately matched")])
    manager = OrderManager(store, transport, retries=3, backoff_s=0.0)

    order = await manager.submit(an_intent())

    assert len(transport.placed) == 1
    assert order.status is OrderStatus.REJECTED
    assert "Post only" in (order.reject_reason or "")
    assert (await store.get(order.cloid)).status is OrderStatus.REJECTED


async def test_partial_fills_accumulate_and_close_at_full_size(store: OrderStore) -> None:
    manager = OrderManager(store, FakeTransport())
    order = await manager.submit(an_intent(size=1.0))

    await manager.record_fill(order.cloid, size=0.25, px=100.0, ts_ms=1, fee=0.01)
    partial = await store.get(order.cloid)
    assert partial.status is OrderStatus.PARTIAL
    assert partial.filled == pytest.approx(0.25)

    await manager.record_fill(order.cloid, size=0.75, px=200.0, ts_ms=2, fee=0.03)
    done = await store.get(order.cloid)
    assert done.status is OrderStatus.FILLED
    assert done.filled == pytest.approx(1.0)
    # Size weighted, not the last price and not the mean of the two.
    assert done.avg_px == pytest.approx(0.25 * 100.0 + 0.75 * 200.0)
    assert done.fees == pytest.approx(0.04)


async def test_a_fill_arriving_after_a_cancel_is_still_recorded(store: OrderStore) -> None:
    """The venue is the truth. A cancel that raced a fill did not stop the fill."""
    transport = FakeTransport()
    manager = OrderManager(store, transport)
    order = await manager.submit(an_intent(size=1.0))

    await manager.cancel(order.cloid)
    assert (await store.get(order.cloid)).status is OrderStatus.CANCELED

    await manager.record_fill(order.cloid, size=0.4, px=50.0, ts_ms=3, fee=0.0)
    after = await store.get(order.cloid)
    assert after.filled == pytest.approx(0.4)
    assert after.status is OrderStatus.PARTIAL


async def test_cancel_reaches_the_transport_and_is_idempotent(store: OrderStore) -> None:
    transport = FakeTransport()
    manager = OrderManager(store, transport)
    order = await manager.submit(an_intent())

    await manager.cancel(order.cloid)
    await manager.cancel(order.cloid)

    assert transport.cancelled == [("ETH", order.cloid)]


async def test_reduce_only_and_tif_reach_the_transport_untouched(store: OrderStore) -> None:
    transport = FakeTransport()
    manager = OrderManager(store, transport)
    await manager.submit(an_intent(reduce_only=True, tif="Ioc"))

    assert transport.placed[0].reduce_only is True
    assert transport.placed[0].tif == "Ioc"


async def test_open_orders_lists_only_live_ones(store: OrderStore) -> None:
    manager = OrderManager(store, FakeTransport())
    resting = await manager.submit(an_intent())
    done = await manager.submit(an_intent(size=1.0))
    await manager.record_fill(done.cloid, size=1.0, px=10.0, ts_ms=1, fee=0.0)

    live = {order.cloid for order in await manager.open_orders()}
    assert live == {resting.cloid}


async def test_state_survives_a_restart(tmp_path: Path) -> None:
    """The registry is the sqlite file, not the process. A restart must find its orders."""
    path = tmp_path / "orders.sqlite"
    store = await OrderStore.open(path)
    manager = OrderManager(store, FakeTransport())
    order = await manager.submit(an_intent(size=2.0))
    await manager.record_fill(order.cloid, size=0.5, px=7.0, ts_ms=1, fee=0.0)
    await store.close()

    reopened = await OrderStore.open(path)
    try:
        recovered = await reopened.get(order.cloid)
        assert recovered.status is OrderStatus.PARTIAL
        assert recovered.filled == pytest.approx(0.5)
        assert [o.cloid for o in await reopened.open_orders()] == [order.cloid]
        assert len(await reopened.fills(order.cloid)) == 1
    finally:
        await reopened.close()


async def test_every_transition_logs_one_event(store: OrderStore) -> None:
    """CLAUDE.md: every intent, submission, ack, fill, cancel and reject is one line."""
    manager = OrderManager(store, FakeTransport())
    with structlog.testing.capture_logs() as entries:
        order = await manager.submit(an_intent(size=1.0))
        await manager.record_fill(order.cloid, size=1.0, px=1.0, ts_ms=1, fee=0.0)

    events = [entry["event"] for entry in entries]
    assert "order_submitted" in events
    assert "order_acked" in events
    assert "order_filled" in events
    assert all("cloid" in entry for entry in entries)


async def test_an_unknown_cloid_is_reported_not_invented(store: OrderStore) -> None:
    """A fill for an order we never placed is a reconciliation problem, not a new order."""
    manager = OrderManager(store, FakeTransport())
    with pytest.raises(KeyError):
        await manager.record_fill("0x" + "9" * 32, size=1.0, px=1.0, ts_ms=1, fee=0.0)


async def test_concurrent_submits_of_one_cloid_place_once(store: OrderStore) -> None:
    """Two tasks, one cloid. The registry has to be the gate, not a check-then-act race."""
    transport = FakeTransport()
    manager = OrderManager(store, transport)
    intent = an_intent()

    await asyncio.gather(manager.submit(intent), manager.submit(intent))

    assert len(transport.placed) == 1
