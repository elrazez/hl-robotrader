"""Reconciliation: every way our book and the venue's can disagree, and what we say.

The rule under test everywhere here is that a difference is reported, never quietly
fixed. `reconcile` returns findings; acting on them is the caller's decision and is
logged when it happens.
"""

from __future__ import annotations

import pytest

from liqfade.execution.hl import VenueOrder, VenuePosition
from liqfade.execution.orders import Order, OrderIntent, OrderStatus
from liqfade.execution.reconcile import Reconciler, Severity, reconcile


def our_order(
    cloid: str,
    *,
    coin: str = "ETH",
    status: OrderStatus = OrderStatus.ACKED,
    oid: int | None = 1,
    size: float = 1.0,
    filled: float = 0.0,
) -> Order:
    return Order(
        intent=OrderIntent(
            cloid=cloid, coin=coin, is_buy=True, px=100.0, size=size, tif="Alo", reduce_only=False
        ),
        status=status,
        oid=oid,
        filled=filled,
    )


def venue_order(
    cloid: str | None, *, oid: int = 1, coin: str = "ETH", size: float = 1.0
) -> VenueOrder:
    return VenueOrder(oid=oid, coin=coin, is_buy=True, px=100.0, size=size, ts_ms=0, cloid=cloid)


def test_a_matching_book_reports_nothing() -> None:
    result = reconcile(
        our_orders=[our_order("0xa")],
        venue_orders=[venue_order("0xa")],
        our_positions={"ETH": 1.0},
        venue_positions={"ETH": VenuePosition("ETH", 1.0, 100.0)},
    )
    assert result.differences == []
    assert result.clean is True


def test_an_order_resting_at_the_venue_that_we_never_placed() -> None:
    """Someone's manual order, or ours from a process that died before it persisted."""
    result = reconcile(
        our_orders=[],
        venue_orders=[venue_order(None, oid=55)],
        our_positions={},
        venue_positions={},
    )
    [difference] = result.differences
    assert difference.kind == "unknown_order"
    assert difference.oid == 55
    assert difference.severity is Severity.CANCEL
    assert "55" in difference.detail


def test_an_order_we_think_is_resting_that_the_venue_does_not_have() -> None:
    """It filled or was cancelled while we were disconnected. Ask, do not assume."""
    result = reconcile(
        our_orders=[our_order("0xa")],
        venue_orders=[],
        our_positions={},
        venue_positions={},
    )
    [difference] = result.differences
    assert difference.kind == "missing_order"
    assert difference.cloid == "0xa"
    assert difference.severity is Severity.INVESTIGATE


def test_a_terminal_order_of_ours_is_not_expected_at_the_venue() -> None:
    result = reconcile(
        our_orders=[our_order("0xa", status=OrderStatus.FILLED, filled=1.0)],
        venue_orders=[],
        our_positions={},
        venue_positions={},
    )
    assert result.differences == []


def test_a_position_the_venue_has_and_we_do_not_is_a_halt() -> None:
    """docs/HYPERLIQUID.md: the bot never adopts a position it did not open."""
    result = reconcile(
        our_orders=[],
        venue_orders=[],
        our_positions={},
        venue_positions={"SOL": VenuePosition("SOL", -20.0, 150.0)},
    )
    [difference] = result.differences
    assert difference.kind == "unknown_position"
    assert difference.coin == "SOL"
    assert difference.severity is Severity.HALT


def test_a_position_we_have_and_the_venue_does_not_is_a_halt() -> None:
    result = reconcile(
        our_orders=[],
        venue_orders=[],
        our_positions={"ETH": 0.5},
        venue_positions={},
    )
    [difference] = result.differences
    assert difference.kind == "missing_position"
    assert difference.severity is Severity.HALT


def test_a_size_difference_beyond_tolerance_is_a_halt() -> None:
    result = reconcile(
        our_orders=[],
        venue_orders=[],
        our_positions={"ETH": 1.0},
        venue_positions={"ETH": VenuePosition("ETH", 1.5, 100.0)},
    )
    [difference] = result.differences
    assert difference.kind == "position_size"
    assert difference.ours == pytest.approx(1.0)
    assert difference.theirs == pytest.approx(1.5)


def test_a_rounding_sized_difference_is_not_a_difference() -> None:
    """szDecimals rounding makes the last digit disagree. That is not a broken book."""
    result = reconcile(
        our_orders=[],
        venue_orders=[],
        our_positions={"ETH": 1.0},
        venue_positions={"ETH": VenuePosition("ETH", 1.0 + 1e-9, 100.0)},
    )
    assert result.differences == []


def test_a_side_flip_is_reported_even_when_the_size_matches() -> None:
    """Long one and short one is the same absolute size and the opposite of correct."""
    result = reconcile(
        our_orders=[],
        venue_orders=[],
        our_positions={"ETH": 1.0},
        venue_positions={"ETH": VenuePosition("ETH", -1.0, 100.0)},
    )
    [difference] = result.differences
    assert difference.kind == "position_size"
    assert difference.severity is Severity.HALT


