"""1m candles from the public `candleSnapshot` endpoint. Free, no AWS, just patient.

`candleSnapshot` returns at most 5000 candles per call, so a window is walked backwards
one page at a time until the API stops returning anything older. At 5000 minutes per
call that is about 3.5 days per request, so a 12 month pull for one coin is roughly 105
calls; the whole market is a few hours of polite polling.

The default coin set is every live perp from `metaAndAssetCtxs`, not the trading
universe. It is free, and the zone and regime features in later phases want context on
coins the bot will never trade. `--coins` narrows it for a quick partial run.

Requests are sync (`httpx.Client`): this is a batch job that sleeps between calls on
purpose, and async would buy nothing but a second code path.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, date, datetime
from datetime import time as time_of_day
from pathlib import Path
from typing import Any

import httpx

from liqfade.data.ingest.common import BackfillError, Manifest, write_partitioned
from liqfade.data.schemas import SOURCE_HL_API
from liqfade.log import get_logger

log = get_logger(__name__)

JOB = "candles"
VENUE = "hyperliquid"
INTERVAL = "1m"
MAX_CANDLES_PER_CALL = 5000
PAGE_MS = MAX_CANDLES_PER_CALL * 60_000
# The published weight allows far more than this. 250ms keeps a whole market pull well
# inside the limit without needing to model it.
SLEEP_S = 0.25
REQUEST_TIMEOUT_S = 30.0


def day_bounds(start: date, end: date) -> tuple[int, int]:
    """[start 00:00:00, end 23:59:59.999] in epoch milliseconds. Dates are inclusive."""
    if end < start:
        raise BackfillError(f"end {end} is before start {start}")
    begin = datetime.combine(start, time_of_day.min, tzinfo=UTC)
    finish = datetime.combine(end, time_of_day.max, tzinfo=UTC)
    return int(begin.timestamp() * 1000), int(finish.timestamp() * 1000)


def _post(client: httpx.Client, api_url: str, payload: dict[str, Any]) -> Any:
    response = client.post(f"{api_url}/info", json=payload, timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()
    return response.json()


def live_coins(client: httpx.Client, api_url: str) -> list[str]:
    """Every perp currently listed, delisted ones excluded. The free default coin set."""
    payload = _post(client, api_url, {"type": "metaAndAssetCtxs"})
    universe = payload[0].get("universe", [])
    return sorted(asset["name"] for asset in universe if not asset.get("isDelisted"))


def fetch_page(
    client: httpx.Client, api_url: str, coin: str, start_ms: int, end_ms: int
) -> list[dict[str, Any]]:
    """One `candleSnapshot` call. Returns the raw candle objects, oldest first."""
    payload = _post(
        client,
        api_url,
        {
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": INTERVAL,
                "startTime": start_ms,
                "endTime": end_ms,
            },
        },
    )
    return list(payload or [])


def walk_back(
    client: httpx.Client,
    api_url: str,
    coin: str,
    start_ms: int,
    end_ms: int,
    *,
    sleep_s: float = SLEEP_S,
) -> Iterator[list[dict[str, Any]]]:
    """Yield pages from `end_ms` backwards until `start_ms` or until the API runs dry.

    The window moves back by a full page every iteration whether or not the call
    returned anything, so a coin listed halfway through the range terminates instead of
    asking for the same empty window forever.
    """
    window_end = end_ms
    while window_end >= start_ms:
        window_start = max(start_ms, window_end - PAGE_MS + 1)
        page = fetch_page(client, api_url, coin, window_start, window_end)
        if not page:
            return
        yield page
        # Oldest candle in this page, so a short page still steps past what it returned.
        oldest = min(int(candle["t"]) for candle in page)
        window_end = min(window_start, oldest) - 1
        if sleep_s:
            time.sleep(sleep_s)


def to_rows(page: list[dict[str, Any]], start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    """Canonical `candles_1m` rows for the candles of `page` inside the window.

    The API rounds a window outwards, so a page can carry candles from outside the
    requested range; they are dropped here rather than written into a neighbouring day.
    """
    rows = []
    for candle in page:
        ts_ms = int(candle["t"])
        if not start_ms <= ts_ms <= end_ms:
            continue
        rows.append(
            {
                "coin": candle["s"],
                "ts_ms": ts_ms,
                "o": float(candle["o"]),
                "h": float(candle["h"]),
                "l": float(candle["l"]),
                "c": float(candle["c"]),
                "v": float(candle["v"]),
                "n": int(candle["n"]),
            }
        )
    return rows


def run(
    client: httpx.Client,
    data_dir: Path,
    api_url: str,
    start: date,
    end: date,
    *,
    coins: list[str] | None = None,
    estimate_only: bool = False,
    sleep_s: float = SLEEP_S,
) -> int:
    """Pull 1m candles for [start, end]. Free, so there is no cost gate to pass."""
    start_ms, end_ms = day_bounds(start, end)
    wanted = coins or live_coins(client, api_url)
    if not wanted:
        raise BackfillError("no coins to pull: metaAndAssetCtxs returned an empty universe")

    calls_per_coin = (end_ms - start_ms) // PAGE_MS + 1
    print(
        f"{len(wanted)} coins, {(end - start).days + 1} days, about "
        f"{len(wanted) * calls_per_coin} calls at {sleep_s}s apart "
        f"(~{len(wanted) * calls_per_coin * sleep_s / 60:.0f} min). No egress cost: this "
        "is the public API."
    )
    if estimate_only:
        return 0

    root = Path(data_dir) / "parquet"
    manifest = Manifest.load(data_dir, JOB)
    total = 0
    for coin in wanted:
        # One manifest entry per coin and range, so a re-run skips finished coins.
        key = f"{coin}/{start:%Y%m%d}-{end:%Y%m%d}"
        if manifest.done(key):
            continue
        written = 0
        for page in walk_back(client, api_url, coin, start_ms, end_ms, sleep_s=sleep_s):
            written += write_partitioned(
                root, "candles_1m", VENUE, SOURCE_HL_API, to_rows(page, start_ms, end_ms)
            )
        manifest.record(key, 0, {"candles_1m": written})
        total += written
        log.info("backfill_candles", coin=coin, rows=written)
        print(f"  {coin}: {written} candles")
    print(f"{total} candles written for {len(wanted)} coins")
    return 0
