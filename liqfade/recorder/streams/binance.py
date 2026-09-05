"""Binance USDⓈ-M all-market liquidation stream.

`wss://fstream.binance.com/ws/!forceOrder@arr` needs no subscribe frame: the stream is
in the URL. Binance publishes at most one event per symbol per second and only the
largest order in that second, so every count and notional from this venue is a lower
bound. That is what `complete=false` in the row records, and why `venues.binance.
liq_weight` exists.

`o.S` is the side of the liquidation order, so SELL means the engine was selling to
close a long: a long was liquidated.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import orjson

from liqfade.data.symbols import to_coin
from liqfade.recorder.streams.base import ParsedRow, Parser

VENUE = "binance"
TABLES = ("liquidations",)

# `o.S` is the side of the liquidation order, so SELL means the engine was selling to
# close a long. Explicit rather than a two-branch default: a re-cased token would
# otherwise write every long liquidation as a short.
LIQ_SIDES = {"SELL": "long", "BUY": "short"}


def on_connect(_symbols: Sequence[str]) -> list[Any]:
    """Nothing to send: the all-market stream is part of the URL."""
    return []


def make_parser(complete: bool) -> Parser:
    """Bind the venue's completeness flag from config into the pure parser."""

    def parse(msg: bytes, rx_ms: int) -> list[ParsedRow]:
        obj = orjson.loads(msg)
        events = obj if isinstance(obj, list) else [obj]
        return [
            _row(event["o"], rx_ms, complete)
            for event in events
            if isinstance(event, dict) and event.get("e") == "forceOrder"
        ]

    return parse


def _row(order: dict[str, Any], rx_ms: int, complete: bool) -> ParsedRow:
    side = LIQ_SIDES.get(str(order.get("S")))
    if side is None:
        return ("liquidations", None)
    symbol = order["s"]
    # ap is the average fill price and is 0 until the order fills; p is the order price.
    px = float(order.get("ap") or 0.0) or float(order["p"])
    sz = float(order["q"])
    return (
        "liquidations",
        {
            "venue": VENUE,
            "coin": to_coin(symbol, VENUE),
            "venue_symbol": symbol,
            "ts_ms": int(order["T"]),
            "rx_ms": rx_ms,
            "side": side,
            "px": px,
            "sz": sz,
            "notional_usd": px * sz,
            "method": "unknown",
            "complete": complete,
        },
    )