def test_an_order_size_that_shrank_at_the_venue_is_a_partial_fill_we_missed() -> None:
    result = reconcile(
        our_orders=[our_order("0xa", size=1.0, filled=0.0)],
        venue_orders=[venue_order("0xa", size=0.4)],
        our_positions={},
        venue_positions={},
    )
    [difference] = result.differences
    assert difference.kind == "order_size"
    assert difference.severity is Severity.INVESTIGATE


def test_our_partial_matching_the_venue_remainder_is_clean() -> None:
    result = reconcile(
        our_orders=[our_order("0xa", size=1.0, filled=0.6, status=OrderStatus.PARTIAL)],
        venue_orders=[venue_order("0xa", size=0.4)],
        our_positions={},
        venue_positions={},
    )
    assert result.differences == []


def test_the_worst_severity_is_what_the_report_leads_with() -> None:
    result = reconcile(
        our_orders=[our_order("0xa")],
        venue_orders=[venue_order(None, oid=9)],
        our_positions={},
        venue_positions={"SOL": VenuePosition("SOL", 1.0, 1.0)},
    )
    assert result.worst is Severity.HALT
    assert result.clean is False
    # The summary is what goes to Telegram, so it has to name every kind, not just one.
    assert "unknown_position" in result.summary()
    assert "missing_order" in result.summary()


# --- the loop -------------------------------------------------------------------


class FakeReader:
    """Answers the two info queries, or raises whatever the test queued."""

    def __init__(
        self, positions=None, orders=None, triggers=None, error: Exception | None = None
    ) -> None:
        from liqfade.execution.hl import AccountSnapshot

        self.snapshot = AccountSnapshot(
            equity=1000.0, margin_used=0.0, withdrawable=1000.0, positions=positions or {}
        )
        self.orders = orders or []
        self.triggers = triggers or []
        self.error = error
        self.calls = 0

    async def account(self, address: str):
        self.calls += 1
        if self.error:
            raise self.error
        return self.snapshot

    async def open_orders(self, address: str):
        return self.orders

    async def trigger_orders(self, address: str):
        return self.triggers


async def test_the_loop_posts_once_per_run_that_found_something() -> None:
    from liqfade.execution.reconcile import Reconciler

    posted: list[str] = []
    reader = FakeReader(positions={"SOL": VenuePosition("SOL", 3.0, 1.0)})
    reconciler = Reconciler(
        reader=reader,  # type: ignore[arg-type]
        address="0x1",
        orders=_no_orders,
        positions=dict,
        alert=_collect(posted),
    )

    result = await reconciler.check("startup")

    assert result is not None and result.worst is Severity.HALT
    assert len(posted) == 1
    assert "unknown_position" in posted[0]
    assert "SOL" in posted[0]


async def test_a_failed_query_is_never_reported_as_clean() -> None:
    """A timed out request looks like a flat account. Reading it as one is the worst bug."""
    from liqfade.execution.reconcile import Reconciler

    posted: list[str] = []
    reconciler = Reconciler(
        reader=FakeReader(error=TimeoutError("read timeout")),  # type: ignore[arg-type]
        address="0x1",
        orders=_no_orders,
        positions=lambda: {"ETH": 1.0},
        alert=_collect(posted),
    )

    assert await reconciler.check("interval") is None
    assert reconciler.failures == 1
    assert reconciler.runs == 0
    assert reconciler.last is None
    assert posted == []


async def test_the_loop_checks_on_startup_on_reconnect_and_on_the_interval() -> None:
    import asyncio

    from liqfade.execution.reconcile import Reconciler

    reader = FakeReader()
    reconciler = Reconciler(
        reader=reader,  # type: ignore[arg-type]
        address="0x1",
        orders=_no_orders,
        positions=dict,
        interval_s=0.05,
    )
    stop, reconnected = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(reconciler.run(stop, reconnected=reconnected))

    await asyncio.sleep(0.01)
    assert reader.calls == 1  # startup
    reconnected.set()
    await asyncio.sleep(0.01)
    assert reader.calls == 2  # reconnect, without waiting out the interval
    await asyncio.sleep(0.12)
    stop.set()
    await task
    assert reader.calls >= 3  # and then on the interval


async def _no_orders():
    return []


def _collect(sink: list[str]):
    async def alert(text: str) -> bool:
        sink.append(text)
        return True

    return alert


