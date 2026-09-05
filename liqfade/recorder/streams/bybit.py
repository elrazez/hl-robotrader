"""Bybit v5 linear liquidation stream.

`allLiquidation.{SYMBOL}`, one topic per symbol, subscribed in batches. Bybit has
published every liquidation since 2025-02-25 rather than a throttled sample, which makes
it the reference venue for cross-exchange liquidation intensity and the reason
`venues.bybit.complete` is true.

`S` is the position side in Bybit's own confusing wording: `Sell` means a long was
liquidated. Same convention as Binance, opposite reading of the field name.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import orjson

from liqfade.data.symbols import to_coin
from liqfade.recorder.streams.base import DiffFramesFn, FramesFn, ParsedRow, Parser

VENUE = "bybit"
TABLES = ("liquidations",)
TOPIC = "allLiquidation"

PING_FRAME = {"op": "ping"}
PING_S = 20.0

# `S` is the position side in Bybit's own wording: Sell means a long was liquidated.
# Explicit rather than a two-branch default, for the same reason as every other venue.
LIQ_SIDES = {"Sell": "long", "Buy": "short"}


def _batched(op: str, symbols: Sequence[str], batch: int) -> list[dict[str, Any]]:
    topics = [f"{TOPIC}.{symbol}" for symbol in symbols]
    return [
        {"op": op, "args": topics[start : start + batch]} for start in range(0, len(topics), batch)
    ]


def make_frames(batch: int) -> tuple[FramesFn, DiffFramesFn]:
    """Subscribe frame builders bound to venues.bybit.subscribe_batch."""

    def on_connect(symbols: Sequence[str]) -> list[Any]:
        return list(_batched("subscribe", symbols, batch))

    def on_universe_change(added: Sequence[str], removed: Sequence[str]) -> list[Any]:
        return [
            *_batched("subscribe", added, batch),
            *_batched("unsubscribe", removed, batch),
        ]

    return on_connect, on_universe_change


def make_parser(complete: bool) -> Parser:
    def parse(msg: bytes, rx_ms: int) -> list[ParsedRow]:
        obj = orjson.loads(msg)
        if not str(obj.get("topic", "")).startswith(f"{TOPIC}."):
            return []  # subscribe acks and pongs
        return [_row(entry, rx_ms, complete) for entry in obj.get("data") or []]

    return parse


def _row(entry: dict[str, Any], rx_ms: int, complete: bool) -> ParsedRow:
    side = LIQ_SIDES.get(str(entry.get("S")))
    if side is None:
        return ("liquidations", None)
    symbol = entry["s"]
    px = float(entry["p"])
    sz = float(entry["v"])
    return (
        "liquidations",
        {
            "venue": VENUE,
            "coin": to_coin(symbol, VENUE),
            "venue_symbol": symbol,
            "ts_ms": int(entry["T"]),
            "rx_ms": rx_ms,
            "side": side,
            "px": px,
            "sz": sz,
            "notional_usd": px * sz,
            "method": "unknown",
            "complete": complete,
        },
    )
