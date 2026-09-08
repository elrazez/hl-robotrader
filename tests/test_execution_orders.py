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


# Real Hyperliquid szDecimals, so the rounding these tests assert is the rounding the
# venue would do. ETH is 4, BTC 5, SOL 2.
SZ_DECIMALS = {"ETH": 4, "BTC": 5, "SOL": 2}


def a_manager(store: OrderStore, transport: Any = None, **kwargs: Any) -> OrderManager:
    return OrderManager(
        store,
        transport or FakeTransport(),
        sz_decimals=SZ_DECIMALS,
        killed=lambda: False,
        **kwargs,
    )


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
    manager = a_manager(store)
    order = await manager.submit(an_intent())

    assert order.status is OrderStatus.ACKED
    assert order.oid is not None
    assert (await store.get(order.cloid)).status is OrderStatus.ACKED


async def test_the_same_cloid_is_never_placed_twice(store: OrderStore) -> None:
    """Idempotency by cloid. A retry at a higher layer must not double the position."""
    transport = FakeTransport()
    manager = a_manager(store, transport)
    intent = an_intent()

    first = await manager.submit(intent)
    second = await manager.submit(intent)

    assert len(transport.placed) == 1
    assert first.oid == second.oid


async def test_a_transient_error_retries_and_then_succeeds(store: OrderStore) -> None:
    transport = FakeTransport(acks=[TransientTransportError("connection reset"), 4242])
    manager = a_manager(store, transport, retries=3, backoff_s=0.0)

    order = await manager.submit(an_intent())

    assert len(transport.placed) == 2
    assert order.oid == 4242
    assert order.status is OrderStatus.ACKED


async def test_transient_errors_give_up_after_the_configured_retries(store: OrderStore) -> None:
    failures = [TransientTransportError("timeout") for _ in range(9)]
    transport = FakeTransport(acks=failures)
    manager = a_manager(store, transport, retries=3, backoff_s=0.0)

    with pytest.raises(TransientTransportError):
        await manager.submit(an_intent())

    # Three retries after the first attempt, and no more.
    assert len(transport.placed) == 4


async def test_a_reject_is_never_retried(store: OrderStore) -> None:
    """docs/HYPERLIQUID.md: rejects are never retried. Retrying one is how a bug repeats."""
    transport = FakeTransport(acks=[RejectError("Post only order would have immediately matched")])
    manager = a_manager(store, transport, retries=3, backoff_s=0.0)

    order = await manager.submit(an_intent())

    assert len(transport.placed) == 1
    assert order.status is OrderStatus.REJECTED
    assert "Post only" in (order.reject_reason or "")
    assert (await store.get(order.cloid)).status is OrderStatus.REJECTED


async def test_partial_fills_accumulate_and_close_at_full_size(store: OrderStore) -> None:
    manager = a_manager(store)
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
    manager = a_manager(store, transport)
    order = await manager.submit(an_intent(size=1.0))

    await manager.cancel(order.cloid)
    assert (await store.get(order.cloid)).status is OrderStatus.CANCELED

    await manager.record_fill(order.cloid, size=0.4, px=50.0, ts_ms=3, fee=0.0)
    after = await store.get(order.cloid)
    assert after.filled == pytest.approx(0.4)
    assert after.status is OrderStatus.PARTIAL


async def test_cancel_reaches_the_transport_and_is_idempotent(store: OrderStore) -> None:
    transport = FakeTransport()
    manager = a_manager(store, transport)
    order = await manager.submit(an_intent())

    await manager.cancel(order.cloid)
    await manager.cancel(order.cloid)

    assert transport.cancelled == [("ETH", order.cloid)]


async def test_reduce_only_and_tif_reach_the_transport_untouched(store: OrderStore) -> None:
    transport = FakeTransport()
    manager = a_manager(store, transport)
    await manager.submit(an_intent(reduce_only=True, tif="Ioc"))

    assert transport.placed[0].reduce_only is True
    assert transport.placed[0].tif == "Ioc"


async def test_open_orders_lists_only_live_ones(store: OrderStore) -> None:
    manager = a_manager(store)
    resting = await manager.submit(an_intent())
    done = await manager.submit(an_intent(size=1.0))
    await manager.record_fill(done.cloid, size=1.0, px=10.0, ts_ms=1, fee=0.0)

    live = {order.cloid for order in await manager.open_orders()}
    assert live == {resting.cloid}


async def test_state_survives_a_restart(tmp_path: Path) -> None:
    """The registry is the sqlite file, not the process. A restart must find its orders."""
    path = tmp_path / "orders.sqlite"
    store = await OrderStore.open(path)
    manager = a_manager(store)
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
    manager = a_manager(store)
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
    manager = a_manager(store)
    with pytest.raises(KeyError):
        await manager.record_fill("0x" + "9" * 32, size=1.0, px=1.0, ts_ms=1, fee=0.0)


