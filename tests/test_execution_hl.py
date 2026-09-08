"""Parsing what Hyperliquid sends back, which is where the offline bugs live.

Every payload here is the shape docs/HYPERLIQUID.md describes. When a testnet probe
disagrees with one of these, the probe wins and this file changes with it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from liqfade.execution.hl import (
    classify_order_response,
    parse_asset_meta,
    parse_clearinghouse_state,
    parse_open_orders,
    parse_user_fills,
)
from liqfade.execution.orders import RejectError, TransientTransportError


def ok(statuses: list[Any]) -> dict[str, Any]:
    return {"status": "ok", "response": {"type": "order", "data": {"statuses": statuses}}}


def test_a_resting_order_yields_its_oid() -> None:
    assert classify_order_response(ok([{"resting": {"oid": 77, "cloid": "0xabc"}}])) == 77


def test_an_immediately_filled_order_yields_its_oid() -> None:
    """An Ioc that filled on submit is an ack, not an error. Both carry an oid."""
    filled = {"filled": {"totalSz": "0.01", "avgPx": "3000.5", "oid": 91}}
    assert classify_order_response(ok([filled])) == 91


def test_a_status_level_error_is_a_reject_carrying_the_venue_string() -> None:
    response = ok([{"error": "Post only order would have immediately matched"}])
    with pytest.raises(RejectError, match="Post only order would have immediately matched"):
        classify_order_response(response)


def test_a_top_level_err_is_a_reject() -> None:
    response = {"status": "err", "response": "Insufficient margin to place order"}
    with pytest.raises(RejectError, match="Insufficient margin"):
        classify_order_response(response)


def test_the_cumulative_request_limit_is_a_reject_not_a_retry() -> None:
    """Retrying this one makes it worse: every attempt spends more of the same budget."""
    message = (
        "Too many cumulative requests sent. Place taker orders to free up 1 request per USDC traded"
    )
    with pytest.raises(RejectError, match="cumulative requests"):
        classify_order_response({"status": "err", "response": message})


def test_an_unrecognised_shape_fails_closed_as_a_reject() -> None:
    """Failing closed. An ambiguous answer retried in a loop is worse than one refusal."""
    with pytest.raises(RejectError):
        classify_order_response({"status": "ok", "response": {"type": "cancel", "data": {}}})


def test_an_empty_statuses_list_is_a_reject() -> None:
    with pytest.raises(RejectError):
        classify_order_response(ok([]))


def test_positions_parse_signed_with_equity_and_margin() -> None:
    state = {
        "marginSummary": {"accountValue": "1234.56", "totalMarginUsed": "100.0"},
        "withdrawable": "1100.0",
        "assetPositions": [
            {
                "position": {
                    "coin": "ETH",
                    "szi": "0.5",
                    "entryPx": "3000.0",
                    "unrealizedPnl": "12.5",
                    "positionValue": "1512.5",
                }
            },
            {
                "position": {
                    "coin": "SOL",
                    "szi": "-20.0",
                    "entryPx": "150.0",
                    "unrealizedPnl": "-3.0",
                    "positionValue": "2940.0",
                }
            },
        ],
    }
    account = parse_clearinghouse_state(state)

    assert account.equity == pytest.approx(1234.56)
    assert account.margin_used == pytest.approx(100.0)
    assert account.positions["ETH"].size == pytest.approx(0.5)
    assert account.positions["SOL"].size == pytest.approx(-20.0)
    assert account.positions["SOL"].entry_px == pytest.approx(150.0)


def test_a_zero_size_position_is_not_a_position() -> None:
    """HL leaves a flat coin in the list. Treating it as open invents a reconciliation gap."""
    state = {
        "marginSummary": {"accountValue": "10", "totalMarginUsed": "0"},
        "assetPositions": [
            {"position": {"coin": "ETH", "szi": "0.0", "entryPx": "0", "unrealizedPnl": "0"}}
        ],
    }
    assert parse_clearinghouse_state(state).positions == {}


def test_open_orders_parse_with_cloid_when_present() -> None:
    payload = [
        {
            "coin": "ETH",
            "oid": 1,
            "side": "B",
            "limitPx": "3000",
            "sz": "0.5",
            "timestamp": 1700000000000,
            "cloid": "0xdeadbeef",
        },
        {
            "coin": "SOL",
            "oid": 2,
            "side": "A",
            "limitPx": "150",
            "sz": "10",
            "timestamp": 1700000000001,
        },
    ]
    orders = parse_open_orders(payload)

    assert orders[0].cloid == "0xdeadbeef"
    assert orders[0].is_buy is True
    assert orders[0].size == pytest.approx(0.5)
    # An order placed by hand in the UI has no cloid at all. That is a real case and the
    # reconciler has to see it as an unknown order rather than crash on a missing key.
    assert orders[1].cloid is None
    assert orders[1].is_buy is False


def test_user_fills_parse_side_fee_and_liquidation_marker() -> None:
    payload = [
        {
            "coin": "ETH",
            "px": "3000",
            "sz": "0.5",
            "side": "B",
            "time": 1700000000000,
            "oid": 1,
            "cloid": "0xaa",
            "fee": "0.02",
            "crossed": True,
            "closedPnl": "0",
        },
        {
            "coin": "SOL",
            "px": "150",
            "sz": "10",
            "side": "A",
            "time": 1700000000001,
            "oid": 2,
            "fee": "0.1",
            "crossed": False,
            "closedPnl": "5.5",
            "liquidation": {"liquidatedUser": "0x1", "markPx": "150", "method": "market"},
        },
    ]
    fills = parse_user_fills(payload)

    assert fills[0].is_buy is True
    assert fills[0].maker is False  # crossed means we were the taker
    assert fills[1].maker is True
    assert fills[1].fee == pytest.approx(0.1)
    assert fills[1].liquidation is True


def test_a_transport_exception_is_transient_not_a_reject() -> None:
    """Only the venue's own words are a reject. A dropped socket may still have landed."""
    from liqfade.execution.hl import as_transport_error

    assert isinstance(as_transport_error(TimeoutError("read timeout")), TransientTransportError)
    assert isinstance(as_transport_error(ConnectionResetError()), TransientTransportError)


