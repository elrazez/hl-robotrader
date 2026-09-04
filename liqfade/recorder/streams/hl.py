"""Hyperliquid WebSocket parsing and subscription frames.

Five channels per coin: `trades`, `bbo`, `l2Book`, `candle` (1m) and `activeAssetCtx`,
feeding the `trades`, `bbo`, `l2`, `candles_1m` and `asset_ctx` tables. Payload shapes
were captured live with `scripts/probe/ws_payloads.py` on 2026-08-30, not taken from a
doc, and the parser tests embed those exact bytes.

Two things the schema doc does not say and the wire does:

- `activeAssetCtx` carries no timestamp, so `ts_ms` is set to `rx_ms` for that table. It
  is a snapshot of a slowly moving context, not an event, so the receive time is the
  honest answer.
- `side` on a trade is the aggressor: `B` is a buy, `A` is a sell.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import orjson

# channel -> canonical table it feeds
CHANNEL_TABLES = {
    "trades": "trades",
    "bbo": "bbo",
    "l2Book": "l2",
    "candle": "candles_1m",
    "activeAssetCtx": "asset_ctx",
}
TABLES = tuple(CHANNEL_TABLES.values())
VENUE = "hyperliquid"
CANDLE_INTERVAL = "1m"
L2_LEVELS = 20  # what the l2Book subscription returns per side

PING_FRAME = {"method": "ping"}
PONG_MARKER = b'"pong"'


def _subscription(channel: str, coin: str) -> dict[str, Any]:
    sub: dict[str, Any] = {"type": channel, "coin": coin}
    if channel == "candle":
        sub["interval"] = CANDLE_INTERVAL
    return sub


def _frames(method: str, coins: Sequence[str]) -> list[dict[str, Any]]:
    return [
        {"method": method, "subscription": _subscription(channel, coin)}
        for coin in coins
        for channel in CHANNEL_TABLES
    ]


def on_connect(symbols: Sequence[str]) -> list[dict[str, Any]]:
    """One subscribe frame per coin per channel. HL allows 1000 subscriptions."""
    return _frames("subscribe", symbols)


def on_universe_change(added: Sequence[str], removed: Sequence[str]) -> list[dict[str, Any]]:
    return _frames("subscribe", added) + _frames("unsubscribe", removed)


def _f(value: Any) -> float | None:
    """HL sends every number as a string. None and "" become null rather than 0.0."""
    if value is None or value == "":
        return None
    return float(value)


def parse(msg: bytes, rx_ms: int) -> list[tuple[str, dict[str, Any]]]:
    """Turn one HL frame into canonical rows. Unknown channels yield nothing."""
    obj = orjson.loads(msg)
    channel = obj.get("channel")
    data = obj.get("data")
    if channel == "trades":
        return [_trade(row, rx_ms) for row in data]
    if channel == "bbo":
        return [_bbo(data, rx_ms)]
    if channel == "l2Book":
        return _l2(data, rx_ms)
    if channel == "candle":
        # A snapshot on subscribe arrives as a list, live updates as one object.
        rows = data if isinstance(data, list) else [data]
        return [_candle(row) for row in rows]
    if channel == "activeAssetCtx":
        return [_asset_ctx(data, rx_ms)]
    return []


def _trade(row: dict[str, Any], rx_ms: int) -> tuple[str, dict[str, Any]]:
    return (
        "trades",
        {
            "venue": VENUE,
            "coin": row["coin"],
            "ts_ms": int(row["time"]),
            "rx_ms": rx_ms,
            "px": _f(row["px"]),
            "sz": _f(row["sz"]),
            "side": "buy" if row.get("side") == "B" else "sell",
            "tid": int(row["tid"]),
        },
    )


def _level(levels: Any, index: int) -> dict[str, Any]:
    """One side of a book level, or an empty dict when that side is thin or absent."""
    if not isinstance(levels, list) or index >= len(levels) or not levels[index]:
        return {}
    level = levels[index]
    return level if isinstance(level, dict) else {}


def _bbo(data: dict[str, Any], rx_ms: int) -> tuple[str, dict[str, Any]]:
    bid, ask = _level(data.get("bbo"), 0), _level(data.get("bbo"), 1)
    return (
        "bbo",
        {
            "coin": data["coin"],
            "ts_ms": int(data["time"]),
            "rx_ms": rx_ms,
            "bid_px": _f(bid.get("px")),
            "bid_sz": _f(bid.get("sz")),
            "ask_px": _f(ask.get("px")),
            "ask_sz": _f(ask.get("sz")),
        },
    )


def _l2(data: dict[str, Any], rx_ms: int) -> list[tuple[str, dict[str, Any]]]:
    levels = data.get("levels") or [[], []]
    bids, asks = [*levels, [], []][:2]
    depth = min(L2_LEVELS, max(len(bids), len(asks)))
    coin, ts_ms = data["coin"], int(data["time"])
    rows = []
    for index in range(depth):
        bid, ask = _level(bids, index), _level(asks, index)
        rows.append(
            (
                "l2",
                {
                    "coin": coin,
                    "ts_ms": ts_ms,
                    "rx_ms": rx_ms,
                    "level": index,
                    "bid_px": _f(bid.get("px")),
                    "bid_sz": _f(bid.get("sz")),
                    "ask_px": _f(ask.get("px")),
                    "ask_sz": _f(ask.get("sz")),
                },
            )
        )
    return rows


def _candle(row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return (
        "candles_1m",
        {
            "coin": row["s"],
            "ts_ms": int(row["t"]),
            "o": _f(row["o"]),
            "h": _f(row["h"]),
            "l": _f(row["l"]),
            "c": _f(row["c"]),
            "v": _f(row["v"]),
            "n": int(row.get("n") or 0),
        },
    )


def _asset_ctx(data: dict[str, Any], rx_ms: int) -> tuple[str, dict[str, Any]]:
    ctx = data.get("ctx") or {}
    # `impactPxs` is [bid, ask], the average fill price of a fixed notional against each
    # side of the book. A coin with an empty side sends a short list or none at all, so
    # the pair is read positionally and missing entries stay null rather than becoming 0.
    impact = ctx.get("impactPxs") or []
    return (
        "asset_ctx",
        {
            "coin": data["coin"],
            # activeAssetCtx carries no exchange timestamp; receive time is the truth.
            "ts_ms": rx_ms,
            "rx_ms": rx_ms,
            "mark_px": _f(ctx.get("markPx")),
            "oracle_px": _f(ctx.get("oraclePx")),
            "mid_px": _f(ctx.get("midPx")),
            "funding": _f(ctx.get("funding")),
            "open_interest": _f(ctx.get("openInterest")),
            "day_ntl_vlm": _f(ctx.get("dayNtlVlm")),
            "premium": _f(ctx.get("premium")),
            "impact_bid_px": _f(impact[0]) if len(impact) > 0 else None,
            "impact_ask_px": _f(impact[1]) if len(impact) > 1 else None,
        },
    )
