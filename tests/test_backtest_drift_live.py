"""Reading the live side of the diff back out of the order registry.

The registry is written by `OrderManager`, which is the writer `PaperSession` uses for
every order it simulates, so these tests drive the same path the soak drives rather than
inserting rows by hand. What comes back has to be the entry attempts, the completed round
trips and the refusals, keyed the way the replay's own output is keyed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from liqfade.backtest.drift import read_live
from liqfade.execution.orders import (
    OrderIntent,
    OrderManager,
    OrderStore,
    new_cloid,
    now_ms,
)
from liqfade.strategy.engine import Rejection

# `OrderManager.submit` stamps `submitted_ms` off the real clock, and `read_live`
# windows on it, so the window here has to be the one the orders actually land in.
START = now_ms() - 1_000
DAY_MS = 86_400_000
SZ_DECIMALS = {"ETH": 4, "BTC": 5}


class Transport:
    """Accepts everything and hands back an oid, as the paper transport does."""

    def __init__(self) -> None:
        self.next_oid = 1000

    async def place(self, intent: OrderIntent) -> int:
        self.next_oid += 1
        return self.next_oid

    async def cancel(self, coin: str, cloid: str) -> None:
        return None


async def a_store(tmp_path: Path) -> tuple[OrderStore, OrderManager]:
    store = await OrderStore.open(tmp_path / "orders.sqlite")
    manager = OrderManager(store, Transport(), sz_decimals=SZ_DECIMALS, killed=lambda: False)
    return store, manager


async def place(
    manager: OrderManager,
    *,
    coin: str = "ETH",
    is_buy: bool = True,
    px: float = 100.0,
    size: float = 1.0,
    tag: str = "entry",
    fill_ms: int | None = None,
    fill_px: float | None = None,
    fee: float = 0.045,
) -> str:
    """Submit one order and, unless `fill_ms` is None, fill it."""
    cloid = new_cloid()
    await manager.submit(
        OrderIntent(
            cloid=cloid,
            coin=coin,
            is_buy=is_buy,
            px=px,
            size=size,
            tif="Alo" if tag == "entry" else "Ioc",
            reduce_only=tag != "entry",
            tag=tag,
        )
    )
    if fill_ms is not None:
        await manager.record_fill(
            cloid,
            size=size,
            px=fill_px if fill_px is not None else px,
            ts_ms=fill_ms,
            fee=fee,
            maker=tag == "entry",
        )
    return cloid


@pytest.mark.asyncio
async def test_an_entry_order_comes_back_as_an_intent_with_its_fill_price(
    tmp_path: Path,
) -> None:
    store, manager = await a_store(tmp_path)
    await place(manager, fill_ms=START + 1_000, fill_px=100.5)
    side = await read_live(store, START, START + DAY_MS)
    await store.close()

    assert len(side.intents) == 1
    assert side.intents[0].coin == "ETH"
    assert side.intents[0].side == "long"
    assert side.intents[0].entry_px == pytest.approx(100.5)


@pytest.mark.asyncio
async def test_a_sell_entry_is_read_as_a_short_not_as_the_wire_direction(
    tmp_path: Path,
) -> None:
    store, manager = await a_store(tmp_path)
    await place(manager, is_buy=False, fill_ms=START + 1_000)
    side = await read_live(store, START, START + DAY_MS)
    await store.close()
    assert side.intents[0].side == "short"


@pytest.mark.asyncio
async def test_an_entry_that_never_filled_has_no_entry_price_rather_than_a_zero(
    tmp_path: Path,
) -> None:
    """A price the loop never got is absent. Zero would be diffed as a 10,000 bps gap."""
    store, manager = await a_store(tmp_path)
    await place(manager, fill_ms=None)
    side = await read_live(store, START, START + DAY_MS)
    await store.close()
    assert len(side.intents) == 1
    assert side.intents[0].entry_px is None


@pytest.mark.asyncio
async def test_an_exit_leg_is_never_read_as_an_entry_attempt(tmp_path: Path) -> None:
    store, manager = await a_store(tmp_path)
    await place(manager, fill_ms=START + 1_000)
    await place(manager, is_buy=False, tag="target", px=101.0, fill_ms=START + 60_000)
    side = await read_live(store, START, START + DAY_MS)
    await store.close()
    assert len(side.intents) == 1


@pytest.mark.asyncio
async def test_an_entry_and_the_exit_that_closed_it_become_one_round_trip(
    tmp_path: Path,
) -> None:
    store, manager = await a_store(tmp_path)
    await place(manager, px=100.0, fill_ms=START + 1_000, fee=0.045)
    await place(manager, is_buy=False, tag="target", px=101.0, fill_ms=START + 61_000, fee=0.045)
    side = await read_live(store, START, START + DAY_MS)
    await store.close()

    assert len(side.round_trips) == 1
    trip = side.round_trips[0]
    assert trip.exit_reason == "target"
    assert trip.entry_px == pytest.approx(100.0)
    assert trip.exit_px == pytest.approx(101.0)
    assert trip.notional == pytest.approx(100.0)
    # One dollar of move on one unit, less the fee recorded on each leg.
    assert trip.net_pnl == pytest.approx(1.0 - 0.045 - 0.045)


@pytest.mark.asyncio
async def test_a_short_round_trip_earns_when_the_price_falls(tmp_path: Path) -> None:
    store, manager = await a_store(tmp_path)
    await place(manager, is_buy=False, px=100.0, fill_ms=START + 1_000, fee=0.0)
    await place(manager, is_buy=True, tag="stop", px=99.0, fill_ms=START + 61_000, fee=0.0)
    side = await read_live(store, START, START + DAY_MS)
    await store.close()
    assert side.round_trips[0].net_pnl == pytest.approx(1.0)
    assert side.round_trips[0].side == "short"


@pytest.mark.asyncio
async def test_a_resting_exit_leg_that_never_filled_closes_nothing(tmp_path: Path) -> None:
    """A bracket registers both legs at entry. Only the one that filled ended the trade."""
    store, manager = await a_store(tmp_path)
    await place(manager, fill_ms=START + 1_000)
    await place(manager, is_buy=False, tag="stop", px=99.0, fill_ms=None)
    side = await read_live(store, START, START + DAY_MS)
    await store.close()
    assert side.round_trips == []


@pytest.mark.asyncio
async def test_an_exit_is_paired_with_the_entry_it_followed_not_with_a_later_one(
    tmp_path: Path,
) -> None:
    store, manager = await a_store(tmp_path)
    await place(manager, px=100.0, fill_ms=START + 1_000)
    await place(manager, is_buy=False, tag="target", px=101.0, fill_ms=START + 61_000)
    await place(manager, px=200.0, fill_ms=START + 600_000)
    await place(manager, is_buy=False, tag="stop", px=199.0, fill_ms=START + 660_000)
    side = await read_live(store, START, START + DAY_MS)
    await store.close()

    assert [t.exit_reason for t in side.round_trips] == ["target", "stop"]
    assert [t.entry_px for t in side.round_trips] == [100.0, 200.0]


@pytest.mark.asyncio
async def test_an_exit_on_one_coin_never_closes_a_position_on_another(
    tmp_path: Path,
) -> None:
    store, manager = await a_store(tmp_path)
    await place(manager, coin="ETH", fill_ms=START + 1_000)
    await place(manager, coin="BTC", is_buy=False, tag="target", fill_ms=START + 61_000)
    side = await read_live(store, START, START + DAY_MS)
    await store.close()
    assert side.round_trips == []


@pytest.mark.asyncio
async def test_only_what_the_window_holds_comes_back(tmp_path: Path) -> None:
    store, manager = await a_store(tmp_path)
    await place(manager, fill_ms=START + 1_000)
    side = await read_live(store, START + DAY_MS, START + 2 * DAY_MS)
    await store.close()
    assert side.intents == []
    assert side.round_trips == []


@pytest.mark.asyncio
async def test_refusals_come_back_keyed_by_stage_and_reason(tmp_path: Path) -> None:
    store, _ = await a_store(tmp_path)
    await store.add_refusals(
        [
            Rejection(
                coin="ETH",
                side="long",
                ts_ms=START + i,
                reason="spread_too_wide",
                stage="filter",
                entry_px=100.0,
                target_px=101.0,
                stop_px=99.0,
                time_stop_s=60.0,
                urgency="normal",
                explain={},
            )
            for i in range(3)
        ]
    )
    side = await read_live(store, START, START + DAY_MS)
    await store.close()
    assert side.refusals == {("filter", "spread_too_wide"): 3}


@pytest.mark.asyncio
async def test_a_refused_intent_is_an_intent_the_live_side_had(tmp_path: Path) -> None:
    """A refusal is an entry attempt that produced no order. The replay counts it as one."""
    store, _ = await a_store(tmp_path)
    await store.add_refusals(
        [
            Rejection(
                coin="ETH",
                side="long",
                ts_ms=START + 5_000,
                reason="daily_loss",
                stage="risk",
                entry_px=100.0,
                target_px=101.0,
                stop_px=99.0,
                time_stop_s=60.0,
                urgency="normal",
                explain={},
            )
        ]
    )
    side = await read_live(store, START, START + DAY_MS)
    await store.close()
    assert len(side.intents) == 1
    assert side.intents[0].stage == "risk"
    assert side.intents[0].reason == "daily_loss"
    assert side.intents[0].entry_px is None


@pytest.mark.asyncio
async def test_an_empty_registry_reads_as_an_empty_side_rather_than_raising(
    tmp_path: Path,
) -> None:
    store, _ = await a_store(tmp_path)
    side = await read_live(store, START, START + DAY_MS)
    await store.close()
    assert side.intents == []
    assert side.round_trips == []
    assert side.refusals == {}
    assert side.config_hash is None