def test_a_post_only_rejection_arrives_inside_an_ok_envelope() -> None:
    """The exact body testnet returned on 2026-09-08, verbatim.

    HTTP 200 and `"status": "ok"` at the top, with the refusal buried in
    `response.data.statuses[0].error`. Code that branches on the envelope reads this as a
    successful order. `classify_order_response` must keep reading down to the per-status
    object, and it must raise `RejectError` rather than return an oid, because an oid is
    what the caller records a live order against.
    """
    response = {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {
                "statuses": [
                    {
                        "error": "Post only order would have immediately matched, "
                        "bbo was 2491.3@2491.4. asset=4"
                    }
                ]
            },
        },
    }
    with pytest.raises(RejectError, match="Post only order"):
        classify_order_response(response)


async def test_a_rejected_order_records_no_fill_and_no_oid(tmp_path: Path) -> None:
    """The whole point of the envelope rule, asserted through the manager.

    A transport that classifies the reject correctly must leave the registry with an
    order that has no oid, no fill and a REJECTED status. If the envelope were trusted,
    this order would be ACKED with a garbage oid and the loop would wait for a fill that
    is never coming.
    """
    from liqfade.execution.orders import OrderIntent, OrderManager, OrderStatus, OrderStore

    class RejectingTransport:
        async def place(self, intent: OrderIntent) -> int:
            return classify_order_response(
                {
                    "status": "ok",
                    "response": {
                        "type": "order",
                        "data": {"statuses": [{"error": "Post only order would have matched"}]},
                    },
                }
            )

        async def cancel(self, coin: str, cloid: str) -> None: ...

        async def modify(self, intent: OrderIntent) -> int:
            raise AssertionError("not reached")

    store = await OrderStore.open(tmp_path / "orders.sqlite")
    try:
        manager = OrderManager(
            store,
            RejectingTransport(),
            sz_decimals={"ETH": 4},
            killed=lambda: False,
        )
        order = await manager.submit(
            OrderIntent(
                cloid="0x" + "ab" * 16, coin="ETH", is_buy=True, px=2000.0, size=0.01, tif="Alo"
            )
        )

        assert order.status is OrderStatus.REJECTED
        assert order.oid is None
        assert order.filled == 0.0
        assert "Post only" in (order.reject_reason or "")
        assert await manager.open_orders() == []
    finally:
        await store.close()


