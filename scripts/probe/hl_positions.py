#!/usr/bin/env python3
"""Print the raw `clearinghouseState` of any Hyperliquid address, public and keyless.

This is what `hl_positions` is parsed from, so run it before touching the poller and
embed a real response in the test rather than a shape copied from a doc. It reads no
key, signs nothing, and only ever posts to `/info`.

    uv run python scripts/probe/hl_positions.py 0x49dfb8dd... 0x3a62fea9...
    uv run python scripts/probe/hl_positions.py --harvest 40

`--harvest N` subscribes to the public BTC/ETH/SOL `trades` feed for a few seconds and
uses the counterparty addresses it sees, which is exactly where the recorder's address
book comes from. Useful when the addresses you have went flat.

Not a test. Nothing under scripts/probe/ is imported by the package or by tests.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx
import orjson
import websockets

INFO = "https://api.hyperliquid.xyz/info"
WS = "wss://api.hyperliquid.xyz/ws"
# The two counterparties of the first trade in tests/test_recorder_parsers.py::HL_TRADES,
# captured 2026-08-30. They may well be flat by the time you read this; use --harvest.
FIXTURE = [
    "0x49dfb8ddbd3ba96c0699a3f120e71f2516c345d8",
    "0x3a62fea9cd2832f639f13d6a5bc5584f62c470e4",
]


async def harvest(count: int, seconds: float) -> list[str]:
    """Counterparty addresses off the public trades feed, biggest notional first."""
    seen: dict[str, float] = {}
    async with websockets.connect(WS) as socket:
        for coin in ("BTC", "ETH", "SOL"):
            await socket.send(
                json.dumps(
                    {"method": "subscribe", "subscription": {"type": "trades", "coin": coin}}
                )
            )
        try:
            async with asyncio.timeout(seconds):
                while True:
                    obj = orjson.loads(await socket.recv())
                    if obj.get("channel") != "trades":
                        continue
                    for row in obj["data"]:
                        notional = float(row["px"]) * float(row["sz"])
                        for address in row.get("users") or []:
                            seen[address] = seen.get(address, 0.0) + notional
        except TimeoutError:
            pass
    ranked = sorted(seen.items(), key=lambda item: -item[1])
    print(f"harvested {len(seen)} addresses in {seconds:.0f}s, taking the top {count}\n")
    return [address for address, _ in ranked[:count]]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("addresses", nargs="*", default=[], help="addresses to ask about")
    parser.add_argument("--harvest", type=int, default=0, help="take N addresses off the tape")
    parser.add_argument("--seconds", type=float, default=8.0, help="seconds to harvest for")
    parser.add_argument("--full", action="store_true", help="print every response in full")
    args = parser.parse_args()

    addresses = list(args.addresses) or (
        asyncio.run(harvest(args.harvest, args.seconds)) if args.harvest else FIXTURE
    )

    interesting = []
    for address in addresses:
        try:
            response = httpx.post(
                INFO, json={"type": "clearinghouseState", "user": address}, timeout=15.0
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            print(f"{address}  FAILED: {exc}", file=sys.stderr)
            continue
        state = response.json()
        positions = state.get("assetPositions") or []
        open_positions = [p for p in positions if float(p["position"]["szi"]) != 0.0]
        nulls = sum(1 for p in open_positions if p["position"].get("liquidationPx") is None)
        kinds = {p["position"]["leverage"]["type"] for p in open_positions}
        print(
            f"{address}  time={state.get('time')}  positions={len(open_positions)}  "
            f"null liquidationPx={nulls}  leverage={sorted(kinds)}"
        )
        if args.full or (len(open_positions) >= 2 and (nulls or len(kinds) > 1)):
            interesting.append((address, state))

    for address, state in interesting:
        print(f"\n--- {address} ---")
        print(json.dumps(state, indent=2))
    if not interesting:
        print(
            "\nNothing with two or more open positions and a null liquidationPx or a mix "
            "of margin types. Re-run with --harvest 40 or --full."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
