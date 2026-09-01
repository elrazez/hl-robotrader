"""Hourly L2 book snapshots from the requester-pays `hyperliquid-archive` bucket.

Layout: `s3://hyperliquid-archive/market_data/{YYYYMMDD}/{H}/l2Book/{COIN}.lz4`, one
file per coin per hour, back to 2023. The backtester needs the book to model what a
taker order would really have paid, which is what these are for.

**`--coins` has no default and is required.** The bucket holds every coin, and the whole
market at hourly granularity is an expensive accident to have by mistake. Naming the
coins is the confirmation.

Files are snapshots, not an incremental feed, so a coin-hour is a handful of book states
rather than a stream. Only the top 20 levels are kept, matching what the recorder's
`l2Book` subscription delivers, so live and historical rows are the same shape.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

import orjson

from liqfade.data.ingest.common import (
    BackfillError,
    Manifest,
    check_cost_gate,
    day_range,
    download_object,
    enough_disk,
    estimate,
    list_objects,
    raw_path,
    read_lz4_lines,
    write_partitioned,
)
from liqfade.data.schemas import SOURCE_HL_ARCHIVE
from liqfade.log import get_logger

log = get_logger(__name__)

JOB = "l2"
BUCKET = "hyperliquid-archive"
VENUE = "hyperliquid"
L2_LEVELS = 20  # what the recorder records, so both sources are the same shape
HOURS = range(24)


def hour_prefix(day: date, hour: int) -> str:
    return f"market_data/{day:%Y%m%d}/{hour}/l2Book/"


def object_key(day: date, hour: int, coin: str) -> str:
    """The documented key for one coin-hour. Listing is what the job actually uses."""
    return f"{hour_prefix(day, hour)}{coin}.lz4"


def coin_of_key(key: str) -> str:
    """`market_data/20260905/11/l2Book/BTC.lz4` -> `BTC`."""
    return key.rsplit("/", 1)[-1].removesuffix(".lz4")


def _snapshot(obj: Any) -> dict[str, Any] | None:
    """Unwrap whatever envelope the archive uses around a raw `l2Book` payload.

    The archive has carried these as bare payloads and as `{"raw": {"data": {..}}}`
    envelopes, so the parser looks for the dict that actually holds `levels` rather than
    assuming a depth. Anything else is skipped rather than guessed at.
    """
    seen = 0
    while isinstance(obj, dict) and seen < 5:
        if "levels" in obj:
            return dict(obj)
        obj = obj.get("data") or obj.get("raw")
        seen += 1
    return None


def _level(levels: Any, index: int) -> dict[str, Any]:
    if not isinstance(levels, list) or index >= len(levels) or not isinstance(levels[index], dict):
        return {}
    return levels[index]


def _f(value: Any) -> float | None:
    return None if value is None or value == "" else float(value)


def parse_snapshots(lines: Iterable[bytes], coin: str) -> list[dict[str, Any]]:
    """Canonical `l2` rows, one per level per snapshot, top `L2_LEVELS` deep.

    `coin` comes from the key and is used when the payload omits it, which is the case
    in the flatter archive envelopes.
    """
    rows: list[dict[str, Any]] = []
    for line in lines:
        snapshot = _snapshot(orjson.loads(line))
        if snapshot is None:
            continue
        levels = snapshot.get("levels") or [[], []]
        bids, asks = [*levels, [], []][:2]
        ts_ms = snapshot.get("time")
        if ts_ms is None:
            continue
        depth = min(L2_LEVELS, max(len(bids), len(asks)))
        for index in range(depth):
            bid, ask = _level(bids, index), _level(asks, index)
            rows.append(
                {
                    "coin": snapshot.get("coin") or coin,
                    "ts_ms": int(ts_ms),
                    "rx_ms": 0,
                    "level": index,
                    "bid_px": _f(bid.get("px")),
                    "bid_sz": _f(bid.get("sz")),
                    "ask_px": _f(ask.get("px")),
                    "ask_sz": _f(ask.get("sz")),
                }
            )
    return rows


def run(
    client: Any,
    data_dir: Path,
    start: date,
    end: date,
    coins: list[str],
    *,
    estimate_only: bool = False,
    yes: bool = False,
    keep_raw: bool = False,
) -> int:
    """Pull hourly L2 snapshots for `coins` over [start, end], newest day first."""
    if not coins:
        raise BackfillError(
            "--coins is required for the l2 job: the archive holds every coin and a "
            "whole-market hourly pull is expensive. Name the coins the backtest trades."
        )

    root = Path(data_dir) / "parquet"
    manifest = Manifest.load(data_dir, JOB)
    wanted = set(coins)

    objects: list[dict[str, Any]] = []
    for day in day_range(start, end):
        for hour in HOURS:
            found = list_objects(client, BUCKET, hour_prefix(day, hour))
            objects.extend(obj for obj in found if coin_of_key(str(obj["Key"])) in wanted)

    todo = [obj for obj in objects if not manifest.done(str(obj["Key"]))]
    est = estimate(todo)
    skipped = len(objects) - len(todo)
    print(f"{est.line()}" + (f" ({skipped} already done, skipped)" if skipped else ""))
    if not objects:
        print(f"WARNING: no objects for {sorted(wanted)} under s3://{BUCKET}/market_data/")
    if estimate_only:
        return 0
    check_cost_gate(est, yes=yes)

    fetched = 0
    for obj in todo:
        key = str(obj["Key"])
        if not enough_disk(data_dir):
            print(
                f"WARNING: stopping, free disk is under the floor. Fetched {fetched} of "
                f"{len(todo)} objects. Re-run to continue where this left off."
            )
            break
        local = download_object(client, BUCKET, key, raw_path(data_dir, JOB, key))
        rows = parse_snapshots(read_lz4_lines(local), coin_of_key(key))
        written = write_partitioned(root, "l2", VENUE, SOURCE_HL_ARCHIVE, rows)
        manifest.record(key, int(obj.get("Size", 0)), {"l2": written})
        if not keep_raw:
            local.unlink()
        fetched += 1
        log.info("backfill_object", job=JOB, key=key, l2=written)

    print(f"fetched {fetched} objects this run, {len(manifest.objects)} in the manifest")
    print(f"  l2: {manifest.total_rows().get('l2', 0)} rows")
    print(f"  egress so far: {manifest.total_bytes() / 1024**3:.2f} GiB")
    return 0
