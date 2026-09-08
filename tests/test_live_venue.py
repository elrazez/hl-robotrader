"""The live order path: real orders, venue fills, and the venue as the truth.

The seam is `VenueSession` driven through a fake transport and a fake reader, because the
point of the transport protocol is that the same session runs against Hyperliquid and
against these. Nothing here reaches the network and nothing here can sign.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from liqfade.execution.hl import AccountSnapshot, VenuePosition
from liqfade.execution.orders import OrderIntent, OrderManager, OrderStatus, OrderStore
from liqfade.live.venue import VenueSession
from liqfade.strategy.types import Intent
from tests.test_live_loop import paper_settings

COIN = "ETH"
START = 1_700_000_000_000


class FakeVenue:
    """Records every call and answers the way testnet answered on 2026-09-08."""

    def __init__(self) -> None:
        self.placed: list[OrderIntent] = []
        self.cancelled: list[tuple[str, str]] = []
        self.modified: list[OrderIntent] = []
        self.groups: list[list[dict[str, Any]]] = []
        self.oid = 1000

    async def place(self, intent: OrderIntent) -> int:
        self.placed.append(intent)
        self.oid += 1
        return self.oid

    async def cancel(self, coin: str, cloid: str) -> None:
        self.cancelled.append((coin, cloid))

    async def modify(self, intent: OrderIntent) -> int:
        self.modified.append(intent)
        self.oid += 1
        return self.oid

    async def place_group(self, requests: list[dict[str, Any]]) -> list[str]:
        self.groups.append(requests)
        return ["waitingForTrigger", "waitingForTrigger"]


class FakeReader:
    async def asset_meta(self) -> dict[str, Any]:
        from liqfade.execution.hl import AssetRules

        return {COIN: AssetRules(coin=COIN, sz_decimals=4, max_leverage=25.0)}

    def __init__(self) -> None:
        self.snapshot = AccountSnapshot(
            equity=1000.0, margin_used=0.0, withdrawable=1000.0, positions={}
        )

    async def account(self, address: str) -> AccountSnapshot:
        return self.snapshot


async def a_session(tmp_path: Path, **dotted: Any) -> tuple[VenueSession, FakeVenue]:
    settings = paper_settings(tmp_path, **dotted)
    store = await OrderStore.open(tmp_path / "orders.sqlite")
    venue = FakeVenue()
    session = VenueSession(
        settings,
        orders=OrderManager(store, venue, sz_decimals={COIN: 4}, killed=lambda: False),
        reader=FakeReader(),
        address="0xacc",
        meta={},
        starting_equity=1000.0,
    )
    session._store = store  # for the fixture to close
    await session.load_asset_rules()
    return session, venue


def quote(session: VenueSession, ts: int, bid: float, ask: float) -> None:
    session.ingest(
        "bbo",
        "hyperliquid",
        {
            "coin": COIN,
            "ts_ms": ts,
            "rx_ms": ts,
            "bid_px": bid,
            "bid_sz": 100.0,
            "ask_px": ask,
            "ask_sz": 100.0,
        },
    )


class Row:
    """The two fields sizing reads off a `FeatureRow`. Enough volume to clear vol_cap."""

    traded_usd_vol_window = 5_000_000.0

    def depth(self, side: str) -> float:
        return 500_000.0


def an_intent(side: str = "long", *, urgency: str = "normal", px: float = 3000.0) -> Intent:
    direction = 1.0 if side == "long" else -1.0
    return Intent(
        coin=COIN,
        side=side,
        entry_px=px,
        target_px=px * (1 + direction * 0.003),
        stop_px=px * (1 - direction * 0.003),
        time_stop_s=600.0,
        urgency=urgency,
        ts_ms=START,
        explain={},
    )


async def test_an_entry_rests_alo_at_the_touch(tmp_path: Path) -> None:
    """A buy rests on the bid. Crossing is what Alo refuses, measured on testnet."""
    session, venue = await a_session(tmp_path)
    try:
        quote(session, START, 2999.0, 3001.0)
        cloid = await session.place(an_intent("long"), START, Row())

        assert cloid is not None
        (order,) = venue.placed
        assert order.tif == "Alo"
        assert order.px == 2999.0, "the bid, not the mid and not the ask"
        assert order.tag == "entry"
        assert order.is_buy is True
    finally:
        await session._store.close()


async def test_a_high_urgency_intent_crosses_instead(tmp_path: Path) -> None:
    """strategy/types.py: only high urgency may take liquidity."""
    session, venue = await a_session(tmp_path)
    try:
        quote(session, START, 2999.0, 3001.0)
        await session.place(an_intent("long", urgency="high"), START, Row())

        assert venue.placed[0].tif == "Ioc"
    finally:
        await session._store.close()


async def test_a_short_rests_on_the_offer(tmp_path: Path) -> None:
    session, venue = await a_session(tmp_path)
    try:
        quote(session, START, 2999.0, 3001.0)
        await session.place(an_intent("short"), START, Row())

        assert venue.placed[0].px == 3001.0
        assert venue.placed[0].is_buy is False
    finally:
        await session._store.close()


async def test_no_book_means_no_order_rather_than_an_invented_price(tmp_path: Path) -> None:
    session, venue = await a_session(tmp_path)
    try:
        assert await session.place(an_intent("long"), START, Row()) is None
        assert venue.placed == []
    finally:
        await session._store.close()


async def test_a_coin_already_held_is_not_entered_again(tmp_path: Path) -> None:
    """The policy reprices a resting entry; it never stacks a second on one cascade."""
    session, venue = await a_session(tmp_path)
    try:
        quote(session, START, 2999.0, 3001.0)
        session.sync_account(
            AccountSnapshot(
                equity=1000.0,
                margin_used=10.0,
                withdrawable=990.0,
                positions={COIN: VenuePosition(coin=COIN, size=0.1, entry_px=3000.0)},
            )
        )

        assert await session.place(an_intent("long"), START, Row()) is None
        assert venue.placed == []
    finally:
        await session._store.close()


async def test_a_resting_entry_is_repriced_to_the_new_touch(tmp_path: Path) -> None:
    """The entry policy, running against the venue instead of the fill model."""
    session, venue = await a_session(
        tmp_path, **{"execution.entry_ttl_s": 2.0, "execution.max_reprices": 2}
    )
    try:
        quote(session, START, 2999.0, 3001.0)
        cloid = await session.place(an_intent("long"), START, Row())
        assert cloid is not None

        quote(session, START + 3_000, 2995.0, 2996.0)
        await session.age_entries(session.orders.get(cloid).priced_ms + 2_000)

        assert len(venue.modified) == 1
        assert venue.modified[0].px == 2995.0, "the new bid"
        assert session.orders.get(cloid).reprices == 1
    finally:
        await session._store.close()


async def test_an_entry_past_its_reprices_is_cancelled(tmp_path: Path) -> None:
    session, venue = await a_session(
        tmp_path, **{"execution.entry_ttl_s": 1.0, "execution.max_reprices": 0}
    )
    try:
        quote(session, START, 2999.0, 3001.0)
        cloid = await session.place(an_intent("long"), START, Row())
        assert cloid is not None

        await session.age_entries(session.orders.get(cloid).priced_ms + 5_000)

        assert venue.cancelled == [(COIN, cloid)]
        assert session.orders.get(cloid).status is OrderStatus.CANCELED
        assert cloid not in session.entry_intents
    finally:
        await session._store.close()


async def test_a_filled_entry_is_bracketed_at_the_venue(tmp_path: Path) -> None:
    """The bracket goes on immediately, so the position survives this process dying."""
    session, venue = await a_session(tmp_path)
    try:
        quote(session, START, 2999.0, 3001.0)
        intent = an_intent("long")
        cloid = await session.place(intent, START, Row())
        assert cloid is not None

        await session.on_fill(cloid, px=2999.0, size=0.01, ts_ms=START + 500)

        assert len(venue.groups) == 1
        legs = venue.groups[0]
        assert {leg["order_type"]["trigger"]["tpsl"] for leg in legs} == {"tp", "sl"}
        assert all(leg["reduce_only"] for leg in legs)
        assert all(leg["is_buy"] is False for leg in legs), "a long exits by selling"
        by_kind = {leg["order_type"]["trigger"]["tpsl"]: leg for leg in legs}
        assert by_kind["tp"]["limit_px"] == pytest.approx(intent.target_px, rel=1e-3)
        assert COIN in session.brackets
    finally:
        await session._store.close()


async def test_a_fill_on_an_unknown_cloid_brackets_nothing(tmp_path: Path) -> None:
    """An exit leg filling is a fill too, and it must not open a bracket of its own."""
    session, venue = await a_session(tmp_path)
    try:
        await session.on_fill("0xstranger", px=3000.0, size=0.01, ts_ms=START)
        assert venue.groups == []
    finally:
        await session._store.close()


async def test_the_venue_overwrites_what_we_think_we_hold(tmp_path: Path) -> None:
    """A local book that argued with clearinghouseState is the bug reconciliation finds."""
    session, _venue = await a_session(tmp_path)
    try:
        session.sync_account(
            AccountSnapshot(
                equity=990.0,
                margin_used=10.0,
                withdrawable=980.0,
                positions={COIN: VenuePosition(coin=COIN, size=0.01, entry_px=3000.0)},
            )
        )
        assert await session.open_positions() == {COIN: 0.01}
        assert session.equity() == 990.0

        session.sync_account(
            AccountSnapshot(equity=1005.0, margin_used=0.0, withdrawable=1005.0, positions={})
        )
        assert await session.open_positions() == {}
    finally:
        await session._store.close()


async def test_a_closed_position_releases_its_bracket(tmp_path: Path) -> None:
    """The venue cancels both legs when the position closes, so we stop tracking them.

    Measured on testnet: both legs were gone within 2s of the close, with no cancel of
    ours. Cancelling them ourselves would spend an action to be told they are gone.
    """
    session, venue = await a_session(tmp_path)
    try:
        quote(session, START, 2999.0, 3001.0)
        cloid = await session.place(an_intent("long"), START, Row())
        assert cloid is not None
        await session.on_fill(cloid, px=2999.0, size=0.01, ts_ms=START)
        session.sync_account(
            AccountSnapshot(
                equity=1000.0,
                margin_used=10.0,
                withdrawable=990.0,
                positions={COIN: VenuePosition(coin=COIN, size=0.01, entry_px=2999.0)},
            )
        )
        assert COIN in session.brackets

        session.sync_account(
            AccountSnapshot(equity=1001.0, margin_used=0.0, withdrawable=1001.0, positions={})
        )

        assert session.brackets == {}
        assert venue.cancelled == [], "the venue removed them; we spend no action saying so"
    finally:
        await session._store.close()


async def test_flatten_cancels_everything_and_closes_reduce_only(tmp_path: Path) -> None:
    """What /flat and the kill's second half do. Reduce-only, because our size can be stale."""
    session, venue = await a_session(tmp_path)
    try:
        quote(session, START, 2999.0, 3001.0)
        await session.place(an_intent("long"), START, Row())
        session.sync_account(
            AccountSnapshot(
                equity=1000.0,
                margin_used=10.0,
                withdrawable=990.0,
                positions={COIN: VenuePosition(coin=COIN, size=0.01, entry_px=3000.0)},
            )
        )

        closed = await session.flatten(now_ms=START)

        assert closed == [COIN]
        assert len(venue.cancelled) == 1, "the resting entry"
        exit_order = venue.placed[-1]
        assert exit_order.reduce_only is True
        assert exit_order.tif == "Ioc"
        assert exit_order.is_buy is False, "a long is closed by selling"
        assert exit_order.px < 2999.0, "crossing down by the slippage bound so an Ioc takes"
    finally:
        await session._store.close()


