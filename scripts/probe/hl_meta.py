#!/usr/bin/env python3
"""Print the top Hyperliquid perps by 24h notional volume, and the round trip time.

This is the first thing to run on a new machine: it proves outbound connectivity to the
Hyperliquid API and gives the numbers that seed universe.core_coins and the volume and
open interest floors in config/default.yaml.

    uv run python scripts/probe/hl_meta.py
    uv run python scripts/probe/hl_meta.py --testnet --top 40

Not a test. Nothing under scripts/probe/ is imported by the package or by tests.
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx

MAINNET = "https://api.hyperliquid.xyz/info"
TESTNET = "https://api.hyperliquid-testnet.xyz/info"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--testnet", action="store_true", help="hit the testnet API instead")
    parser.add_argument("--top", type=int, default=20, help="how many rows to print")
    args = parser.parse_args()

    url = TESTNET if args.testnet else MAINNET
    started = time.perf_counter()
    try:
        response = httpx.post(url, json={"type": "metaAndAssetCtxs"}, timeout=15.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"request to {url} failed: {exc}", file=sys.stderr)
        return 1
    rtt_ms = (time.perf_counter() - started) * 1000.0

    # metaAndAssetCtxs returns [meta, contexts] positionally aligned by asset index.
    meta, contexts = response.json()
    universe = meta["universe"]
    if len(universe) != len(contexts):
        print(
            f"universe has {len(universe)} assets but {len(contexts)} contexts came back; "
            "the response shape changed, update docs/HYPERLIQUID.md",
            file=sys.stderr,
        )
        return 1

    rows = []
    for asset, ctx in zip(universe, contexts, strict=True):
        if asset.get("isDelisted"):
            continue
        mark = float(ctx.get("markPx") or 0.0)
        rows.append(
            {
                "coin": asset["name"],
                "day_ntl_vlm": float(ctx.get("dayNtlVlm") or 0.0),
                # openInterest is in coin units; multiply by mark for a USD figure.
                "oi_usd": float(ctx.get("openInterest") or 0.0) * mark,
                "max_leverage": asset.get("maxLeverage"),
                "sz_decimals": asset.get("szDecimals"),
                "funding_8h_bps": float(ctx.get("funding") or 0.0) * 8 * 10_000,
            }
        )
    rows.sort(key=lambda row: row["day_ntl_vlm"], reverse=True)

    print(f"{url}  round trip {rtt_ms:.0f} ms  {len(rows)} live perps")
    print(
        f"\n{'#':>3}  {'coin':<10} {'24h notional $':>16} {'open interest $':>16} "
        f"{'maxLev':>7} {'szDec':>6} {'fund 8h bps':>12}"
    )
    for index, row in enumerate(rows[: args.top], start=1):
        print(
            f"{index:>3}  {row['coin']:<10} {row['day_ntl_vlm']:>16,.0f} {row['oi_usd']:>16,.0f} "
            f"{row['max_leverage']:>7} {row['sz_decimals']:>6} {row['funding_8h_bps']:>12.2f}"
        )

    print(
        "\nSeed config/universe from this: core_coins for the names you always want, "
        "min_day_ntl_vlm_usd and min_open_interest_usd below the tail you are willing to trade."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
