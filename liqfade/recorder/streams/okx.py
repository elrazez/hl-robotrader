"""OKX v5 liquidation-orders stream.

One subscription covers every SWAP, so the universe never changes the subscription. OKX
publishes at most one update per contract per second, so like Binance this is a lower
bound and `complete` is false.

Size caveat, worth knowing before using `notional_usd` from this venue: OKX quotes swap
size in contracts, and one contract is `ctVal` of the coin (0.01 BTC, 0.1 ETH and so
on), so `px * sz` is not USD. The raw `sz` and `venue_symbol` are recorded faithfully so
the correction can be applied later from the instruments endpoint; see the note in
docs/DATA_SOURCES.md. Inverse (coin margined) contracts map to `coin = null` for the
same reason.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import orjson

from liqfade.data.symbols import to_coin
from liqfade.recorder.streams.base import ParsedRow, Parser

VENUE = "okx"
TABLES = ("liquidations",)
CHANNEL = "liquidation-orders"

# OKX closes a connection with no traffic for 30s. Its ping is the literal string.
PING_FRAME = "ping"
PING_S = 25.0


def on_connect(_symbols: Sequence[str]) -> list[Any]:
    return [{"op": "subscribe", "args": [{"channel": CHANNEL, "instType": "SWAP"}]}]


def make_parser(complete: bool) -> Parser:
    def parse(msg: bytes, rx_ms: int) -> list[ParsedRow]:
        if msg == b"pong":
            return []
        obj = orjson.loads(msg)
        if obj.get("arg", {}).get("channel") != CHANNEL or "data" not in obj:
            return []  # subscribe ack or an error frame
        return [
            _row(instrument["instId"], detail, rx_ms, complete)
            for instrument in obj["data"]
            for detail in instrument.get("details") or []
        ]

    return parse


def _row(inst_id: str, detail: dict[str, Any], rx_ms: int, complete: bool) -> ParsedRow:
    px = float(detail["bkPx"])
    sz = float(detail["sz"])
    # posSide is the liquidated position when OKX sends it; in one-way mode it is "net",
    # and then the order side tells the story: the engine buys to close a short.
    pos_side = detail.get("posSide")
    side = (
        pos_side
        if pos_side in ("long", "short")
        else ("short" if detail.get("side") == "buy" else "long")
    )
    return (
        "liquidations",
        {
            "venue": VENUE,
            "coin": to_coin(inst_id, VENUE),
            "venue_symbol": inst_id,
            "ts_ms": int(detail["ts"]),
            "rx_ms": rx_ms,
            "side": side,
            "px": px,
            "sz": sz,
            "notional_usd": px * sz,
            "method": "unknown",
            "complete": complete,
        },
    )
