"""The risk engine's hooks on the venue path: every gate has to be able to fire on testnet.

`tests/test_risk.py` proves the gates. This proves the live order path feeds them, which is
the other half and the half that was missing: an entry the venue took is recorded for the
cluster cap, an exit it reported moves the day's PnL and the streak, a reject is counted, a
reconnect is counted, and equity is marked so the drawdown peak tracks a gain. A gate that
nothing updates is a limit that cannot fire, and it looks exactly like a limit that is not
being hit.

The seam is `VenueSession` through the fake transport, same as `tests/test_live_venue.py`.
Fills arrive the way `UserStream.drain` delivers them: `record_fill` into the registry
first, then the session's hook.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from liqfade.execution.hl import AccountSnapshot
from liqfade.execution.orders import RejectError
from liqfade.live.venue import VenueSession
from liqfade.strategy.types import Intent
from tests.test_live_loop import paper_settings
from tests.test_live_venue import COIN, START, Row, a_session, an_intent, quote

EQUITY = 1000.0  # what `a_session` starts with


def an_intent_for(coin: str, side: str = "long", *, px: float) -> Intent:
    """`an_intent` for a second coin, which the cluster cap needs."""
    direction = 1.0 if side == "long" else -1.0
    return Intent(
        coin=coin,
        side=side,  # type: ignore[arg-type]
        entry_px=px,
        target_px=px * (1 + direction * 0.003),
        stop_px=px * (1 - direction * 0.003),
        time_stop_s=600.0,
        urgency="normal",
        ts_ms=START,
        explain={},
    )


def quote_coin(session: VenueSession, coin: str, ts: int, bid: float, ask: float) -> None:
    session.ingest(
        "bbo",
        "hyperliquid",
        {
            "coin": coin,
            "ts_ms": ts,
            "rx_ms": ts,
            "bid_px": bid,
            "bid_sz": 1000.0,
            "ask_px": ask,
            "ask_sz": 1000.0,
        },
    )


async def a_fill(session: VenueSession, cloid: str, *, px: float, size: float, ts_ms: int) -> None:
    """One fill, delivered the way `UserStream.drain` delivers it: registry, then hook."""
    await session.orders.record_fill(cloid, px=px, size=size, ts_ms=ts_ms)
    await session.on_fill(cloid, px=px, size=size, ts_ms=ts_ms)


async def an_exit_fill(
    session: VenueSession, cloid: str, *, px: float, size: float, pnl: float, ts_ms: int
) -> None:
    """A bracket leg filling, carrying the venue's own `closedPnl` for the round trip."""
    await session.orders.record_fill(cloid, px=px, size=size, ts_ms=ts_ms, closed_pnl=pnl)
    await session.on_fill(cloid, px=px, size=size, ts_ms=ts_ms)


async def a_round_trip(session: VenueSession, *, pnl: float, at_ms: int = START) -> None:
    """Enter, fill, and let the stop fire for `pnl`. One closed round trip on the venue path."""
    quote_coin(session, COIN, at_ms, 2999.0, 3001.0)
    cloid = await session.place(an_intent("long"), at_ms, Row())
    assert cloid is not None
    size = session.orders.get(cloid).intent.size  # the whole entry, so it stops working
    await a_fill(session, cloid, px=2999.0, size=size, ts_ms=at_ms + 10)
    legs = session.brackets[COIN]
    await an_exit_fill(session, legs.sl_cloid, px=2900.0, size=size, pnl=pnl, ts_ms=at_ms + 20)


async def test_an_entry_the_venue_took_is_recorded_against_the_cluster_cap(
    tmp_path: Path,
) -> None:
    """`on_entry`, without which the cluster cap counts nothing and never refuses.

    The cap is the one gate that only sees entries the bot actually placed, so on the venue
    path it was permanently at zero: forty coins faded on one sweep would all have passed.
    """
    session, _venue = await a_session(tmp_path)
    try:
        quote(session, START, 2999.0, 3001.0)
        cloid = await session.place(an_intent("long"), START, Row())
        assert cloid is not None
        placed = session.orders.get(cloid).intent
        notional = placed.px * placed.size

        entries = session.risk.state.entries
        # Sizing's own notional, which is the rounded size against the intent's entry
        # price rather than against the touch the order rested at.
        assert [entry.notional_usd for entry in entries] == [pytest.approx(notional, rel=1e-3)]
        assert entries[0].side == "long"

        # A cap that fits one of these entries and not two. Swapped in the way
        # `reload_config` swaps one, so the second entry meets a cap it must exceed.
        session.risk.settings = paper_settings(
            tmp_path, **{"risk.max_cluster_exposure_frac": 1.5 * notional / EQUITY}
        )
        session.orders.sz_decimals["SOL"] = 2
        quote_coin(session, "SOL", START + 100, 199.0, 201.0)

        second = await session.place(an_intent_for("SOL", px=200.0), START + 100, Row())

        assert second is None
        assert session.result.risk_blocks == {"cluster_exposure": 1}
    finally:
        await session._store.close()


