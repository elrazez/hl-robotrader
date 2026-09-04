"""Hyperliquid exact fills from requester-pays S3, and the tables derived from them.

This is the pull that costs money, and the only source of exact Hyperliquid
liquidations. One object per hour under `node_fills_by_block/hourly/YYYYMMDD/HH.lz4`,
each an LZ4 frame of newline delimited block envelopes:

    {"local_time": .., "block_time": .., "block_number": .., "events": [[user, fill], ..]}

Three tables come out of one download:

- `fills_full`, the pay-once archive: every field of every fill for every coin, no
  universe filter. Roughly 1 GB per day of history.
- `liquidations` and `trades`, the canonical tables, **derived** from `fills_full` by the
  pure functions below. `liqfade backfill rederive` rebuilds them from the local archive
  at zero cost, so a parsing rule that turns out wrong is a local fix rather than a
  second egress bill. Never re-download to fix a derivation.

**Counting a liquidation once.** Fills arrive in maker and taker pairs, so every event
appears twice. The dedup key is `(block_time, coin, tid)`. Getting this wrong doubles
every liquidation number and makes the strategy look far better than it is.

**Which side was liquidated.** A liquidation fill carries `liquidation.liquidatedUser`.
Find the record whose `user_address` is that address: it is the forced party, and its
own `side` says what it was forced to do. Forced to sell (`A`) means it was long, so
longs were liquidated. `verify_side_rule()` checks this against price action on the
first day pulled and prints the result, because an inverted side rule would flip the
sign of the entire strategy.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import orjson

from liqfade.data.ingest.common import (
    BackfillError,
    Manifest,
    check_cost_gate,
    day_range,
    estimate,
    list_objects,
    parallel_pull,
    read_lz4_lines,
    replace_source_rows,
    write_partitioned,
)
from liqfade.data.schemas import SOURCE_HL_S3
from liqfade.log import get_logger

log = get_logger(__name__)

JOB = "s3-fills"
BUCKET = "hl-mainnet-node-data"
PREFIX = "node_fills_by_block/hourly"
VENUE = "hyperliquid"
TABLES = ("fills_full", "liquidations", "trades")

# The `hourly/` prefix starts here. Older history lives under the `node_fills` and
# `node_trades` prefixes in a different format that this parser does not read; see
# docs/DATA_SOURCES.md. Days before this are reported, never silently skipped.
HOURLY_PREFIX_START = date(2025, 7, 27)

# Which side was liquidated, given the forced party's own side on its fill. Flip this
# pair, and only this pair, if verify_side_rule() ever reports the rule inverted.
LIQUIDATED_ON_SELL = "long"  # forced to sell (side "A") means it held a long
LIQUIDATED_ON_BUY = "short"

# Seconds of price action either side of a liquidation used by the side rule check.
SIDE_CHECK_WINDOW_S = 60

# Raw JSON bytes parsed and written per batch. Counted in bytes rather than lines
# because a line is one block and a block during a cascade carries hundreds of events:
# on 2025-10-10 a few thousand lines are already gigabytes, while a quiet hour needs tens
# of thousands to fill one batch. Parsed rows cost several times their JSON, so this
# leaves room for a handful of workers on a 16 GB box.
BATCH_BYTES = 16 * 1024 * 1024


def day_prefix(day: date) -> str:
    return f"{PREFIX}/{day:%Y%m%d}/"


# --- parsing ------------------------------------------------------------------


def _ms(value: Any) -> int | None:
    """Milliseconds UTC from either an epoch number or an ISO 8601 string.

    Envelope times in the node data are ISO strings, fill times are epoch milliseconds,
    and both have to end up as the integer milliseconds the lake uses everywhere.
    """
    if value is None or value == "":
        return None
    if isinstance(value, int | float):
        return int(value)
    text = str(value).replace("Z", "+00:00")
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp() * 1000)


def _f(value: Any) -> float | None:
    """Hyperliquid sends every number as a string. None and "" become null, not 0.0."""
    if value is None or value == "":
        return None
    return float(value)


def _i(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def parse_fill(user: str, fill: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
    """One `fills_full` row from one `[user_address, fill]` event and its block."""
    liq = fill.get("liquidation") or {}
    return {
        "coin": fill.get("coin"),
        "ts_ms": _ms(fill.get("time")),
        "rx_ms": 0,
        "block_time": _ms(envelope.get("block_time")),
        "block_number": _i(envelope.get("block_number")),
        "local_time": _ms(envelope.get("local_time")),
        "user_address": user,
        "px": _f(fill.get("px")),
        "sz": _f(fill.get("sz")),
        "side": fill.get("side"),
        "start_position": _f(fill.get("startPosition")),
        "dir": fill.get("dir"),
        "closed_pnl": _f(fill.get("closedPnl")),
        "hash": fill.get("hash"),
        "oid": _i(fill.get("oid")),
        "crossed": fill.get("crossed"),
        "fee": _f(fill.get("fee")),
        "fee_token": fill.get("feeToken"),
        "tid": _i(fill.get("tid")),
        "cloid": fill.get("cloid"),
        "builder": fill.get("builder"),
        "builder_fee": _f(fill.get("builderFee")),
        "twap_id": _i(fill.get("twapId")),
        "liq_user": liq.get("liquidatedUser"),
        "liq_mark_px": _f(liq.get("markPx")),
        "liq_method": liq.get("method"),
    }


def parse_fills(lines: Iterable[bytes]) -> Iterator[dict[str, Any]]:
    """Every fill in a `node_fills_by_block` file, one `fills_full` row per event."""
    for line in lines:
        envelope = orjson.loads(line)
        for event in envelope.get("events") or []:
            user, fill = event[0], event[1]
            yield parse_fill(user, fill, envelope)


# --- derivation ---------------------------------------------------------------


def _groups(fills: Iterable[dict[str, Any]]) -> dict[tuple[Any, Any, Any], list[dict[str, Any]]]:
    """Fills keyed by (block_time, coin, tid): one key is one trade, seen from both sides."""
    grouped: dict[tuple[Any, Any, Any], list[dict[str, Any]]] = {}
    for fill in fills:
        grouped.setdefault((fill["block_time"], fill["coin"], fill["tid"]), []).append(fill)
    return grouped


def derive_trades(fills: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Canonical `trades` rows: one per (block_time, coin, tid), aggressor side.

    `crossed` marks the taker, so the taker's own `side` is the aggressor side. When only
    the maker's half of a pair is present the aggressor is the opposite side, which is
    why this does not simply skip a group with no crossed record.
    """
    rows = []
    for group in _groups(fills).values():
        taker = next((fill for fill in group if fill.get("crossed")), None)
        if taker is not None:
            side = "buy" if taker["side"] == "B" else "sell"
            source_fill = taker
        else:
            source_fill = group[0]
            side = "sell" if source_fill["side"] == "B" else "buy"
        rows.append(
            {
                "venue": VENUE,
                "coin": source_fill["coin"],
                "ts_ms": source_fill["ts_ms"],
                "rx_ms": 0,
                "px": source_fill["px"],
                "sz": source_fill["sz"],
                "side": side,
                "tid": source_fill["tid"],
            }
        )
    # Sort on ts_ms alone. `tid` is a hash, not a sequence (997296749534590 precedes
    # 653061949558227 in one sweep), so adding it to the key replaces the exchange's
    # order inside a millisecond with an arbitrary one, and a minute's first and last
    # trade are then the wrong ones. Python's sort is stable, and `_groups` yields in
    # first-appearance order, so this keeps the block's event order. Measured against
    # `candleSnapshot` on 2026-09-04: tid order matched the API's open on 76% of BTC
    # minutes, block order on 100%. See liqfade/data/derive/candles.py.
    rows.sort(key=lambda row: row["ts_ms"] or 0)
    return rows