def test_equity_counts_the_spot_usdc_that_is_actually_the_collateral() -> None:
    """`accountValue` is 0.0 on a flat account whose USDC sits in the spot wallet.

    Measured on testnet 2026-09-08: perps `accountValue` 0.0, `withdrawable` 0.0,
    `totalMarginUsed` 0.0, and 996.96 USDC in spot, on an account that opens positions
    perfectly well. Reading equity as `accountValue` alone made sizing refuse every entry
    for `risk_budget`, which looks exactly like a strategy finding nothing to trade. The
    same would happen on mainnet to an owner who leaves their USDC in spot.
    """
    state = {
        "marginSummary": {
            "accountValue": "0.0",
            "totalNtlPos": "0.0",
            "totalRawUsd": "0.0",
            "totalMarginUsed": "0.0",
        },
        "crossMaintenanceMarginUsed": "0.0",
        "withdrawable": "0.0",
        "assetPositions": [],
    }
    spot = {"balances": [{"coin": "USDC", "token": 0, "total": "996.956248", "hold": "0.0"}]}

    snapshot = parse_clearinghouse_state(state, spot=spot)

    assert snapshot.equity == pytest.approx(996.956248)
    assert snapshot.spot_usdc == pytest.approx(996.956248)


def test_equity_adds_the_two_rather_than_choosing_one() -> None:
    """With a position on, margin sits in perps and the rest is still in spot."""
    state = {
        "marginSummary": {
            "accountValue": "1.48872",
            "totalNtlPos": "14.87",
            "totalRawUsd": "0.0",
            "totalMarginUsed": "0.74409",
        },
        "crossMaintenanceMarginUsed": "0.0",
        "withdrawable": "0.74463",
        "assetPositions": [],
    }
    spot = {"balances": [{"coin": "USDC", "total": "995.0"}]}

    assert parse_clearinghouse_state(state, spot=spot).equity == pytest.approx(996.48872)


def test_no_spot_payload_leaves_equity_as_the_perps_account_value() -> None:
    """Callers that never ask for spot keep the old meaning rather than getting a zero."""
    state = {
        "marginSummary": {
            "accountValue": "500.0",
            "totalNtlPos": "0.0",
            "totalRawUsd": "0.0",
            "totalMarginUsed": "0.0",
        },
        "crossMaintenanceMarginUsed": "0.0",
        "withdrawable": "500.0",
        "assetPositions": [],
    }
    assert parse_clearinghouse_state(state).equity == 500.0


def test_asset_meta_comes_from_the_venue_not_from_a_default() -> None:
    """`szDecimals` decides whether an order is even accepted, so it cannot be guessed.

    `_resolve_meta` answers 5 for every coin, which is a backtest fallback. The testnet
    loop inherited it and sent ETH at 0.00435, five decimals against the four the venue
    allows: "Order has invalid size." CLAUDE.md says a coin with no szDecimals is refused
    rather than rounded on a guessed grid, and a wrong default is a guess that passes.
    """
    payload = {
        "universe": [
            {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
            {"name": "ETH", "szDecimals": 4, "maxLeverage": 25},
            {"name": "kPEPE", "szDecimals": 0, "maxLeverage": 10, "isDelisted": True},
        ]
    }

    meta = parse_asset_meta(payload)

    assert meta["ETH"].sz_decimals == 4
    assert meta["BTC"].sz_decimals == 5
    assert meta["ETH"].max_leverage == 25.0
    assert "kPEPE" not in meta, "a delisted asset is not one to size an order for"


def test_a_zero_decimal_asset_survives_the_parse() -> None:
    """szDecimals 0 is a whole-unit asset, not a missing field."""
    meta = parse_asset_meta({"universe": [{"name": "kBONK", "szDecimals": 0, "maxLeverage": 5}]})
    assert meta["kBONK"].sz_decimals == 0


async def test_signed_actions_never_share_a_millisecond() -> None:
    """Hyperliquid nonces are millisecond timestamps, so two actions in one ms collide.

    Drilled on testnet: the kill cancelled three orders back to back and the second came
    back `{"status": "err", "response": "Invalid nonce: duplicate nonce 1788866428841"}`.
    The registry marked it cancelled anyway, so the venue kept an order the bot believed
    it had cancelled, which is the exact disagreement the kill path cannot afford.

    The transport serialises signed calls and refuses to issue two inside one millisecond.
    """
    import time

    from liqfade.execution.hl import NonceGate

    gate = NonceGate()
    stamps = []

    async def one() -> None:
        async with gate:
            stamps.append(int(time.time() * 1000))

    await asyncio.gather(*(one() for _ in range(6)))

    assert len(set(stamps)) == len(stamps), f"two actions shared a millisecond: {stamps}"
    assert stamps == sorted(stamps), "and they must be issued in order"
