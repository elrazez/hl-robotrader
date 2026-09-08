#!/usr/bin/env python3
"""Build and check the Phase 5 drill state on testnet, in one command each.

    python scripts/drill/testnet_state.py build     # a position with a bracket, and a resting order
    python scripts/drill/testnet_state.py check     # one line: what the venue holds
    python scripts/drill/testnet_state.py flat      # cancel and close everything, by hand

`build` drives the loop over its control socket, so the state is one the loop placed
itself: an order a script places drills the script. It waits for the venue to show the
result rather than sleeping a guessed number of seconds.

`check` is the pass criterion for every drill. The venue decides, never the log counters:
on 2026-09-08 a kill reported "cancelled 2, closed 1" while Hyperliquid still held the
position, one order and a bracket leg.

Testnet only, like every other script under scripts/.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import httpx

SOCKET = os.environ.get("LIQFADE_DRILL_SOCKET", "/srv/liqfade/data/control-testnet.sock")
INFO = "https://api.hyperliquid-testnet.xyz/info"


def venue(address: str) -> tuple[dict[str, float], list[str], list[str]]:
    """(positions, plain orders, trigger orders). Triggers live only in frontendOpenOrders."""
    state = httpx.post(INFO, json={"type": "clearinghouseState", "user": address}).json()
    front = httpx.post(INFO, json={"type": "frontendOpenOrders", "user": address}).json()
    positions = {
        p["position"]["coin"]: float(p["position"]["szi"])
        for p in state.get("assetPositions", [])
        if float(p["position"]["szi"]) != 0.0
    }
    triggers = [f"{o['coin']}:{o['orderType']}" for o in front if o.get("isTrigger")]
    orders = [f"{o['coin']}:{o['side']}" for o in front if not o.get("isTrigger")]
    return positions, orders, triggers


def line(address: str) -> str:
    positions, orders, triggers = venue(address)
    return (
        f"positions={positions or '{}'} orders={orders or '[]'} triggers={triggers or '[]'} "
        f"FLAT={not positions and not orders and not triggers}"
    )


def wait_for(address: str, predicate, *, timeout_s: float, what: str) -> bool:
    """Poll the venue until `predicate` holds. One place that waits, with a deadline."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate(*venue(address)):
            return True
        time.sleep(1.0)
    print(f"timed out after {timeout_s:.0f}s waiting for {what}", file=sys.stderr)
    return False


async def control(cmd: str, **args: object) -> object:
    from liqfade.control import ControlClient

    client = ControlClient(SOCKET, connect_timeout_s=5.0, request_timeout_s=20.0)
    return await client.send(cmd, **args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("build", "check", "flat"))
    parser.add_argument("--position-coin", default="ETH")
    parser.add_argument("--order-coin", default="SOL")
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args(argv)

    from liqfade.config import load_settings
    from liqfade.execution.signer import load_signer

    settings = load_settings()
    if "testnet" not in settings.venues.hyperliquid.exchange_base:
        print("refusing: not testnet", file=sys.stderr)
        return 2
    address = load_signer(settings).account_address

    if args.action == "check":
        print(line(address))
        return 0

    if args.action == "flat":
        asyncio.run(control("flat"))
        ok = wait_for(
            address,
            lambda p, o, t: not p and not o and not t,
            timeout_s=args.timeout,
            what="the account to go flat",
        )
        print(line(address))
        return 0 if ok else 1

    # build: flatten first. A coin already held is refused by the entry path, so a
    # leftover position from the last drill silently produces no new state at all.
    asyncio.run(control("flat"))
    if not wait_for(
        address,
        lambda p, o, t: not p and not o and not t,
        timeout_s=args.timeout,
        what="the account to go flat first",
    ):
        print(line(address))
        return 1
    asyncio.run(control("resume"))
    asyncio.run(
        control(
            "inject_intent",
            coin=args.position_coin,
            side="long",
            target_bps=300.0,
            stop_bps=300.0,
            urgency="high",
        )
    )
    if not wait_for(
        address,
        lambda p, o, t: args.position_coin in p and len(t) >= 2,
        timeout_s=args.timeout,
        what="the position and its bracket",
    ):
        print(line(address))
        return 1

    asyncio.run(
        control(
            "inject_intent", coin=args.order_coin, side="long", target_bps=300.0, stop_bps=300.0
        )
    )
    wait_for(
        address,
        lambda p, o, t: any(x.startswith(args.order_coin) for x in o),
        timeout_s=args.timeout,
        what="the resting order",
    )
    print(line(address))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