async def test_shutdown_cancels_orders_and_leaves_the_position_to_its_bracket(
    tmp_path: Path,
) -> None:
    """A deploy restart must not realise a loss. The bracket at the venue outlives us."""
    session, venue = await a_session(tmp_path)
    try:
        quote(session, START, 2999.0, 3001.0)
        await session.place(an_intent("long"), START, Row())
        session.sync_account(
            AccountSnapshot(
                equity=1000.0,
                margin_used=10.0,
                withdrawable=990.0,
                positions={COIN: VenuePosition(coin=COIN, size=0.01, entry_px=3000.0)},
            )
        )
        before = len(venue.placed)

        await session.close_out(START + 1_000)

        assert len(venue.cancelled) == 1
        assert len(venue.placed) == before, "no closing order: the bracket has the position"
    finally:
        await session._store.close()


def test_the_testnet_unit_isolates_every_file_the_paper_loop_owns() -> None:
    """Two loops on one box. Anything shared is a collision waiting for the drill day.

    The control server takes an exclusive flock per socket, one heartbeat would leave the
    watchdog unable to say which loop stopped, one registry would mix simulated orders
    with venue ones, and one risk_state.json would let paper's daily loss halt testnet.
    """
    unit = Path("scripts/systemd/liqfade-testnet.service").read_text()
    paper = Path("scripts/systemd/liqfade-paper.service").read_text()

    for key in (
        "LIQFADE_CONTROL__SOCKET_PATH",
        "LIQFADE_CONTROL__STATE_FILE",
        "LIQFADE_CONTROL__HEARTBEAT_FILE",
        "LIQFADE_CONTROL__RISK_STATE_FILE",
        "LIQFADE_EXECUTION__ORDER_DB",
    ):
        mine = [ln for ln in unit.splitlines() if ln.startswith(f"Environment={key}=")]
        assert mine, f"the testnet unit must set {key}"
        assert mine[0] not in paper, f"{key} collides with the paper loop"

    # One flag kills both, which is the whole point of it being one flag.
    assert "test ! -e /srv/liqfade/data/KILL" in unit
    # Never armed for live trading here.
    assert "Environment=LIQFADE_LIVE_TRADING" not in unit