def derive_liquidations(fills: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Canonical `liquidations` rows: one per liquidation event, side of the forced party.

    Only fills carrying a `liquidation` object count, and a maker plus taker pair counts
    once. The forced party is the record whose `user_address` equals
    `liquidation.liquidatedUser`; if only the counterparty's half is in the file, its
    side is inverted to recover the forced party's.
    """
    rows = []
    for group in _groups(fills).values():
        liq_user = next((fill["liq_user"] for fill in group if fill.get("liq_user")), None)
        if liq_user is None:
            continue
        victim = next((fill for fill in group if fill["user_address"] == liq_user), None)
        if victim is not None:
            forced_to_sell = victim["side"] == "A"
            source_fill = victim
        else:
            source_fill = group[0]
            forced_to_sell = source_fill["side"] == "B"
        px, sz = source_fill["px"], source_fill["sz"]
        rows.append(
            {
                "venue": VENUE,
                "coin": source_fill["coin"],
                "venue_symbol": source_fill["coin"],
                "ts_ms": source_fill["ts_ms"],
                "rx_ms": 0,
                "side": LIQUIDATED_ON_SELL if forced_to_sell else LIQUIDATED_ON_BUY,
                "px": px,
                "sz": sz,
                "notional_usd": None if px is None or sz is None else px * sz,
                "method": source_fill["liq_method"] or "unknown",
                "complete": True,
            }
        )
    rows.sort(key=lambda row: row["ts_ms"] or 0)
    return rows


# --- side rule verification ---------------------------------------------------


class SideRuleCheck:
    """Accumulates the price move around each liquidation, split by liquidated side.

    Long liquidations are forced selling, so the price into them should be falling. If
    the mean return before long liquidations is not below the mean before short
    liquidations, the side rule is inverted and every downstream sign is wrong.
    """

    def __init__(self, window_s: int = SIDE_CHECK_WINDOW_S) -> None:
        self.window_ms = window_s * 1000
        self.totals: dict[str, list[float]] = {"long": [], "short": []}

    def add(self, liquidations: list[dict[str, Any]], trades: list[dict[str, Any]]) -> None:
        """Fold in one file's worth of events. Trades supply the price series."""
        from bisect import bisect_right

        series: dict[str, tuple[list[int], list[float]]] = {}
        for trade in sorted(trades, key=lambda row: row["ts_ms"] or 0):
            if trade["px"] is None or trade["ts_ms"] is None:
                continue
            times, prices = series.setdefault(trade["coin"], ([], []))
            times.append(trade["ts_ms"])
            prices.append(trade["px"])

        for liq in liquidations:
            found = series.get(liq["coin"])
            if found is None or liq["ts_ms"] is None:
                continue
            times, prices = found
            at = bisect_right(times, liq["ts_ms"]) - 1
            before = bisect_right(times, liq["ts_ms"] - self.window_ms) - 1
            # before < 0 means the window opens before this file starts: no baseline.
            if before < 0 or at < 0 or prices[before] <= 0:
                continue
            self.totals[liq["side"]].append(
                (prices[at] - prices[before]) / prices[before] * 10_000.0
            )

    def result(self) -> dict[str, Any]:
        means = {
            side: (sum(values) / len(values) if values else None)
            for side, values in self.totals.items()
        }
        counts = {side: len(values) for side, values in self.totals.items()}
        if means["long"] is None or means["short"] is None:
            verdict = "inconclusive"
        elif means["long"] < means["short"]:
            verdict = "consistent"
        else:
            verdict = "INVERTED"
        return {"means_bps": means, "counts": counts, "verdict": verdict}

    def report(self) -> str:
        result = self.result()
        means, counts = result["means_bps"], result["counts"]

        def fmt(side: str) -> str:
            mean = means[side]
            return f"{side}: n={counts[side]} mean {SIDE_CHECK_WINDOW_S}s return " + (
                "n/a" if mean is None else f"{mean:+.2f} bps"
            )

        lines = [
            f"side rule check ({SIDE_CHECK_WINDOW_S}s price move into each liquidation):",
            f"  {fmt('long')}",
            f"  {fmt('short')}",
        ]
        if result["verdict"] == "consistent":
            lines.append("  OK: long liquidations follow falling prices, as the rule assumes.")
        elif result["verdict"] == "INVERTED":
            lines.append(
                "  INVERTED: long liquidations follow RISING prices. Flip LIQUIDATED_ON_SELL "
                "and LIQUIDATED_ON_BUY in liqfade/data/ingest/hl_s3.py, record the correction "
                "in docs/DATA_SOURCES.md, and run `liqfade backfill rederive`."
            )
        else:
            lines.append("  inconclusive: too few liquidations with a price baseline.")
        return "\n".join(lines)


# --- the job ------------------------------------------------------------------


def _warn_before_hourly_prefix(days: list[date]) -> list[date]:
    """Split off days the hourly prefix does not cover, loudly."""
    old = [day for day in days if day < HOURLY_PREFIX_START]
    if old:
        print(
            f"WARNING: {len(old)} requested days ({min(old)} to {max(old)}) are before "
            f"{HOURLY_PREFIX_START}, when the {PREFIX}/ prefix begins. Older history lives "
            "under the node_fills and node_trades prefixes in a different format that this "
            "job does not parse; see docs/DATA_SOURCES.md. Those days are being skipped."
        )
    return [day for day in days if day >= HOURLY_PREFIX_START]


def stage_fills(local: Path, key: str, stage_root: Path) -> dict[str, int]:
    """The ObjectHandler for this job. Module level so worker processes can import it."""
    return write_one_file(stage_root, local, None)


def _batches(lines: Iterable[bytes], max_bytes: int) -> Iterator[list[bytes]]:
    """Group lines into batches of at most `max_bytes` of JSON, splitting only on lines.

    A line that is on its own larger than the budget still goes out alone: the point is
    to bound how much is held at once, not to guarantee a size.
    """
    batch: list[bytes] = []
    size = 0
    for line in lines:
        batch.append(line)
        size += len(line)
        if size >= max_bytes:
            yield batch
            batch, size = [], 0
    if batch:
        yield batch


def write_one_file(root: Path, path: Path, check: SideRuleCheck | None = None) -> dict[str, int]:
    """Parse one downloaded object and write its three tables. Returns rows per table.

    Parsed in byte-bounded batches rather than all at once. Hours are not the same size:
    a quiet hour is 20 MB compressed while the 2025-10-10 cascade is 340 MB, which
    expands to tens of GB of Python objects and kills the worker holding it. Batching
    caps that at one batch's worth however big the object is.

    A batch boundary is always a line boundary, and a line is one block, so both halves
    of a trade stay in the same batch and `derive_trades` still sees its pairs. Each
    batch appends its own Parquet file to the partition, which readers glob and
    compaction folds, exactly as the recorder's hourly flushes already do.
    """
    totals = {"fills_full": 0, "liquidations": 0, "trades": 0}
    for batch in _batches(read_lz4_lines(path), BATCH_BYTES):
        fills = list(parse_fills(batch))
        liquidations = derive_liquidations(fills)
        trades = derive_trades(fills)
        if check is not None:
            check.add(liquidations, trades)
        totals["fills_full"] += write_partitioned(root, "fills_full", VENUE, SOURCE_HL_S3, fills)
        totals["liquidations"] += write_partitioned(
            root, "liquidations", VENUE, SOURCE_HL_S3, liquidations
        )
        totals["trades"] += write_partitioned(root, "trades", VENUE, SOURCE_HL_S3, trades)
    return totals


def run(
    client: Any,
    data_dir: Path,
    start: date,
    end: date,
    *,
    estimate_only: bool = False,
    yes: bool = False,
    keep_raw: bool = False,
    workers: int = 1,
    client_factory: Any = None,
) -> int:
    """Pull fills for [start, end], newest day first. Returns a process exit code."""
    days = _warn_before_hourly_prefix(day_range(start, end))
    if not days:
        raise BackfillError(
            f"no days in range are covered by {PREFIX}/, which begins {HOURLY_PREFIX_START}"
        )

    manifest = Manifest.load(data_dir, JOB)

    objects: list[dict[str, Any]] = []
    for day in days:
        found = list_objects(client, BUCKET, day_prefix(day))
        if not found:
            print(f"WARNING: nothing under s3://{BUCKET}/{day_prefix(day)}")
        objects.extend(found)

    todo = [obj for obj in objects if not manifest.done(str(obj["Key"]))]
    est = estimate(todo)
    skipped = len(objects) - len(todo)
    print(f"{est.line()}" + (f" ({skipped} already done, skipped)" if skipped else ""))
    if estimate_only:
        return 0
    check_cost_gate(est, yes=yes)

    check = SideRuleCheck()
    first_day = todo[0]["Key"].rsplit("/", 2)[-2] if todo else None

    def inspect(staged: Path, key: str) -> None:
        # Parent side, before the merge: feed the side rule check from the staged files
        # of the first day pulled. Workers cannot share the check across processes.
        if key.rsplit("/", 2)[-2] != first_day:
            return
        import pyarrow.parquet as pq

        def rows_of(table: str) -> list[dict[str, Any]]:
            files = sorted((staged / table).rglob("*.parquet"))
            return [
                row for path in files for row in pq.read_table(path, partitioning=None).to_pylist()
            ]

        check.add(rows_of("liquidations"), rows_of("trades"))

    fetched = parallel_pull(
        client,
        BUCKET,
        todo,
        data_dir,
        JOB,
        manifest,
        stage_fills,
        workers=workers,
        keep_raw=keep_raw,
        inspect=inspect,
        client_factory=client_factory,
    )

    print(f"\n{check.report()}\n")
    totals = manifest.total_rows()
    print(f"fetched {fetched} objects this run, {len(manifest.objects)} in the manifest")
    for table in TABLES:
        print(f"  {table}: {totals.get(table, 0)} rows")
    print(f"  egress so far: {manifest.total_bytes() / 1024**3:.2f} GiB")
    return 0


def rederive(data_dir: Path, start: date, end: date) -> int:
    """Rebuild `liquidations` and `trades` from the local `fills_full` archive.

    Free: it touches no network. This is the whole point of the pay-once rule. Rows from
    other sources in the same partitions are preserved.
    """
    import pyarrow.parquet as pq

    from liqfade.recorder.writer import partition_dir

    root = Path(data_dir) / "parquet"
    total = {"liquidations": 0, "trades": 0}
    for day in day_range(start, end, newest_first=False):
        stamp = day.isoformat()
        directory = partition_dir(root, "fills_full", stamp)
        files = sorted(directory.glob("*.parquet")) if directory.is_dir() else []
        if not files:
            print(f"{stamp}: no fills_full, nothing to rederive")
            continue
        fills = [
            row for path in files for row in pq.read_table(path, partitioning=None).to_pylist()
        ]
        for table, rows in (
            ("liquidations", derive_liquidations(fills)),
            ("trades", derive_trades(fills)),
        ):
            written = replace_source_rows(root, table, stamp, VENUE, SOURCE_HL_S3, rows)
            total[table] += written
        print(f"{stamp}: {len(fills)} fills -> {total}")
    print(f"rederived {total['liquidations']} liquidations and {total['trades']} trades")
    return 0
