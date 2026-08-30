"""Capture one raw sample of every WebSocket message shape the recorder parses.

Run it when a venue changes its payload or when the parser tests need refreshing:

    uv run python scripts/probe/ws_payloads.py --seconds 60 > /tmp/payloads.json

Public feeds only, no keys, read only. The output is what the parser tests embed
verbatim, so the tests fail against reality rather than against what a doc claimed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

import websockets


async def collect(name: str, url: str, subs: list[Any], seconds: float) -> dict[str, Any]:
    seen: dict[str, str] = {}
    try:
        async with asyncio.timeout(seconds), websockets.connect(url, max_size=None) as ws:
            for sub in subs:
                await ws.send(json.dumps(sub))
            async for message in ws:
                text = message if isinstance(message, str) else message.decode()
                try:
                    obj = json.loads(text)
                except ValueError:
                    seen.setdefault(f"raw:{text[:20]}", text)
                    continue
                key = (
                    obj.get("channel")
                    or obj.get("arg", {}).get("channel")
                    or obj.get("topic")
                    or obj.get("e")
                    or "other"
                )
                seen.setdefault(str(key), text)
    except (TimeoutError, OSError) as exc:
        print(f"{name}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return seen


async def main(seconds: float, coin: str) -> None:
    hl = [
        {"method": "subscribe", "subscription": {"type": t, "coin": coin}}
        for t in ("trades", "bbo", "l2Book", "activeAssetCtx")
    ]
    hl.append(
        {"method": "subscribe", "subscription": {"type": "candle", "coin": coin, "interval": "1m"}}
    )
    hl.append({"method": "ping"})
    tasks = {
        "hyperliquid": collect("hyperliquid", "wss://api.hyperliquid.xyz/ws", hl, seconds),
        "binance": collect("binance", "wss://fstream.binance.com/ws/!forceOrder@arr", [], seconds),
        "bybit": collect(
            "bybit",
            "wss://stream.bybit.com/v5/public/linear",
            [
                {
                    "op": "subscribe",
                    "args": [
                        f"allLiquidation.{c}USDT" for c in ("BTC", "ETH", "SOL", "XRP", "DOGE")
                    ],
                },
                {"op": "ping"},
            ],
            seconds,
        ),
        "okx": collect(
            "okx",
            "wss://ws.okx.com:8443/ws/v5/public",
            [{"op": "subscribe", "args": [{"channel": "liquidation-orders", "instType": "SWAP"}]}],
            seconds,
        ),
    }
    results = dict(zip(tasks, await asyncio.gather(*tasks.values()), strict=True))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--coin", default="BTC")
    args = parser.parse_args()
    asyncio.run(main(args.seconds, args.coin))
