"""Tardis.dev free first-of-month CSVs. No signup, no key, no cost.

    https://datasets.tardis.dev/v1/{exchange}/{dataType}/{year}/{month}/01/{symbol}.csv.gz

Twelve days a year per exchange. That is a validation set, not a training set: use it to
check that cross-exchange liquidation behaviour in the recorded window looks like earlier
months, never to fit a threshold. See docs/DATA_SOURCES.md, "The one thing you cannot
buy".

Four data types, all free:

- `liquidations` and `trades` map onto the canonical schemas through `data/symbols.py`.
- `book_snapshot_5` and `derivative_ticker` keep their native Tardis columns in the side
  tables `tardis_book_snapshot_5` and `tardis_derivative_ticker`. Forcing a 5 level
  snapshot into the 20 level `l2` schema would invent fifteen empty levels, and the
  ticker has no canonical home at all, so neither is worth the distortion.

Timestamps in these files are microseconds and are converted to the milliseconds the
lake uses. `local_timestamp` is Tardis's collector's receive time, not ours, so it is not
carried into `rx_ms`: historical rows keep `rx_ms = 0` and no-lookahead checks stay
honest about which rows have a receive time we can trust.

A month that 404s is skipped and reported. Some exchange, data type and month
combinations legitimately do not exist, and Hyperliquid liquidations are the obvious one:
Hyperliquid has never published them on a public feed, so nobody, Tardis included, has
them.
"""

from __future__ import annotations

import csv
import gzip
import io
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from liqfade.data.ingest.common import Manifest, write_partitioned
from liqfade.data.schemas import SOURCE_TARDIS
from liqfade.data.symbols import to_venue_symbol
from liqfade.log import get_logger
from liqfade.recorder.writer import partition_dir, write_atomic

log = get_logger(__name__)

JOB = "tardis"
BASE_URL = "https://datasets.tardis.dev/v1"
REQUEST_TIMEOUT_S = 120.0

# Tardis exchange id -> the venue name the lake uses.
EXCHANGES = {
    "binance-futures": "binance",
    "bybit": "bybit",
    "okex-swap": "okx",
    "hyperliquid": "hyperliquid",
}
DATA_TYPES = ("liquidations", "trades", "book_snapshot_5", "derivative_ticker")
# The two that have no canonical schema keep their own columns in a side table.
SIDE_TABLES = {
    "book_snapshot_5": "tardis_book_snapshot_5",
    "derivative_ticker": "tardis_derivative_ticker",
}

# Bybit has published every liquidation since this date (`allLiquidation`); before it,
# and on Binance and OKX always, the public feed is throttled to one event per symbol per
# second and the recorded history is a lower bound. Same flag the recorder sets.
BYBIT_COMPLETE_FROM = date(2025, 2, 25)


def url_for(exchange: str, data_type: str, day: date, symbol: str) -> str:
    """The free-day URL. Only day 01 of a month is free, which is why day is a month."""
    return f"{BASE_URL}/{exchange}/{data_type}/{day.year}/{day.month:02d}/01/{symbol}.csv.gz"


def months(start: date, end: date) -> list[date]:
    """The first of every month touched by [start, end], newest first."""
    found: list[date] = []
    year, month = end.year, end.month
    while (year, month) >= (start.year, start.month):
        found.append(date(year, month, 1))
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return found


def is_complete(venue: str, day: date) -> bool:
    return venue == "bybit" and day >= BYBIT_COMPLETE_FROM


def _us_to_ms(value: Any) -> int | None:
    """Tardis timestamps are microseconds since epoch."""
    if value in (None, ""):
        return None
    return int(value) // 1000


def _f(value: Any) -> float | None:
    return None if value in (None, "") else float(value)