async def test_the_account_snapshot_is_handed_to_the_caller_before_the_comparison() -> None:
    """The venue is the truth about positions, and the session has to be told.

    The order matters: the local book is overwritten from `clearinghouseState` and only
    then compared, so a reconcile reports what the loop believes *after* it has been
    corrected. Comparing first would report a difference the same call was about to fix,
    once every interval, forever.
    """
    from liqfade.execution.hl import AccountSnapshot, VenuePosition

    snapshot = AccountSnapshot(
        equity=990.0,
        margin_used=10.0,
        withdrawable=980.0,
        positions={"ETH": VenuePosition(coin="ETH", size=0.01, entry_px=3000.0)},
    )
    seen: list[AccountSnapshot] = []
    ours: dict[str, float] = {}

    class Reader:
        async def open_orders(self, address: str):
            return []

        async def trigger_orders(self, address: str):
            return []

        async def account(self, address: str) -> AccountSnapshot:
            return snapshot

    def on_account(account: AccountSnapshot) -> None:
        seen.append(account)
        ours.clear()
        ours.update({coin: p.size for coin, p in account.positions.items()})

    reconciler = Reconciler(
        reader=Reader(),
        address="0xacc",
        orders=lambda: _none(),
        positions=lambda: dict(ours),
        compare_positions=True,
        on_account=on_account,
    )

    result = await reconciler.check("test")

    assert seen == [snapshot]
    assert result is not None and result.clean, "corrected first, then compared"


async def _none():
    return []


# --- the bracket legs, which live in a different query ----------------------------


def venue_trigger(cloid: str | None, *, oid: int = 9, coin: str = "ETH", size: float = 0.01):
    from liqfade.execution.hl import TriggerOrder

    return TriggerOrder(
        cloid=cloid,
        coin=coin,
        oid=oid,
        trigger_px=100.0,
        is_stop=True,
        size=size,
        reduce_only=True,
    )


async def test_a_bracketed_position_reconciles_clean(tmp_path) -> None:
    """The legs of a bracket rest in `frontendOpenOrders`, not in `openOrders`.

    Read from `openOrders` alone, every bracketed position reports its two legs missing
    every interval, and the week without a mismatch the ROADMAP asks for cannot happen.
    """
    from tests.test_live_venue import COIN, START, Row, a_session, an_intent, quote

    session, _venue = await a_session(tmp_path)
    try:
        quote(session, START, 2999.0, 3001.0)
        cloid = await session.place(an_intent("long"), START, Row())
        await session.on_fill(cloid, px=3000.0, size=0.01, ts_ms=START + 10)
        ours = await session.orders.store.open_orders()
        legs = session.brackets[COIN]
        entry = next(o for o in ours if o.intent.tag == "entry")

        result = reconcile(
            our_orders=ours,
            venue_orders=[venue_order(entry.cloid, coin=COIN, size=entry.remaining)],
            venue_triggers=[
                venue_trigger(legs.tp_cloid, coin=COIN),
                venue_trigger(legs.sl_cloid, coin=COIN),
            ],
            our_positions={COIN: 0.01},
            venue_positions={COIN: VenuePosition(coin=COIN, size=0.01, entry_px=3000.0)},
        )

        assert result.clean, [d.detail for d in result.differences]
        assert result.checked_orders == 6  # three ours, three theirs
    finally:
        await session._store.close()


def test_a_bracket_leg_the_venue_no_longer_has_is_reported() -> None:
    """The stop is gone and the position is not. That is the difference worth waking for."""
    result = reconcile(
        our_orders=[our_order("0xtp", size=0.01), our_order("0xsl", size=0.01)],
        venue_orders=[],
        venue_triggers=[venue_trigger("0xtp")],
        our_positions={},
        venue_positions={},
    )

    assert [(d.kind, d.cloid) for d in result.differences] == [("missing_order", "0xsl")]
    assert result.worst is Severity.INVESTIGATE


def test_a_trigger_order_we_never_placed_is_reported_like_any_other_stranger() -> None:
    """A stop placed by hand in the UI is one the bot did not put there. Same finding."""
    result = reconcile(
        our_orders=[],
        venue_orders=[],
        venue_triggers=[venue_trigger("0xstranger")],
        our_positions={},
        venue_positions={},
    )

    assert [(d.kind, d.severity) for d in result.differences] == [
        ("unknown_order", Severity.CANCEL)
    ]


async def test_the_reconciler_asks_the_venue_for_its_trigger_orders() -> None:
    """`InfoReader.trigger_orders` is the second query, and it has to be made."""

    class Reader:
        def __init__(self) -> None:
            self.asked: list[str] = []

        async def account(self, address: str):
            from liqfade.execution.hl import AccountSnapshot

            self.asked.append("account")
            return AccountSnapshot(equity=1.0, margin_used=0.0, withdrawable=1.0, positions={})

        async def open_orders(self, address: str):
            self.asked.append("open_orders")
            return []

        async def trigger_orders(self, address: str):
            self.asked.append("trigger_orders")
            return [venue_trigger("0xsl")]

    reader = Reader()
    reconciler = Reconciler(
        reader=reader,
        address="0x1",
        orders=lambda: _one_leg(),
        positions=dict,
        compare_positions=False,
    )

    result = await reconciler.check("startup")

    assert "trigger_orders" in reader.asked
    assert result is not None and result.clean, "the leg is at the venue, in the other query"


async def _one_leg():
    return [our_order("0xsl", size=0.01)]