def test_the_drill_command_is_refused_on_mainnet(tmp_path: Path) -> None:
    """inject_intent opens a position with no signal behind it. Not on real money."""
    import asyncio

    session, _venue = asyncio.run(a_session(tmp_path))
    try:
        object.__setattr__(session.settings, "env", "mainnet")
        with pytest.raises(PermissionError, match="mainnet"):
            session.inject_intent(coin=COIN, side="long", now_ms=START)
    finally:
        asyncio.run(session._store.close())


def test_only_one_loop_polls_telegram_for_commands() -> None:
    """Two long polls on one bot token is a 409, and neither side gets the commands.

    Telegram allows one `getUpdates` consumer per token. Paper soaks for a week and owns
    the channel; the testnet loop sends alerts and takes commands over its own control
    socket instead. The one command that must reach both is `/kill`, and it does: it
    writes data/KILL, which both loops watch, which is the point of it being one flag.
    """
    unit = Path("scripts/systemd/liqfade-testnet.service").read_text()
    assert "Environment=LIQFADE_TELEGRAM__COMMANDS=0" in unit

    from liqfade.config import load_settings

    settings = load_settings("testnet", environ={"LIQFADE_TELEGRAM__COMMANDS": "0"})
    assert settings.telegram.commands is False
    assert load_settings("paper", environ={}).telegram.commands is True


