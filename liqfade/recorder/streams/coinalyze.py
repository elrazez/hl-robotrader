"""Coinalyze poller: cross-exchange open interest and funding into `xchg_ctx`.

Not a stream. Coinalyze is a REST API on the free tier (40 calls/min), so this polls
every `venues.coinalyze.poll_s` and writes one row per (exchange, coin). It exists for
regime context (is OI building or unwinding across venues, is funding stretched), not
for anything at scalp resolution; docs/DATA_SOURCES.md explains why the intraday history
is useless to us and the live poll is not.

Three calls per poll per symbol chunk: open interest, funding, predicted funding. Open
interest is requested with `convert_to_usd`, so the column is USD and comparable across
venues. `COINALYZE_API_KEY` is read from the environment at start; without it the poller
logs once and stays idle rather than failing the recorder.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from typing import Any

import httpx
import orjson

from liqfade.config.models import Coinalyze
from liqfade.data.symbols import to_coin
from liqfade.log import get_logger
from liqfade.recorder.streams.base import RowSink, StreamStats

log = get_logger(__name__)

VENUE = "coinalyze"
TABLES = ("xchg_ctx",)
API_KEY_ENV = "COINALYZE_API_KEY"
SYMBOLS_PER_CALL = 20  # keeps each response small enough to parse in one go
# Exchanges worth a row. Coinalyze covers dozens; these are the ones whose liquidations
# we also stream, so the OI and funding line up with the rest of the lake.
EXCHANGES = ("binance", "bybit", "okx", "hyperliquid")

ENDPOINTS = {
    "open_interest": ("open-interest", {"convert_to_usd": "true"}),
    "funding": ("funding-rate", {}),
    "predicted_funding": ("predicted-funding-rate", {}),
}


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[start : start + size] for start in range(0, len(items), size)]


class CoinalyzePoller:
    """Polls current OI and funding for the universe and sinks one row per market."""

    def __init__(self, cfg: Coinalyze, sink: RowSink, *, api_key: str | None = None) -> None:
        self.cfg = cfg
        self.sink = sink
        self.api_key = api_key if api_key is not None else os.environ.get(API_KEY_ENV, "")
        self.coins: list[str] = []
        # symbol -> (coin, exchange name). Fetched once from future-markets.
        self.markets: dict[str, tuple[str, str]] = {}
        self.stats = {f"{VENUE}.xchg_ctx": StreamStats(name=f"{VENUE}.xchg_ctx")}

    @property
    def _stat(self) -> StreamStats:
        return self.stats[f"{VENUE}.xchg_ctx"]

    def set_universe(self, coins: list[str]) -> None:
        self.coins = sorted(coins)

    def feed_states(self, now_ms: int) -> list[Any]:
        return [stat.feed_state(now_ms) for stat in self.stats.values()]

    # --- http ----------------------------------------------------------------

    async def _get(self, client: httpx.AsyncClient, path: str, params: dict[str, str]) -> Any:
        response = await client.get(path, params=params, headers={"api_key": self.api_key})
        response.raise_for_status()
        return orjson.loads(response.content)

    async def _load_markets(self, client: httpx.AsyncClient) -> None:
        """symbol -> (coin, exchange). One call at start, refreshed only on restart."""
        codes = {
            str(row["code"]): str(row["name"]).lower()
            for row in await self._get(client, "exchanges", {})
        }
        markets: dict[str, tuple[str, str]] = {}
        for row in await self._get(client, "future-markets", {}):
            symbol = str(row.get("symbol", ""))
            if not row.get("is_perpetual"):
                continue
            exchange = codes.get(str(row.get("exchange")), str(row.get("exchange")))
            if exchange not in EXCHANGES:
                continue
            coin = to_coin(symbol, VENUE)
            if coin:
                markets[symbol] = (coin, exchange)
        self.markets = markets
        log.info(
            "coinalyze_markets_loaded",
            markets=len(markets),
            exchanges=sorted(set(codes.values()) & set(EXCHANGES)),
        )

    def _symbols(self) -> list[str]:
        wanted = set(self.coins)
        return sorted(s for s, (coin, _) in self.markets.items() if coin in wanted)

    async def poll_once(self, client: httpx.AsyncClient) -> int:
        """One full poll. Returns the number of rows sunk."""
        if not self.markets:
            await self._load_markets(client)
        symbols = self._symbols()
        if not symbols:
            return 0

        merged: dict[str, dict[str, Any]] = {}
        for field, (path, extra) in ENDPOINTS.items():
            for chunk in _chunks(symbols, SYMBOLS_PER_CALL):
                payload = await self._get(client, path, {"symbols": ",".join(chunk), **extra})
                for entry in payload:
                    symbol = str(entry.get("symbol", ""))
                    if symbol not in self.markets:
                        continue
                    row = merged.setdefault(symbol, {})
                    row[field] = float(entry["value"])
                    row["ts_ms"] = max(int(entry.get("update") or 0), int(row.get("ts_ms") or 0))

        rx_ms = int(time.time() * 1000)
        self._stat.record_message(rx_ms)  # one poll is one message, whatever it returned
        for symbol, values in merged.items():
            coin, exchange = self.markets[symbol]
            self.sink(
                "xchg_ctx",
                VENUE,
                {
                    "venue": exchange,
                    "coin": coin,
                    "ts_ms": int(values.get("ts_ms") or rx_ms),
                    "rx_ms": rx_ms,
                    "open_interest": values.get("open_interest"),
                    "funding": values.get("funding"),
                    "predicted_funding": values.get("predicted_funding"),
                },
            )
        return len(merged)

    async def run(self, stop: asyncio.Event) -> None:
        if not self.api_key:
            log.warning("coinalyze_disabled", reason=f"{API_KEY_ENV} is not set")
            return
        async with httpx.AsyncClient(base_url=self.cfg.base_url, timeout=20.0) as client:
            while not stop.is_set():
                try:
                    rows = await self.poll_once(client)
                    self._stat.connected = True
                    log.info("coinalyze_poll", rows=rows)
                except Exception as exc:
                    self._stat.connected = False
                    log.warning("coinalyze_poll_failed", error=f"{type(exc).__name__}: {exc}")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.cfg.poll_s)