async def test_the_drawdown_peak_tracks_a_gain_on_the_venue_path(tmp_path: Path) -> None:
    """`on_equity`, without which the peak stays at the equity the process started with.

    1050 is not a drawdown from 1000 and is one from 1200. The block only happens if the
    gain was marked, which is what nothing on this path was doing. The cluster cap is
    lifted so that three entries in a row meet the gate this test is about.
    """
    session, _venue = await a_session(tmp_path, **{"risk.max_cluster_exposure_frac": 1.0})
    try:
        quote(session, START, 2999.0, 3001.0)
        first = await session.place(an_intent("long"), START, Row())
        assert first is not None
        await session.orders.cancel(first)  # so the next entry is not "already working"

        session.sync_account(
            AccountSnapshot(equity=1200.0, margin_used=0.0, withdrawable=1200.0, positions={})
        )
        second = await session.place(an_intent("long"), START + 1_000, Row())
        assert second is not None
        await session.orders.cancel(second)
        assert session.risk.state.peak_equity == 1200.0

        session.sync_account(
            AccountSnapshot(equity=1050.0, margin_used=0.0, withdrawable=1050.0, positions={})
        )
        third = await session.place(an_intent("long"), START + 2_000, Row())

        assert third is None
        assert session.result.risk_blocks == {"drawdown": 1}
        assert session.risk.state.halt_reason == "drawdown"
    finally:
        await session._store.close()


async def test_a_losing_exit_the_venue_reported_spends_the_daily_budget(tmp_path: Path) -> None:
    """`on_exit`, without which the daily loss limit never sees a loss.

    The PnL is the venue's `closedPnl` off the fill that closed the position, which is what
    `VenueTrade` documents: the venue charged the fees and matched the entry.
    """
    session, _venue = await a_session(tmp_path)
    try:
        await a_round_trip(session, pnl=-50.0)  # 5% of equity, over the 3% daily budget

        assert session.risk.state.daily_realized_pnl == pytest.approx(-50.0)
        assert [trade.net_pnl for trade in session.portfolio.trades] == [pytest.approx(-50.0)]

        quote(session, START + 30, 2899.0, 2901.0)
        blocked = await session.place(an_intent("long"), START + 30, Row())

        assert blocked is None
        assert session.result.risk_blocks == {"daily_loss": 1}
    finally:
        await session._store.close()


async def test_a_losing_exit_counts_towards_the_streak_halt(tmp_path: Path) -> None:
    session, _venue = await a_session(tmp_path, **{"risk.max_consecutive_losses": 1})
    try:
        await a_round_trip(session, pnl=-1.0)

        assert session.risk.state.consecutive_losses == 1
        quote(session, START + 30, 2899.0, 2901.0)
        blocked = await session.place(an_intent("long"), START + 30, Row())

        assert blocked is None
        assert session.result.risk_blocks == {"losing_streak": 1}
    finally:
        await session._store.close()


async def test_a_coin_just_exited_is_on_cooldown_on_the_venue_path(tmp_path: Path) -> None:
    """A winning exit still gets the short cooldown: the same cascade is not a new trade."""
    session, _venue = await a_session(tmp_path)
    try:
        await a_round_trip(session, pnl=10.0)

        assert session.risk.state.consecutive_losses == 0
        quote(session, START + 30, 2899.0, 2901.0)
        blocked = await session.place(an_intent("long"), START + 30, Row())

        assert blocked is None
        assert session.result.risk_blocks == {"coin_cooldown": 1}
    finally:
        await session._store.close()


async def test_rejects_from_the_venue_halt_entries(tmp_path: Path) -> None:
    """`on_reject`. Two refusals against a limit of one, because the gate is a `>`."""
    session, venue = await a_session(
        tmp_path,
        # The cluster cap is lifted so the second refusal is a refusal by the venue
        # rather than a gate that never let the order out.
        **{"risk.max_rejects_10min": 1, "risk.max_cluster_exposure_frac": 1.0},
    )
    try:

        async def refuse(intent: object) -> int:
            raise RejectError("Insufficient margin to place order")

        venue.place = refuse  # type: ignore[assignment]
        quote(session, START, 2999.0, 3001.0)
        assert await session.place(an_intent("long"), START, Row()) is None
        assert await session.place(an_intent("long"), START + 10, Row()) is None
        assert len(session.risk.state.reject_ms) == 2

        blocked = await session.place(an_intent("long"), START + 20, Row())

        assert blocked is None
        assert session.result.risk_blocks == {"reject_storm": 1}
    finally:
        await session._store.close()


async def test_a_reconnect_storm_halts_entries_on_the_venue_path(tmp_path: Path) -> None:
    """`on_reconnect`. A feed that keeps dropping is a feed the strategy cannot trust."""
    session, _venue = await a_session(tmp_path, **{"risk.max_reconnects_10min": 1})
    try:
        session.on_reconnect(START)
        session.on_reconnect(START + 1)

        quote(session, START + 10, 2999.0, 3001.0)
        blocked = await session.place(an_intent("long"), START + 10, Row())

        assert blocked is None
        assert session.result.risk_blocks == {"reconnect_storm": 1}
    finally:
        await session._store.close()