async def test_a_coin_the_venue_has_no_rules_for_is_not_ordered(tmp_path: Path) -> None:
    """A guessed szDecimals is how an order gets filled at the wrong size."""
    session, venue = await a_session(tmp_path)
    try:
        session.books["DOGE"] = session.books.get(COIN, None) or None
        quote(session, START, 2999.0, 3001.0)
        intent = an_intent("long")
        object.__setattr__(intent, "coin", "DOGE")

        assert await session.place(intent, START, Row()) is None
        assert venue.placed == []
    finally:
        await session._store.close()


async def test_a_partly_filled_entry_brackets_the_whole_position(tmp_path: Path) -> None:
    """Found by drilling on testnet: a 0.0076 position wearing a 0.0019 bracket.

    The entry filled in four parts. `on_fill` bracketed the first and then skipped the
    rest because the coin already had a bracket, so three quarters of the position had no
    stop behind it. That is the exact failure the tpsl module warns about: a position that
    looks protected and is not is worse than one that plainly is not.
    """
    session, venue = await a_session(tmp_path)
    try:
        quote(session, START, 2999.0, 3001.0)
        cloid = await session.place(an_intent("long"), START, Row())
        assert cloid is not None

        for i in range(4):
            await session.on_fill(cloid, px=2999.0, size=0.0019, ts_ms=START + i)

        assert len(venue.groups) >= 1
        legs = venue.groups[-1]
        assert all(leg["sz"] == pytest.approx(0.0076) for leg in legs), (
            "the last bracket must cover every fill, not the first one"
        )
        # The superseded legs are cancelled, or the venue holds two brackets for one
        # position and the second fill to trigger closes a position that is already flat.
        assert len(venue.cancelled) == len(venue.groups) * 2 - 2
    finally:
        await session._store.close()