async def test_concurrent_submits_of_one_cloid_place_once(store: OrderStore) -> None:
    """Two tasks, one cloid. The registry has to be the gate, not a check-then-act race."""
    transport = FakeTransport()
    manager = a_manager(store, transport)
    intent = an_intent()

    await asyncio.gather(manager.submit(intent), manager.submit(intent))

    assert len(transport.placed) == 1


async def test_an_unrounded_intent_reaches_the_venue_rounded(store: OrderStore) -> None:
    """CLAUDE.md: rounding lives in one place. That place is the boundary, not the caller.

    The strategy works in floats and does not know szDecimals. `submit` is the last thing
    that touches a price before the wire, so it is the only thing that rounds one.
    """
    transport = FakeTransport()
    manager = a_manager(store, transport)

    await manager.submit(an_intent(coin="ETH", px=3000.123456789, size=0.0123456789))

    sent = transport.placed[0]
    assert sent.wire_px == "3000.1"  # five significant figures, two decimals at szDecimals 4
    assert sent.wire_size == "0.0123"
    assert sent.px == pytest.approx(3000.1)
    assert sent.size == pytest.approx(0.0123)


async def test_the_registry_records_what_was_actually_sent(store: OrderStore) -> None:
    """A row holding the price we asked for rather than the one we sent reconciles wrong."""
    manager = a_manager(store)

    order = await manager.submit(an_intent(coin="BTC", px=104999.987654, size=0.123456789))

    row = await store.get(order.cloid)
    assert row.intent.px == pytest.approx(105000.0)  # five significant figures
    assert row.intent.size == pytest.approx(0.12346)  # BTC szDecimals is 5


async def test_a_trigger_price_is_rounded_too(store: OrderStore) -> None:
    transport = FakeTransport()
    manager = a_manager(store, transport)

    await manager.submit(
        an_intent(coin="ETH", px=3000.0, size=1.0, trigger_px=2991.987654, tpsl="sl", tif="Ioc")
    )

    # Five significant figures, which for this price lands on the whole number.
    assert transport.placed[0].trigger_px == pytest.approx(2992.0)


async def test_a_coin_with_no_szdecimals_is_refused_rather_than_guessed(
    store: OrderStore,
) -> None:
    """Guessing the grid is how a bug becomes an order. There is no safe default here."""
    transport = FakeTransport()
    manager = a_manager(store, transport)

    order = await manager.submit(an_intent(coin="DOGE", px=0.1234567, size=100.0))

    assert order.status is OrderStatus.REJECTED
    assert "szDecimals" in (order.reject_reason or "")
    assert transport.placed == []


async def test_a_size_that_rounds_away_is_refused(store: OrderStore) -> None:
    """Zero is a size the venue would take and nobody meant."""
    transport = FakeTransport()
    manager = a_manager(store, transport)

    order = await manager.submit(an_intent(coin="SOL", px=150.0, size=0.001))

    assert order.status is OrderStatus.REJECTED
    assert transport.placed == []


async def test_submit_refuses_while_the_kill_flag_is_there(store: OrderStore) -> None:
    """The check made once at `load_signer` is not the check that matters at submit time.

    A kill can land in the seconds between loading a signer and placing an order, and
    CLAUDE.md rule 5 says the flag wins. The order path asks again rather than trusting an
    answer from before the flag existed.
    """
    transport = FakeTransport()
    manager = OrderManager(store, transport, sz_decimals=SZ_DECIMALS, killed=lambda: True)

    order = await manager.submit(an_intent(coin="ETH"))

    assert order.status is OrderStatus.REJECTED
    assert "kill" in (order.reject_reason or "").lower()
    assert transport.placed == []


# --- the entry policy: repricing by cloid ---------------------------------------


