"""The Coinalyze poller: market loading, chunking, the row it writes, and the api key.

`xchg_ctx` was the one canonical table with no parser test, so a mapping that quietly
wrote nulls for open interest or funding would have shipped and shown up as a hole in the
lake weeks later. `poll_once` already takes an injectable client, so this needs no
network: an `httpx.MockTransport` answers the four endpoints and records what was asked.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from typing import Any

import httpx
import pytest

from liqfade.config import load_settings
from liqfade.config.models import Coinalyze
from liqfade.data.schemas import SOURCE_RECORDER, rows_to_table, validate_table
from liqfade.log import setup_logging
from liqfade.recorder.streams.coinalyze import CoinalyzePoller, _chunks

CFG: Coinalyze = load_settings("paper", environ={}).venues.coinalyze
KEY = "coinalyze-secret-key-value"  # a fake, for the redaction test

# The exchange code is the suffix after the dot; `name` is what the lake calls the venue.
EXCHANGES = [
    {"code": "A", "name": "Binance"},
    {"code": "6", "name": "Bybit"},
    {"code": "3", "name": "OKX"},
    {"code": "H", "name": "Hyperliquid"},
    {"code": "F", "name": "Deribit"},  # a venue we do not stream liquidations for
]
MARKETS = [
    {"symbol": "BTCUSDT_PERP.A", "exchange": "A", "is_perpetual": True},
    {"symbol": "ETHUSDT_PERP.6", "exchange": "6", "is_perpetual": True},
    {"symbol": "1000PEPEUSDT_PERP.A", "exchange": "A", "is_perpetual": True},
    {"symbol": "BTCUSDT_PERP.F", "exchange": "F", "is_perpetual": True},  # wrong exchange
    {"symbol": "BTCUSDT_2026Q1.A", "exchange": "A", "is_perpetual": False},  # dated
    {"symbol": "NOTACOIN", "exchange": "A", "is_perpetual": True},  # unmappable shape
]
VALUES = {
    "open-interest": {"BTCUSDT_PERP.A": 1.25e9, "ETHUSDT_PERP.6": 4.0e8},
    "funding-rate": {"BTCUSDT_PERP.A": 0.0000125, "ETHUSDT_PERP.6": -0.00003},
    "predicted-funding-rate": {"BTCUSDT_PERP.A": 0.000011, "ETHUSDT_PERP.6": -0.00002},
}
UPDATE_MS = 1788099600000


class FakeCoinalyze:
    """Answers the four endpoints and keeps every request for the assertions."""

    def __init__(self, values: dict[str, dict[str, float]] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.values = VALUES if values is None else values

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=CFG.base_url, transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "exchanges":
            return httpx.Response(200, json=EXCHANGES)
        if path == "future-markets":
            return httpx.Response(200, json=MARKETS)
        asked = request.url.params.get("symbols", "").split(",")
        table = self.values.get(path, {})
        return httpx.Response(
            200,
            json=[
                {"symbol": symbol, "value": table[symbol], "update": UPDATE_MS}
                for symbol in asked
                if symbol in table
            ],
        )

    def asked(self, endpoint: str) -> list[list[str]]:
        """The symbol lists sent to one endpoint, in order."""
        return [
            request.url.params["symbols"].split(",")
            for request in self.requests
            if request.url.path.rsplit("/", 1)[-1] == endpoint
        ]

    def calls(self, endpoint: str) -> int:
        return sum(r.url.path.rsplit("/", 1)[-1] == endpoint for r in self.requests)


def _poller(fake: FakeCoinalyze, coins: list[str]) -> tuple[CoinalyzePoller, list[Any]]:
    rows: list[Any] = []
    poller = CoinalyzePoller(
        CFG, lambda table, venue, row: rows.append((table, venue, row)), api_key=KEY
    )
    poller.set_universe(coins)
    return poller, rows


# --- markets -------------------------------------------------------------------


async def test_load_markets_keeps_only_perpetuals_on_the_venues_we_stream() -> None:
    fake = FakeCoinalyze()
    poller, _ = _poller(fake, ["BTC"])
    async with fake.client() as client:
        await poller._load_markets(client)
    assert poller.markets == {
        "BTCUSDT_PERP.A": ("BTC", "binance"),
        "ETHUSDT_PERP.6": ("ETH", "bybit"),
        "1000PEPEUSDT_PERP.A": ("kPEPE", "binance"),
    }


async def test_markets_are_fetched_once_and_reused_across_polls() -> None:
    """One call at start, refreshed only on restart: the free tier is 40 calls/min."""
    fake = FakeCoinalyze()
    poller, _ = _poller(fake, ["BTC"])
    async with fake.client() as client:
        await poller.poll_once(client)
        await poller.poll_once(client)
    assert (fake.calls("future-markets"), fake.calls("exchanges")) == (1, 1)
    assert fake.calls("open-interest") == 2  # but the values are polled every time


# --- the row -------------------------------------------------------------------


async def test_one_row_per_market_with_open_interest_and_both_funding_rates() -> None:
    fake = FakeCoinalyze()
    poller, rows = _poller(fake, ["BTC", "ETH"])
    async with fake.client() as client:
        assert await poller.poll_once(client) == 2

    tables = {table for table, _, _ in rows}
    assert tables == {"xchg_ctx"}
    validate_table("xchg_ctx", rows_to_table("xchg_ctx", [r for _, _, r in rows], SOURCE_RECORDER))

    by_coin = {row["coin"]: row for _, _, row in rows}
    assert by_coin["BTC"] | {"rx_ms": 0} == {
        "venue": "binance",
        "coin": "BTC",
        "ts_ms": UPDATE_MS,
        "rx_ms": 0,
        "open_interest": 1.25e9,
        "funding": 0.0000125,
        "predicted_funding": 0.000011,
    }
    assert by_coin["ETH"]["venue"] == "bybit"
    assert by_coin["ETH"]["funding"] == -0.00003
    assert by_coin["BTC"]["rx_ms"] > 0  # a live poll, so it has a real receive time


async def test_a_market_only_one_endpoint_answered_for_keeps_the_others_null() -> None:
    """A null is honest; a zero funding rate would read as a real, flat market."""
    fake = FakeCoinalyze(values={"open-interest": {"BTCUSDT_PERP.A": 5.0}})
    poller, rows = _poller(fake, ["BTC"])
    async with fake.client() as client:
        assert await poller.poll_once(client) == 1
    (row,) = [row for _, _, row in rows]
    assert (row["open_interest"], row["funding"], row["predicted_funding"]) == (5.0, None, None)


async def test_a_universe_no_market_matches_polls_nothing() -> None:
    fake = FakeCoinalyze()
    poller, rows = _poller(fake, ["FARTCOIN"])
    async with fake.client() as client:
        assert await poller.poll_once(client) == 0
    assert rows == []
    assert fake.asked("open-interest") == []


async def test_the_poll_counts_as_one_message_however_many_markets_it_returned() -> None:
    fake = FakeCoinalyze()
    poller, _ = _poller(fake, ["BTC", "ETH"])
    async with fake.client() as client:
        await poller.poll_once(client)
    (state,) = poller.feed_states(UPDATE_MS)
    assert (state.name, state.msgs_1m) == ("coinalyze.xchg_ctx", 1)


# --- chunking ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "expected"),
    [(0, []), (1, [1]), (20, [20]), (21, [20, 1]), (45, [20, 20, 5])],
)
def test_chunks_splits_at_the_size_and_never_returns_an_empty_chunk(
    count: int, expected: list[int]
) -> None:
    assert [len(chunk) for chunk in _chunks([str(n) for n in range(count)], 20)] == expected


async def test_a_universe_over_the_chunk_size_is_asked_for_in_batches() -> None:
    """Each endpoint is one call per chunk, so the free tier's 40/min has to be respected."""
    markets = {f"C{n}USDT_PERP.A": (f"C{n}", "binance") for n in range(25)}
    fake = FakeCoinalyze(values={})
    poller, _ = _poller(fake, [coin for coin, _ in markets.values()])
    poller.markets = markets
    async with fake.client() as client:
        await poller.poll_once(client)
    assert [len(chunk) for chunk in fake.asked("open-interest")] == [20, 5]
    assert [len(chunk) for chunk in fake.asked("funding-rate")] == [20, 5]