async def test_an_urgent_entry_crosses_the_spread_instead_of_resting_on_it(
    tmp_path: Path,
) -> None:
    """Drilled on testnet: "Order could not immediately match against any resting orders."

    A high urgency entry is an `Ioc`, and an `Ioc` buy priced at the bid cannot match
    anything. It has to reach through the offer by `execution.taker_slippage_bps`, which
    is the same bound `close_position` crosses by. Priced at the touch, every urgent entry
    was refused, so the loop would have taken exactly the trades the strategy considers
    least worth chasing and refused the ones it considers most urgent.
    """
    session, venue = await a_session(tmp_path)
    try:
        quote(session, START, 2999.0, 3001.0)
        await session.place(an_intent("long", urgency="high"), START, Row())

        (order,) = venue.placed
        assert order.tif == "Ioc"
        assert order.px > 3001.0, "a taker buy must reach through the offer"
        bound = 1 + session.settings.execution.taker_slippage_bps / 1e4
        assert order.px == pytest.approx(3001.0 * bound, rel=1e-6)
    finally:
        await session._store.close()


async def test_an_urgent_short_crosses_down_through_the_bid(tmp_path: Path) -> None:
    session, venue = await a_session(tmp_path)
    try:
        quote(session, START, 2999.0, 3001.0)
        await session.place(an_intent("short", urgency="high"), START, Row())

        (order,) = venue.placed
        assert order.px < 2999.0
        bound = 1 - session.settings.execution.taker_slippage_bps / 1e4
        assert order.px == pytest.approx(2999.0 * bound, rel=1e-6)
    finally:
        await session._store.close()


async def test_a_fill_that_beats_the_submit_return_still_brackets(tmp_path: Path) -> None:
    """Drilled on testnet: two positions with no bracket at all.

    An `Ioc` fills at the venue in a few hundred milliseconds and the user stream pushes
    the fill straight back, while `submit` is still waiting on its own HTTP response. The
    intent was recorded in `entry_intents` *after* submit returned, so the fill arrived
    for a cloid the session did not yet recognise, `on_fill` returned silently, and the
    position ended up with no stop behind it. The faster the venue, the more likely.
    """
    session, venue = await a_session(tmp_path)
    try:
        quote(session, START, 2999.0, 3001.0)
        intent = an_intent("long", urgency="high")

        # The transport fills and pushes the fill back before it returns the oid.
        async def place_and_fill(order_intent):
            await session.on_fill(order_intent.cloid, px=3001.0, size=0.01, ts_ms=START)
            venue.placed.append(order_intent)
            return 4242

        venue.place = place_and_fill
        await session.place(intent, START, Row())

        assert len(venue.groups) == 1, "the fill must find its intent, however early it lands"
    finally:
        await session._store.close()