def _tid(value: Any) -> int | None:
    """Tardis trade ids are numeric on some venues and opaque strings on others."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def read_csv(payload: bytes) -> list[dict[str, str]]:
    """Rows of a gzipped Tardis CSV, as dicts keyed by the header row."""
    text = gzip.decompress(payload).decode("utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def to_liquidations(rows: Iterable[dict[str, str]], venue: str, day: date) -> list[dict[str, Any]]:
    """Canonical `liquidations` rows.

    Tardis normalises `side` to the liquidation order's side, so `sell` means the engine
    was selling and a long was liquidated. Same convention as the Binance and Bybit live
    parsers, and the field name reads the opposite way on every venue, which is why it is
    spelled out here.
    """
    from liqfade.data.symbols import to_coin

    complete = is_complete(venue, day)
    out = []
    for row in rows:
        symbol = row["symbol"]
        px, sz = _f(row["price"]), _f(row["amount"])
        out.append(
            {
                "venue": venue,
                "coin": to_coin(symbol, venue),
                "venue_symbol": symbol,
                "ts_ms": _us_to_ms(row["timestamp"]),
                "rx_ms": 0,
                "side": "long" if row["side"] == "sell" else "short",
                "px": px,
                "sz": sz,
                "notional_usd": None if px is None or sz is None else px * sz,
                "method": "unknown",
                "complete": complete,
            }
        )
    return out


def to_trades(rows: Iterable[dict[str, str]], venue: str) -> list[dict[str, Any]]:
    """Canonical `trades` rows. Tardis `side` is already the aggressor side."""
    from liqfade.data.symbols import to_coin

    return [
        {
            "venue": venue,
            "coin": to_coin(row["symbol"], venue),
            "ts_ms": _us_to_ms(row["timestamp"]),
            "rx_ms": 0,
            "px": _f(row["price"]),
            "sz": _f(row["amount"]),
            "side": "buy" if row["side"] == "buy" else "sell",
            "tid": _tid(row.get("id")),
        }
        for row in rows
    ]


def write_side_table(
    root: Path, table: str, venue: str, coin: str, day: date, payload: bytes
) -> int:
    """Write a Tardis CSV to its side table with the columns Tardis gave it.

    Types are inferred by pyarrow rather than declared: these tables have no canonical
    schema by design, and pinning one would mean re-deriving it every time Tardis adds a
    column.

    The file name carries the coin because `run` calls this once per coin for the same
    venue and day. It used to be `{venue}_00.parquet` for all of them, so every coin
    silently overwrote the one before and only the last survived.
    """
    import pyarrow.csv as pa_csv

    arrow = pa_csv.read_csv(io.BytesIO(gzip.decompress(payload)))
    if arrow.num_rows == 0:
        return 0
    directory = partition_dir(root, table, day.isoformat())
    directory.mkdir(parents=True, exist_ok=True)
    write_atomic(arrow, directory / f"{venue}_{coin}_00.parquet")
    return int(arrow.num_rows)


def fetch(client: httpx.Client, url: str) -> bytes | None:
    """The gzipped CSV, or None when Tardis has no file for that combination.

    Tardis answers "we do not carry this" two different ways: 404 for a month it has no
    file for, and 400 with `Data type 'x' is not supported for 'SYMBOL'` for a data type
    an exchange never had, which is how it replies to Hyperliquid liquidations. Both are
    answers about coverage rather than failures, so both are reported as missing and the
    pull carries on to the next file.
    """
    response = client.get(url, timeout=REQUEST_TIMEOUT_S, follow_redirects=True)
    if response.status_code in (httpx.codes.NOT_FOUND, httpx.codes.BAD_REQUEST):
        return None
    response.raise_for_status()
    return response.content


def run(
    client: httpx.Client,
    data_dir: Path,
    start: date,
    end: date,
    coins: list[str],
    *,
    exchanges: dict[str, str] | None = None,
    data_types: tuple[str, ...] = DATA_TYPES,
    estimate_only: bool = False,
) -> int:
    """Pull the free first-of-month files for `coins` across [start, end]. Free."""
    venues = exchanges or EXCHANGES
    wanted_months = months(start, end)
    targets = [
        (exchange, venue, data_type, day, coin)
        for day in wanted_months
        for exchange, venue in venues.items()
        for data_type in data_types
        for coin in coins
    ]
    print(
        f"{len(targets)} files: {len(wanted_months)} months x {len(venues)} exchanges x "
        f"{len(data_types)} data types x {len(coins)} coins. Free, no AWS involved."
    )
    if estimate_only:
        return 0

    root = Path(data_dir) / "parquet"
    manifest = Manifest.load(data_dir, JOB)
    missing: list[str] = []
    totals: dict[str, int] = {}

    for exchange, venue, data_type, day, coin in targets:
        symbol = to_venue_symbol(coin, venue)
        url = url_for(exchange, data_type, day, symbol)
        if manifest.done(url):
            continue
        payload = fetch(client, url)
        if payload is None:
            missing.append(url.removeprefix(f"{BASE_URL}/"))
            # Recorded as done with zero rows: a 404 is an answer, not a failure, and
            # re-asking every run wastes the next hour of somebody's life.
            manifest.record(url, 0, {})
            continue

        if data_type in SIDE_TABLES:
            table = SIDE_TABLES[data_type]
            written = write_side_table(root, table, venue, coin, day, payload)
        else:
            rows = read_csv(payload)
            table = data_type
            parsed = (
                to_liquidations(rows, venue, day)
                if data_type == "liquidations"
                else to_trades(rows, venue)
            )
            written = write_partitioned(root, table, venue, SOURCE_TARDIS, parsed)

        manifest.record(url, len(payload), {table: written})
        totals[table] = totals.get(table, 0) + written
        log.info("backfill_tardis", venue=venue, data_type=data_type, day=str(day), rows=written)

    for table, count in sorted(totals.items()):
        print(f"  {table}: {count} rows")
    if missing:
        print(f"\n{len(missing)} files Tardis does not have (404, skipped):")
        for path in missing:
            print(f"  {path}")
    return 0