# --- the api key ---------------------------------------------------------------


async def test_the_api_key_never_reaches_a_log_line() -> None:
    """It goes out in a header on every request, and this poller logs on every poll."""
    fake = FakeCoinalyze()
    poller, _ = _poller(fake, ["BTC"])
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        setup_logging(proc="unit", env="paper")
        async with fake.client() as client:
            await poller.poll_once(client)

    output = buffer.getvalue()
    assert output.strip(), "the poller logs nothing at all, so this proves nothing"
    for line in output.splitlines():
        assert KEY not in line
        json.loads(line)  # and it is still one JSON object per line


async def test_the_key_is_sent_as_a_header_and_not_in_the_url() -> None:
    """A query parameter would land in the httpx access log and in any proxy's."""
    fake = FakeCoinalyze()
    poller, _ = _poller(fake, ["BTC"])
    async with fake.client() as client:
        await poller.poll_once(client)
    assert fake.requests
    for request in fake.requests:
        assert request.headers["api_key"] == KEY
        assert KEY not in str(request.url)


@pytest.mark.parametrize("enabled, key", [(False, KEY), (True, "")])
async def test_a_poller_that_will_not_poll_declares_no_stream(enabled: bool, key: str) -> None:
    # Declared-but-idle would read as disconnected and page Telegram every ten minutes.
    import asyncio

    cfg = CFG.model_copy(update={"enabled": enabled})
    poller = CoinalyzePoller(cfg, lambda table, venue, row: None, api_key=key)
    assert poller.stats == {}
    assert poller.feed_states(0) == []
    stop = asyncio.Event()
    await asyncio.wait_for(poller.run(stop), timeout=1.0)  # returns at once, polls nothing
