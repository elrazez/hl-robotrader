"""Parsing what Hyperliquid sends back, which is where the offline bugs live.

Every payload here is the shape docs/HYPERLIQUID.md describes. When a testnet probe
disagrees with one of these, the probe wins and this file changes with it.
"""

from __future__ import annotations

from typing import Any

import pytest

from liqfade.execution.hl import (
    classify_order_response,
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
