#!/usr/bin/env python3
"""The six Phase 5 testnet probes from docs/HYPERLIQUID.md, in one script.

    uv run python scripts/probe/hl_testnet.py --list
    uv run python scripts/probe/hl_testnet.py gtc_cancel --coin ETH
    uv run python scripts/probe/hl_testnet.py user_fees

Every probe prints the raw response it got, unedited, because the point is to find out
what the venue actually sends rather than to confirm what a doc claimed. Copy the answers
into docs/HYPERLIQUID.md and note the date in docs/DECISIONS.md.

**Testnet only.** The script refuses to run against any URL without "testnet" in it, on
top of the signer's own refusals, because a probe that places a real mainnet order is the
one bug in here that costs money. Run it with LIQFADE_ENV=testnet and the testnet agent
key in the environment; nothing here reads a key itself.

Not a test. Nothing under scripts/probe/ is imported by the package or by tests.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import websockets

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from liqfade.config import load_settings
from liqfade.execution.rounding import floor_size, round_price
from liqfade.execution.signer import Signer, load_signer

FAR_FROM_MARKET = 0.5  # a resting bid at half the mid never fills and never gets stale
QUIET_SIZE_USD = 12.0  # just over the 10 USDC minimum, per docs/HYPERLIQUID.md


def show(label: str, value: Any) -> None:
    """Print one labelled raw response. json where it is json, repr where it is not."""
    print(f"\n--- {label} " + "-" * max(0, 60 - len(label)))
    try:
        print(json.dumps(value, indent=2, default=str))
    except (TypeError, ValueError):
        print(repr(value))


def info_post(base_url: str, body: dict[str, Any]) -> Any:
    """One raw POST /info. Used where the SDK wraps the payload we want to see."""
    response = httpx.post(f"{base_url}/info", json=body, timeout=15.0)
    response.raise_for_status()
    return response.json()


def mid_price(base_url: str, coin: str) -> float:
    mids = info_post(base_url, {"type": "allMids"})
    return float(mids[coin])


def sz_decimals(base_url: str, coin: str) -> int:
    meta = info_post(base_url, {"type": "meta"})
    for asset in meta["universe"]:
        if asset["name"] == coin:
            return int(asset["szDecimals"])
    raise SystemExit(f"{coin} is not in the testnet universe")


def new_cloid() -> Any:
    from hyperliquid.utils.types import Cloid

    return Cloid.from_str("0x" + uuid.uuid4().hex)


def hl_price(base_url: str, coin: str, px: float) -> float:
    """A price Hyperliquid will accept, through `execution/rounding.py` and nowhere else.

    These probes used a bare `round(px)`, which is the integer for anything priced above a
    dollar and zero for anything priced below one. CLAUDE.md keeps rounding in one module
    for exactly that reason: a probe that rounds its own way is not probing the rules the
    bot will actually place orders under.
    """
    return float(round_price(px, sz_decimals(base_url, coin)))


def hl_size(base_url: str, coin: str, notional_usd: float, px: float) -> float:
    """A size for `notional_usd` at `px`, floored to the asset's `szDecimals`.

    Floored rather than rounded: rounding up can put the order over the notional the probe
    meant to risk, and these run against a faucet-funded account.
    """
    return float(floor_size(notional_usd / px, sz_decimals(base_url, coin)))


# --- probe 1: Gtc round trip and cancel by cloid ---------------------------------


def probe_gtc_cancel(signer: Signer, args: argparse.Namespace) -> None:
    """Place a Gtc far from market, watch orderUpdates, cancel by cloid, watch again."""
    exchange = signer.exchange()
    mid = mid_price(signer.base_url, args.coin)
    px = hl_price(signer.base_url, args.coin, mid * FAR_FROM_MARKET)
    size = hl_size(signer.base_url, args.coin, QUIET_SIZE_USD, px)
    cloid = new_cloid()
    show(
        "plan",
        {"coin": args.coin, "mid": mid, "limit_px": px, "size": size, "cloid": cloid.to_raw()},
    )

    frames = asyncio.run(
        _with_order_updates(
            signer,
            lambda: [
                (
                    "order",
                    exchange.order(
                        args.coin, True, size, px, {"limit": {"tif": "Gtc"}}, cloid=cloid
                    ),
                ),
                ("cancel_by_cloid", exchange.cancel_by_cloid(args.coin, cloid)),
            ],
            settle_s=args.settle,
        )
    )
    for label, value in frames["actions"]:
        show(label, value)
    show("orderUpdates sequence", frames["updates"])
    show(
        "openOrders after",
        info_post(signer.base_url, {"type": "openOrders", "user": signer.account_address}),
    )


async def _with_order_updates(
    signer: Signer,
    actions: Callable[[], list[tuple[str, Any]]],
    *,
    settle_s: float,
) -> dict[str, Any]:
    """Subscribe to orderUpdates, run the (blocking) actions, keep listening, return both.

    The SDK is synchronous, so the actions run on a thread while this coroutine keeps
    draining the socket. That ordering is the whole point: subscribing after placing the
    order would miss the very transition being probed.
    """
    updates: list[Any] = []
    url = signer.base_url.replace("https://", "wss://") + "/ws"
    async with websockets.connect(url, max_size=None) as ws:
        await ws.send(
            json.dumps(
                {
                    "method": "subscribe",
                    "subscription": {"type": "orderUpdates", "user": signer.account_address},
                }
            )
        )
        await asyncio.sleep(1.0)  # let the subscription land before anything is placed
        task = asyncio.create_task(asyncio.to_thread(actions))
        deadline = time.monotonic() + settle_s
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(
                    ws.recv(), timeout=max(0.1, deadline - time.monotonic())
                )
            except TimeoutError:
                break
            message = json.loads(raw)
            if message.get("channel") != "subscriptionResponse":
                updates.append(message)
        results = await task
    return {"actions": results, "updates": updates}


# --- probe 2: Alo that would cross -----------------------------------------------


def probe_alo_cross(signer: Signer, args: argparse.Namespace) -> None:
    """Post-only through the touch. The answer wanted is the exact rejection string."""
    exchange = signer.exchange()
    book = info_post(signer.base_url, {"type": "l2Book", "coin": args.coin})
    best_ask = float(book["levels"][1][0]["px"])
    px = hl_price(signer.base_url, args.coin, best_ask * 1.01)
    size = hl_size(signer.base_url, args.coin, QUIET_SIZE_USD, px)
    show("plan", {"coin": args.coin, "best_ask": best_ask, "buy_limit_px": px, "size": size})
    show(
        "alo_that_crosses",
        exchange.order(args.coin, True, size, px, {"limit": {"tif": "Alo"}}, cloid=new_cloid()),
    )


# --- probe 3: TP/SL with positionTpsl grouping -----------------------------------


def probe_tpsl_group(signer: Signer, args: argparse.Namespace) -> None:
    """Open a small position, attach TP and SL as one positionTpsl group, then watch.

    Needs faucet USDC. It leaves a position open on purpose: the thing being probed is
    what happens when one leg fills, so run it, move the trigger with --tp-bps until one
    side hits, and read the orderUpdates the loop prints.
    """
    from hyperliquid.utils.signing import OrderRequest

    exchange = signer.exchange()
    mid = mid_price(signer.base_url, args.coin)
    size = hl_size(signer.base_url, args.coin, max(QUIET_SIZE_USD, args.notional), mid)
    show("entry", exchange.market_open(args.coin, True, size, None, 0.01, cloid=new_cloid()))

    tp_px = hl_price(signer.base_url, args.coin, mid * (1 + args.tp_bps / 1e4))
    sl_px = hl_price(signer.base_url, args.coin, mid * (1 - args.tp_bps / 1e4))
    requests: list[OrderRequest] = [
        {
            "coin": args.coin,
            "is_buy": False,
            "sz": size,
            "limit_px": tp_px,
            "order_type": {"trigger": {"triggerPx": tp_px, "isMarket": True, "tpsl": "tp"}},
            "reduce_only": True,
            "cloid": new_cloid(),
        },
        {
            "coin": args.coin,
            "is_buy": False,
            "sz": size,
            "limit_px": sl_px,
            "order_type": {"trigger": {"triggerPx": sl_px, "isMarket": True, "tpsl": "sl"}},
            "reduce_only": True,
            "cloid": new_cloid(),
        },
    ]
    show("positionTpsl group", exchange.bulk_orders(requests, grouping="positionTpsl"))
    show(
        "frontendOpenOrders",
        info_post(signer.base_url, {"type": "frontendOpenOrders", "user": signer.account_address}),
    )
    print("\nOne leg filling should cancel the other. Watch with:")
    print(f"  uv run python scripts/probe/hl_testnet.py gtc_cancel --coin {args.coin} --settle 60")
    print("or close by hand at app.hyperliquid-testnet.xyz when done.")


# --- probe 4: submit to ack latency ----------------------------------------------


def probe_latency(signer: Signer, args: argparse.Namespace) -> None:
    """N place-and-cancel round trips far from market, timing submit to response.

    Run it once at a quiet hour and once at a busy one; docs/HYPERLIQUID.md wants both.
    Every order is cancelled immediately, so nothing rests and nothing fills.
    """
    exchange = signer.exchange()
    mid = mid_price(signer.base_url, args.coin)
    px = hl_price(signer.base_url, args.coin, mid * FAR_FROM_MARKET)
    size = hl_size(signer.base_url, args.coin, QUIET_SIZE_USD, px)
    submits, cancels, statuses = [], [], []
    for _ in range(args.count):
        cloid = new_cloid()
        started = time.perf_counter()
        response = exchange.order(args.coin, True, size, px, {"limit": {"tif": "Gtc"}}, cloid=cloid)
        submits.append((time.perf_counter() - started) * 1000.0)
        statuses.append(response.get("status"))
        started = time.perf_counter()
        exchange.cancel_by_cloid(args.coin, cloid)
        cancels.append((time.perf_counter() - started) * 1000.0)

    def summarise(name: str, values: list[float]) -> dict[str, float]:
        ordered = sorted(values)
        return (
            {
                "n": len(values),
                "min": round(min(values), 1),
                "p50": round(statistics.median(values), 1),
                "p95": round(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))], 1),
                "max": round(max(values), 1),
                "mean": round(statistics.fmean(values), 1),
            }
            if values
            else {"n": 0}
        )

    show("submit_ms (REST order round trip)", summarise("submit", submits))
    show("cancel_ms", summarise("cancel", cancels))
    show("statuses", sorted(set(map(str, statuses))))
    show("raw submit_ms", [round(v, 1) for v in submits])


# --- probe 5: scheduleCancel ------------------------------------------------------


def probe_schedule_cancel(signer: Signer, args: argparse.Namespace) -> None:
    """Is the dead man's switch available to a small account, and what is the error?"""
    exchange = signer.exchange()
    at_ms = int(time.time() * 1000) + args.seconds * 1000
    show("schedule_cancel at", {"in_s": args.seconds, "time_ms": at_ms})
    try:
        show("schedule_cancel", exchange.schedule_cancel(at_ms))
    except Exception as exc:
        show("schedule_cancel raised", f"{type(exc).__name__}: {exc}")
    show("clear it again", exchange.schedule_cancel(None))


