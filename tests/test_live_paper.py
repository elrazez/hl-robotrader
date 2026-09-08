"""The paper session: the backtest engine on a live clock, with real orders tracked.

The seam is `PaperSession`: rows in, ticks in, positions and simulated fills out. Every
test drives it exactly as the loop does, so what passes here is what runs on the box.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from liqfade.config import resolve
from liqfade.config.models import Settings
from liqfade.execution.orders import OrderStatus, OrderStore
from liqfade.live.paper import PaperSession
from liqfade.strategy.types import AssetMeta

START = 1_700_000_000_000
COIN = "ETH"


def paper_settings(**dotted: Any) -> Settings:
    merged, _ = resolve("paper")
    merged["cascade"]["trigger"] = "crossvenue"
    for key, value in dotted.items():
        node = merged
        parts = key.split(".")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
    return Settings.model_validate(merged)


_OPENED: list[PaperSession] = []


@pytest.fixture(autouse=True)
async def close_sessions() -> AsyncIterator[None]:
    """Close every session a test opened: aiosqlite runs a worker thread per connection."""
    yield
    while _OPENED:
        await _OPENED.pop().aclose()


async def a_session(tmp_path: Path, settings: Settings | None = None) -> PaperSession:
    settings = settings or paper_settings()
    store = await OrderStore.open(tmp_path / "orders.sqlite")
    session = PaperSession(
        settings,
        store=store,
        meta={
            COIN: AssetMeta(
                coin=COIN, sz_decimals=4, max_leverage=25.0, tier="core", listing_days=400.0
            )
        },
        starting_equity=10_000.0,
    )
    _OPENED.append(session)
    return session


def quote(session: PaperSession, ts: int, px: float, size: float = 50.0) -> None:
    session.ingest(
        "bbo",
        "hyperliquid",
        {
            "coin": COIN,
            "ts_ms": ts,
            "rx_ms": ts,
            "bid_px": px - 0.5,
            "bid_sz": size,
            "ask_px": px + 0.5,
            "ask_sz": size,
        },
    )


def print_trade(
    session: PaperSession, ts: int, px: float, sz: float = 5.0, side: str = "sell"
) -> None:
    session.ingest(
        "trades",
        "hyperliquid",
        {"coin": COIN, "ts_ms": ts, "rx_ms": ts, "px": px, "sz": sz, "side": side, "tid": ts},
    )


async def test_an_injected_intent_becomes_a_resting_order_with_a_cloid(tmp_path: Path) -> None:
    """The drill path: no cascade needed, and every layer below the strategy still runs."""
    session = await a_session(tmp_path)
    quote(session, START, 3000.0)
    print_trade(session, START, 3000.0)
    await session.tick(START + 1_000)

    intent = session.inject_intent(coin=COIN, side="long", now_ms=START + 1_000)
    await session.tick(START + 1_250)

    assert intent is not None
    orders = await session.store.open_orders()
    assert len(orders) == 1
    assert orders[0].cloid.startswith("0x")
    assert orders[0].coin == COIN
    assert orders[0].status in {OrderStatus.ACKED, OrderStatus.PARTIAL}


async def test_a_resting_entry_fills_against_a_print_and_opens_a_position(
    tmp_path: Path,
) -> None:
    session = await a_session(tmp_path)
    quote(session, START, 3000.0)
    print_trade(session, START, 3000.0)
    await session.tick(START + 1_000)
    session.inject_intent(coin=COIN, side="long", now_ms=START + 1_000)
    await session.tick(START + 1_250)

    # Past the latency, then a seller trades through our resting bid.
    later = START + 1_000 + session.settings.backtest.latency_ms + 500
    print_trade(session, later, 2990.0, sz=50.0, side="sell")
    await session.tick(later + 250)

    assert COIN in session.portfolio.positions
    assert session.fills, "the fill should have been recorded"
    filled = [
        order for order in await session.store.all_orders() if order.status is OrderStatus.FILLED
    ]
    assert filled, "the filled entry should be persisted"


async def test_the_stop_exits_the_position_and_books_a_trade(tmp_path: Path) -> None:
    session = await a_session(tmp_path)
    quote(session, START, 3000.0)
    print_trade(session, START, 3000.0)
    await session.tick(START + 1_000)
    session.inject_intent(coin=COIN, side="long", now_ms=START + 1_000)
    await session.tick(START + 1_250)
    later = START + 1_000 + session.settings.backtest.latency_ms + 500
    print_trade(session, later, 2990.0, sz=50.0, side="sell")
    await session.tick(later + 250)
    assert COIN in session.portfolio.positions

    # Now collapse the market well through the stop.
    crash = later + 5_000
    quote(session, crash, 2000.0)
    print_trade(session, crash, 2000.0)
    await session.tick(crash + 250)

    assert COIN not in session.portfolio.positions
    assert session.portfolio.trades, "a completed round trip should be booked"
    assert session.portfolio.trades[-1].exit_reason == "stop"


async def test_flatten_closes_everything_and_cancels_the_resting_orders(
    tmp_path: Path,
) -> None:
    """What /flat and the kill switch both call."""
    session = await a_session(tmp_path)
    quote(session, START, 3000.0)
    print_trade(session, START, 3000.0)
    await session.tick(START + 1_000)
    session.inject_intent(coin=COIN, side="long", now_ms=START + 1_000)
    await session.tick(START + 1_250)
    later = START + 1_000 + session.settings.backtest.latency_ms + 500
    print_trade(session, later, 2990.0, sz=50.0, side="sell")
    await session.tick(later + 250)

    closed = await session.flatten(now_ms=later + 500)

    assert closed == [COIN]
    assert session.portfolio.positions == {}
    assert await session.open_order_cloids() == []


async def test_cancel_all_leaves_nothing_working(tmp_path: Path) -> None:
    session = await a_session(tmp_path)
    quote(session, START, 3000.0)
    print_trade(session, START, 3000.0)
    await session.tick(START + 1_000)
    session.inject_intent(coin=COIN, side="long", now_ms=START + 1_000)
    await session.tick(START + 1_250)
    assert await session.open_order_cloids()

    await session.cancel_all()

    assert await session.open_order_cloids() == []
    cancelled = [
        order for order in await session.store.all_orders() if order.status is OrderStatus.CANCELED
    ]
    assert cancelled


async def test_inject_is_refused_on_mainnet() -> None:
    """A drill command that can open a real position is not a drill command."""
    merged, _ = resolve("mainnet")
    settings = Settings.model_validate(merged)
    session = PaperSession(settings, store=None, meta={}, starting_equity=1000.0)

    with pytest.raises(PermissionError, match="mainnet"):
        session.inject_intent(coin=COIN, side="long", now_ms=START)


async def test_inject_needs_a_price_it_can_actually_see(tmp_path: Path) -> None:
    """No book, no intent. Inventing an entry price would be inventing the drill."""
    session = await a_session(tmp_path)
    assert session.inject_intent(coin=COIN, side="long", now_ms=START) is None


async def test_the_snapshot_reports_positions_and_orders(tmp_path: Path) -> None:
    session = await a_session(tmp_path)
    quote(session, START, 3000.0)
    print_trade(session, START, 3000.0)
    await session.tick(START + 1_000)
    session.inject_intent(coin=COIN, side="long", now_ms=START + 1_000)
    await session.tick(START + 1_250)
    later = START + 1_000 + session.settings.backtest.latency_ms + 500
    print_trade(session, later, 2990.0, sz=50.0, side="sell")
    await session.tick(later + 250)

    positions, orders = session.snapshot()

    assert [position.coin for position in positions] == [COIN]
    assert positions[0].side == "long"
    assert positions[0].notional_usd > 0
    assert isinstance(orders, list)


async def test_equity_falls_with_the_fees_a_round_trip_pays(tmp_path: Path) -> None:
    """Paper is not free. If it were, its numbers would not be comparable to a backtest."""
    session = await a_session(tmp_path)
    quote(session, START, 3000.0)
    print_trade(session, START, 3000.0)
    await session.tick(START + 1_000)
    session.inject_intent(coin=COIN, side="long", now_ms=START + 1_000)
    await session.tick(START + 1_250)
    later = START + 1_000 + session.settings.backtest.latency_ms + 500
    print_trade(session, later, 2990.0, sz=50.0, side="sell")
    await session.tick(later + 250)
    await session.flatten(now_ms=later + 1_000)

    trade = session.portfolio.trades[-1]
    assert trade.fees_usd > 0
    assert trade.net_pnl < trade.gross_pnl


async def test_a_liquidation_with_no_notional_reaches_neither_the_book_nor_the_features(
    tmp_path: Path,
) -> None:
    """The box found this one: OKX sends a few an hour and they raised on every one.

    The feature engine had been fixed and this path had not, because the edit was written
    against a line ruff had already wrapped and nothing asserted that it matched. The loop
    logged paper_ingest_failed every time instead of ingesting anything.
    """
    session = await a_session(tmp_path)
    row = {
        "coin": COIN,
        "ts_ms": START,
        "rx_ms": START,
        "side": "long",
        "notional_usd": None,
        "px": None,
    }

    import structlog

    with structlog.testing.capture_logs() as entries:
        session.ingest("liquidations", "okx", row)

    assert [entry for entry in entries if "failed" in entry["event"]] == []
    session.ingest("liquidations", "okx", {**row, "notional_usd": 9_000.0, "px": 100.0})
    await session.tick(START + 1_000)
    assert session.features.row(COIN, START + 1_000).liq_long_usd_w == pytest.approx(9_000.0)


async def test_an_abandoned_entry_is_cancelled_in_the_registry_not_filled(tmp_path: Path) -> None:
    """The entry policy's terminal action must not look like a fill.

    `_expire_entries` abandons an entry past `max_reprices` by dropping it from the coin
    state. `sync_orders` reads "gone from the coin state and not cancelled" as "filled the
    rest of the way", which is right for an order the fill model consumed and wrong for
    one the policy gave up on. Recording a phantom fill puts a position in the registry
    that the portfolio does not have, which is the exact disagreement reconciliation
    exists to catch and would report forever.
    """
    settings = paper_settings(**{"execution.entry_ttl_s": 1.0, "execution.max_reprices": 0})
    session = await a_session(tmp_path, settings)
    quote(session, START, 3000.0)
    print_trade(session, START, 3000.0)
    await session.tick(START + 1_000)
    assert session.inject_intent(coin=COIN, side="long", now_ms=START + 1_000) is not None
    await session.tick(START + 1_250)

    entry = session.state[COIN].entry
    assert entry is not None, "the drill should have rested an entry"
    cloid = entry.cloid

    # Past the TTL with no reprices left: the policy abandons it.
    later = START + 10_000
    quote(session, later, 3000.0)
    await session.tick(later)

    assert session.state[COIN].entry is None, "the policy should have abandoned it"
    order = session.orders.get(cloid)
    assert order.status is OrderStatus.CANCELED, f"abandoned, not filled (was {order.status})"
    assert order.filled == 0.0


async def test_a_refused_intent_is_written_to_the_registry_and_the_engine_is_drained(
    tmp_path: Path,
) -> None:
    """The leak this closes: `_rejections` grew for the life of the process and was
    written nowhere, and a sizing refusal dropped the intent that carried the prices.

    Driven through `inject_intent` because it is the real path: the drill intent goes
    through sizing and the risk gate exactly as the strategy's own do.
    """
    session = await a_session(tmp_path)
    quote(session, START, 2000.0)

    assert session.inject_intent(coin=COIN, side="long", now_ms=START + 1_000) is not None
    assert session.result.refused, "sizing refused it but the intent was not kept"
    await session.tick(START + 2_000)

    assert not session.result.refused, "the tick did not drain what sizing refused"
    assert session.strategy.take_rejections() == []
    assert session.store is not None
    rows = await session.store.refusals(0, START + 10_000)
    # `vol_cap` on this fixture: nothing has traded, so the first cap to bind is the
    # participation one. Which cap binds is sizing's business; what this pins is that the
    # refusal reached the registry with the prices attached.
    assert [(row["stage"], row["coin"]) for row in rows] == [("sizing", COIN)]
    assert rows[0]["reason"]
    assert rows[0]["entry_px"] > 0
    assert rows[0]["stop_px"] > 0


async def test_a_session_with_no_store_drains_and_discards(tmp_path: Path) -> None:
    """Persisting is the second thing the drain does; bounding the list is the first."""
    session = PaperSession(
        paper_settings(),
        store=None,
        meta={
            COIN: AssetMeta(
                coin=COIN, sz_decimals=4, max_leverage=25.0, tier="core", listing_days=400.0
            )
        },
        starting_equity=10_000.0,
    )
    _OPENED.append(session)
    quote(session, START, 2000.0)
    session.inject_intent(coin=COIN, side="long", now_ms=START + 1_000)
    assert session.result.refused

    await session.tick(START + 2_000)

    assert not session.result.refused