class ModifyTransport(FakeTransport):
    """A transport that can also modify, answering with whatever the test queued."""

    def __init__(self, *, modifies: list[Any] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.modified: list[tuple[str, float]] = []
        self.modifies = modifies or []

    async def modify(self, intent: OrderIntent) -> int:
        self.modified.append((intent.cloid, intent.px))
        if self.modifies:
            outcome = self.modifies.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return int(outcome)
        self.next_oid += 1
        return self.next_oid


async def test_a_reprice_keeps_the_cloid_and_takes_the_new_oid(store: OrderStore) -> None:
    """Measured on testnet 2026-09-08: modify answers with a new oid and the same cloid.

    So the registry tracks the entry by cloid across its reprices, and records the oid the
    venue moved it to, because the old one is dead the moment the modify lands.
    """
    transport = ModifyTransport(modifies=[7777])
    manager = a_manager(store, transport)
    order = await manager.submit(an_intent(px=2000.0))

    repriced = await manager.reprice(order.cloid, 2010.0)

    assert repriced.cloid == order.cloid
    assert repriced.oid == 7777
    assert repriced.intent.px == 2010.0
    assert repriced.reprices == 1
    assert repriced.status is OrderStatus.ACKED
    assert transport.modified == [(order.cloid, 2010.0)]


async def test_a_reprice_rounds_onto_the_venue_grid_like_a_submit(store: OrderStore) -> None:
    """A reprice is an order too. An unrounded price is refused, not sent."""
    manager = a_manager(store, ModifyTransport())
    order = await manager.submit(an_intent(px=2000.0))

    repriced = await manager.reprice(order.cloid, 2000.123456789)

    assert repriced.intent.px == 2000.1  # 5 significant figures, docs/HYPERLIQUID.md
    assert repriced.intent.wire_px == "2000.1"


async def test_a_reprice_that_lost_the_race_is_left_for_reconciliation(store: OrderStore) -> None:
    """ "Cannot modify canceled or filled order" means the venue already moved on.

    Measured on testnet: modifying a stale oid answers with that error and leaves the live
    order untouched, so the fill or the cancel that beat us is the truth. Recording it as a
    rejection would mark an order REJECTED that may have filled, which is the one direction
    the registry must never be wrong in. It stays live and the next reconciliation settles
    it against the venue.
    """
    gone = RejectError("Cannot modify canceled or filled order")
    manager = a_manager(store, ModifyTransport(modifies=[gone]))
    order = await manager.submit(an_intent(px=2000.0))

    repriced = await manager.reprice(order.cloid, 2010.0)

    assert repriced.status is OrderStatus.ACKED, "not REJECTED: it may have filled"
    assert repriced.intent.px == 2000.0, "the price the venue still has, not the one we wanted"
    assert repriced.reprices == 0, "a move that did not happen is not a move"


async def test_a_finished_order_is_not_repriced(store: OrderStore) -> None:
    transport = ModifyTransport()
    manager = a_manager(store, transport)
    order = await manager.submit(an_intent(px=2000.0))
    await manager.cancel(order.cloid)

    repriced = await manager.reprice(order.cloid, 2010.0)

    assert repriced.status is OrderStatus.CANCELED
    assert transport.modified == []


async def test_the_reprice_count_survives_a_restart(tmp_path: Path) -> None:
    """The count bounds the reprices. Losing it across a restart unbounds them."""
    first = await OrderStore.open(tmp_path / "orders.sqlite")
    try:
        manager = a_manager(first, ModifyTransport())
        order = await manager.submit(an_intent(px=2000.0))
        await manager.reprice(order.cloid, 2010.0)
    finally:
        await first.close()

    second = await OrderStore.open(tmp_path / "orders.sqlite")
    try:
        manager2 = a_manager(second, ModifyTransport())
        await manager2.load()
        assert manager2.get(order.cloid).reprices == 1
    finally:
        await second.close()


# --- the action budget ----------------------------------------------------------


async def test_a_submit_past_the_budget_is_refused_before_the_venue(store: OrderStore) -> None:
    """Refused here, not by Hyperliquid. A rejected order still costs an action there."""
    from liqfade.execution.ratelimit import ActionBudget

    transport = FakeTransport()
    manager = a_manager(store, transport, budget=ActionBudget(per_hour=1))
    await manager.submit(an_intent(px=2000.0))

    second = await manager.submit(an_intent(px=2001.0))

    assert second.status is OrderStatus.REJECTED
    assert "budget" in (second.reject_reason or "").lower()
    assert len(transport.placed) == 1, "the second order never reached the transport"


async def test_a_cancel_is_never_refused_by_the_budget(store: OrderStore) -> None:
    """CLAUDE.md rule 5. A limiter that can block a cancel can hold a position open."""
    from liqfade.execution.ratelimit import ActionBudget

    transport = FakeTransport()
    manager = a_manager(store, transport, budget=ActionBudget(per_hour=1))
    order = await manager.submit(an_intent(px=2000.0))

    cancelled = await manager.cancel(order.cloid)

    assert cancelled.status is OrderStatus.CANCELED
    assert transport.cancelled == [("ETH", order.cloid)]


async def test_a_reprice_past_the_budget_is_refused(store: OrderStore) -> None:
    """Repricing is what spends this budget, so it is what the budget has to stop."""
    from liqfade.execution.ratelimit import ActionBudget

    transport = ModifyTransport()
    manager = a_manager(store, transport, budget=ActionBudget(per_hour=1))
    order = await manager.submit(an_intent(px=2000.0))

    repriced = await manager.reprice(order.cloid, 2010.0)

    assert repriced.intent.px == 2000.0, "unmoved"
    assert repriced.status is OrderStatus.ACKED, "still live, just not moved"
    assert transport.modified == []


async def test_the_manager_reports_what_the_panel_renders(store: OrderStore) -> None:
    from liqfade.execution.ratelimit import ActionBudget

    manager = a_manager(store, FakeTransport(), budget=ActionBudget(per_hour=600))
    await manager.submit(an_intent(px=2000.0))

    assert manager.actions_remaining() == 599


async def test_without_a_budget_nothing_is_counted(store: OrderStore) -> None:
    """The budget is optional so the backtest and the tests that predate it still run."""
    manager = a_manager(store, FakeTransport())
    await manager.submit(an_intent(px=2000.0))

    assert manager.actions_remaining() is None