# --- probe 6: userFees ------------------------------------------------------------


def probe_user_fees(signer: Signer, args: argparse.Namespace) -> None:
    """The payload shape and the account's live rates, which the cost model reads."""
    show(
        "userFees", info_post(signer.base_url, {"type": "userFees", "user": signer.account_address})
    )


PROBES: dict[str, Callable[[Signer, argparse.Namespace], None]] = {
    "gtc_cancel": probe_gtc_cancel,
    "alo_cross": probe_alo_cross,
    "tpsl_group": probe_tpsl_group,
    "latency": probe_latency,
    "schedule_cancel": probe_schedule_cancel,
    "user_fees": probe_user_fees,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("probe", nargs="?", choices=sorted(PROBES), help="which probe to run")
    parser.add_argument("--list", action="store_true", help="list the probes and exit")
    parser.add_argument("--coin", default="ETH", help="testnet perp to use (default ETH)")
    parser.add_argument("--count", type=int, default=50, help="latency: round trips (default 50)")
    parser.add_argument("--settle", type=float, default=10.0, help="seconds to keep listening")
    parser.add_argument("--notional", type=float, default=QUIET_SIZE_USD, help="tpsl: USD size")
    parser.add_argument("--tp-bps", type=float, default=50.0, help="tpsl: trigger distance in bps")
    parser.add_argument("--seconds", type=int, default=10, help="schedule_cancel: seconds ahead")
    args = parser.parse_args(argv)

    if args.list or not args.probe:
        for name, fn in sorted(PROBES.items()):
            print(f"{name:18s} {(fn.__doc__ or '').splitlines()[0]}")
        return 0

    settings = load_settings()
    if "testnet" not in settings.venues.hyperliquid.api_url:
        print(
            f"refusing: api_url is {settings.venues.hyperliquid.api_url}, which is not "
            f"testnet. Set LIQFADE_ENV=testnet.",
            file=sys.stderr,
        )
        return 2
    signer = load_signer(settings)
    print(f"account {signer.account_address}  agent {signer.agent_address}  {signer.base_url}")
    PROBES[args.probe](signer, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
