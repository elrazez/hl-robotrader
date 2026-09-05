"""Which coins the recorder subscribes to, refreshed from metaAndAssetCtxs.

Selection: the top `universe.top_n_by_volume` HL perps by 24h notional volume that also
clear the volume and open interest floors, union the core coins, minus the exclude list.
The cross-venue streams follow the same set through data/symbols.py, so one universe
change resubscribes every venue.

`universe.min_listing_days` is not applied here. metaAndAssetCtxs carries no listing
date, and recording a young coin costs nothing; the floor belongs on the trading side and
is enforced in risk/ in Phase 5.

The same REST call doubles as the REST latency probe, since it is the only recurring
request the recorder makes to Hyperliquid.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import orjson

from liqfade.config.models import Universe
from liqfade.log import get_logger

log = get_logger(__name__)

INFO_PATH = "/info"
REQUEST_TIMEOUT_S = 20.0


def select_universe(payload: Any, cfg: Universe) -> list[str]:
    """Coins to subscribe, from a raw `metaAndAssetCtxs` response. Pure and testable."""
    meta, ctxs = payload[0], payload[1]
    assets = meta.get("universe", [])
    ranked: list[tuple[float, str]] = []
    for asset, ctx in zip(assets, ctxs, strict=False):
        if asset.get("isDelisted"):
            continue
        volume = float(ctx.get("dayNtlVlm") or 0.0)
        mark = float(ctx.get("markPx") or 0.0)
        open_interest_usd = float(ctx.get("openInterest") or 0.0) * mark
        if volume < cfg.min_day_ntl_vlm_usd or open_interest_usd < cfg.min_open_interest_usd:
            continue
        ranked.append((volume, asset["name"]))

    ranked.sort(key=lambda row: (-row[0], row[1]))
    top = {name for _, name in ranked[: cfg.top_n_by_volume]}
    return sorted((top | set(cfg.core_coins)) - set(cfg.exclude))


async def fetch_meta_and_ctxs(client: httpx.AsyncClient, api_url: str) -> tuple[Any, float]:
    """POST /info type=metaAndAssetCtxs. Returns (payload, round trip milliseconds)."""
    started = time.monotonic()
    response = await client.post(
        f"{api_url}{INFO_PATH}",
        json={"type": "metaAndAssetCtxs"},
        timeout=REQUEST_TIMEOUT_S,
    )
    response.raise_for_status()
    payload = orjson.loads(response.content)
    return payload, (time.monotonic() - started) * 1000.0


class UniverseManager:
    """Refreshes the universe on a timer and calls back when the set changes."""

    def __init__(
        self,
        cfg: Universe,
        api_url: str,
        *,
        on_change: Callable[[list[str]], Awaitable[bool]],
        on_rtt_ms: Callable[[float], None] | None = None,
        refresh_min: int | None = None,
    ) -> None:
        self.cfg = cfg
        self.api_url = api_url
        self.on_change = on_change
        self.on_rtt_ms = on_rtt_ms
        self.refresh_s = (refresh_min or cfg.refresh_min) * 60.0
        self.coins: list[str] = []

    def apply_config(self, cfg: Universe) -> None:
        """Adopt reloaded config. Takes effect at the next refresh, never mid tick."""
        self.cfg = cfg
        self.refresh_s = cfg.refresh_min * 60.0

    async def refresh(self, client: httpx.AsyncClient) -> bool:
        """Re-select the universe and hand it to the streams. True when it was applied.

        `self.coins` advances only after `on_change` reports that every stream took the
        new set. It used to advance first, so a stream that raised mid-subscribe left the
        venues after it on the old universe while the next refresh compared equal and
        returned False: those venues then under-recorded that coin until a restart.
        """
        payload, rtt_ms = await fetch_meta_and_ctxs(client, self.api_url)
        if self.on_rtt_ms is not None:
            self.on_rtt_ms(rtt_ms)
        coins = select_universe(payload, self.cfg)
        if coins == self.coins:
            return False
        added = [c for c in coins if c not in self.coins]
        removed = [c for c in self.coins if c not in coins]
        log.info("universe_change", added=added, removed=removed, universe=coins, size=len(coins))
        if not await self.on_change(coins):
            log.warning("universe_partly_applied", universe=coins, size=len(coins))
            return False
        self.coins = coins
        return True

    async def run(self, stop: asyncio.Event) -> None:
        async with httpx.AsyncClient() as client:
            while not stop.is_set():
                try:
                    await self.refresh(client)
                except Exception as exc:
                    log.warning("universe_refresh_failed", error=f"{type(exc).__name__}: {exc}")
                    # Keep the current universe and retry sooner than the full interval;
                    # an empty universe would silently record nothing.
                    delay = min(60.0, self.refresh_s) if self.coins else 10.0
                else:
                    delay = self.refresh_s
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=delay)
