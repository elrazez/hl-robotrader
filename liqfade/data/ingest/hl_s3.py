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

import os
from collections.abc import Iterable, Iterator
from datetime import date
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
    log_egress,
    parallel_pull,
    read_lz4_lines,
    write_partitioned,
)
from liqfade.data.parse import to_float, to_int, to_ms
from liqfade.data.schemas import SOURCE_HL_S3
from liqfade.log import get_logger
from liqfade.recorder.writer import lake_files

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

# The only columns `SideRuleCheck.add` reads. Named so the parent-side inspection can
# project them out of the staged files instead of materialising a whole partition.
SIDE_CHECK_TRADE_COLUMNS = ["coin", "ts_ms", "px"]
SIDE_CHECK_LIQ_COLUMNS = ["coin", "ts_ms", "side"]

# Raw JSON bytes parsed and written per batch. Counted in bytes rather than lines
# because a line is one block and a block during a cascade carries hundreds of events:
# on 2025-10-10 a few thousand lines are already gigabytes, while a quiet hour needs tens
# of thousands to fill one batch. Parsed rows cost several times their JSON, so this
# leaves room for a handful of workers on a 16 GB box.
BATCH_BYTES = 16 * 1024 * 1024


def day_prefix(day: date) -> str:
    return f"{PREFIX}/{day:%Y%m%d}/"


# --- parsing ------------------------------------------------------------------


def parse_fill(user: str, fill: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
    """One `fills_full` row from one `[user_address, fill]` event and its block."""
    liq = fill.get("liquidation") or {}
    return {
        "coin": fill.get("coin"),
        "ts_ms": to_ms(fill.get("time")),
        "rx_ms": 0,
        "block_time": to_ms(envelope.get("block_time")),
        "block_number": to_int(envelope.get("block_number")),
        "local_time": to_ms(envelope.get("local_time")),
        "user_address": user,
        "px": to_float(fill.get("px")),
        "sz": to_float(fill.get("sz")),
        "side": fill.get("side"),
        "start_position": to_float(fill.get("startPosition")),
        "dir": fill.get("dir"),
        "closed_pnl": to_float(fill.get("closedPnl")),
        "hash": fill.get("hash"),
        "oid": to_int(fill.get("oid")),
        "crossed": fill.get("crossed"),
        "fee": to_float(fill.get("fee")),
        "fee_token": fill.get("feeToken"),
        "tid": to_int(fill.get("tid")),
        "cloid": fill.get("cloid"),
        "builder": fill.get("builder"),
        "builder_fee": to_float(fill.get("builderFee")),
        "twap_id": to_int(fill.get("twapId")),
        "liq_user": liq.get("liquidatedUser"),
        "liq_mark_px": to_float(liq.get("markPx")),
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
        log.warning(
            "backfill_days_before_prefix",
            job=JOB,
            days=len(old),
            first=str(min(old)),
            last=str(max(old)),
            prefix=PREFIX,
        )
        print(
            f"WARNING: {len(old)} requested days ({min(old)} to {max(old)}) are before "
            f"{HOURLY_PREFIX_START}, when the {PREFIX}/ prefix begins. Older history lives "
            "under the node_fills and node_trades prefixes in a different format that this "
            "job does not parse; see docs/DATA_SOURCES.md. Those days are being skipped."
        )
    return [day for day in days if day >= HOURLY_PREFIX_START]


def stage_fills(local: Path, key: str, stage_root: Path) -> dict[str, int]:
    """The ObjectHandler for this job. Module level so worker processes can import it."""
    return write_one_file(stage_root, local)


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


def write_one_file(root: Path, path: Path) -> dict[str, int]:
    """Parse one downloaded object and write its three tables. Returns rows per table.

    Parsed in byte-bounded batches rather than all at once. Hours are not the same size:
    a quiet hour is 20 MB compressed while the 2025-10-10 cascade is 340 MB, which
    expands to tens of GB of Python objects and kills the worker holding it. Batching
    caps that at one batch's worth however big the object is.

    A batch boundary is always a line boundary, and a line is one block, so both halves
    of a trade stay in the same batch and `derive_trades` still sees its pairs. Each
    batch appends its own Parquet file to the partition, which readers glob and
    compaction folds, exactly as the recorder's hourly flushes already do.

    The side rule check is fed by the parent-side inspector in `run`, not from here: this
    runs inside a worker process, and processes cannot share an accumulator.
    """
    totals = {"fills_full": 0, "liquidations": 0, "trades": 0}
    for batch in _batches(read_lz4_lines(path), BATCH_BYTES):
        fills = list(parse_fills(batch))
        liquidations = derive_liquidations(fills)
        trades = derive_trades(fills)
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
            log.warning("backfill_day_empty", job=JOB, bucket=BUCKET, prefix=day_prefix(day))
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

        def rows_of(table: str, columns: list[str]) -> list[dict[str, Any]]:
            """Only the columns `SideRuleCheck.add` reads.

            Reading the whole staged `trades` partition into Python dicts is the same
            "millions of rows as Python objects" failure BATCH_BYTES was introduced to
            fix, moved into the parent process: a cascade hour is millions of rows over
            eight columns where three are wanted. The staging root is a `mkdtemp` on the
            internal SSD, not the lake, so a bare glob is safe here.
            """
            files = sorted((staged / table).rglob("*.parquet"))
            return [
                row
                for path in files
                for row in pq.read_table(path, columns=columns, partitioning=None).to_pylist()
            ]

        check.add(
            rows_of("liquidations", SIDE_CHECK_LIQ_COLUMNS),
            rows_of("trades", SIDE_CHECK_TRADE_COLUMNS),
        )

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
    print(log_egress(JOB, manifest))
    return 0


# --- rederive -----------------------------------------------------------------
#
# Rebuilding `liquidations` and `trades` from the local `fills_full` archive. Free: it
# touches no network. This is the whole point of the pay-once rule, and 02d is what it
# was kept for: `derive_trades` sorted on `(ts_ms, tid)` until 2026-09-04, and a
# Hyperliquid `tid` is a hash, so the year in the lake has an arbitrary order inside each
# millisecond. `fills_full` was never sorted and still carries the exchange's order.
#
# The work is vectorised through liqfade/data/derive/from_fills.py, not the dict
# functions above: a year is ~3bn fills and the dict path is hours. The dict functions
# stay as the reference the twins are tested against.

# The rewritten day is written under the compacted name, so `liqfade compact` leaves the
# partition alone afterwards and a reader still finds one file per table per day.
REDERIVE_OUTPUT = "all_day.parquet"

# Where the rewritten partition is built before any input is unlinked. Hidden, so
# `lake_files` never hands it to a reader, and a fixed name rather than a pid, so the run
# after a crash can find it and finish the job. See `_finish_interrupted`.
PENDING_OUTPUT = f".{REDERIVE_OUTPUT}.rederive-pending"

# Stamped into the Parquet footer of every partition this job writes, and the only thing
# the resume rule trusts. The file name cannot carry that on its own: `liqfade compact` produced
# exactly this name for every day of the year in 02b, out of the wrongly ordered rows
# this job exists to replace, so "one file called all_day.parquet" says nothing about
# whether the rows in it are right. The stamp is not a version either: after a genuine
# re-pull of `fills_full`, use --force.
REDERIVE_STAMP = (b"liqfade_rederive", b"1")

# How many times a day is re-listed when its `fills_full` files vanish mid-read. The
# compaction runs detached alongside this, oldest first, and folds a day's hourly files
# into one; a day it reaches while a worker is reading it gets listed and redone. Two
# retries is one more than the single fold that can happen to any given day.
RELIST_ATTEMPTS = 3

# What a day that did no work prints. `skipped` is the resume rule; `empty` is a day the
# S3 pull never covered, which is information rather than an error.
REDERIVE_STATUS = {
    "skipped": "already current, skipped",
    "empty": "no fills_full, nothing to rederive",
}


def _partition_is_current(root: Path, day: str) -> bool:
    """True when both derived tables are one file that this job wrote.

    The resume rule. The run is detached behind a retry loop and the mount times out an
    open every few hours, so a restart has to be cheap. It reads the footer rather than
    the mtime, because the `fills_full` compaction runs alongside this and rewrites a
    day's fills without changing a single row of them; comparing timestamps would redo
    every day it happened to reach after this job had already done it.
    """
    import pyarrow.parquet as pq

    from liqfade.recorder.writer import lake_files, partition_dir

    for table in ("trades", "liquidations"):
        files = lake_files(partition_dir(root, table, day))
        if len(files) != 1 or files[0].name != REDERIVE_OUTPUT:
            return False
        key, value = REDERIVE_STAMP
        if (pq.read_metadata(files[0]).metadata or {}).get(key) != value:
            return False
    return True


def _finish_interrupted(directory: Path) -> None:
    """Promote a finished `PENDING_OUTPUT` left behind by an interrupted `_assemble`.

    The temp is written before any input is unlinked and holds every row the partition is
    meant to end up with, so if it is complete it is the right answer and the partition
    around it is whatever the interrupted unlink loop got through. Promoting it first
    means the run that follows reads one file rather than a file plus the leftovers whose
    rows are already inside it, which is the doubling this ordering exists to prevent.

    "Complete" is a readable Parquet footer. A temp from a crash mid-write has none, and
    is deleted.
    """
    import pyarrow.parquet as pq

    pending = directory / PENDING_OUTPUT
    if not pending.exists():
        return
    try:
        pq.read_metadata(pending)
    except Exception:
        log.warning("rederive_pending_unreadable", path=str(pending))
        pending.unlink(missing_ok=True)
        return
    log.warning("rederive_pending_promoted", path=str(pending))
    for path in lake_files(directory):
        path.unlink()
    os.replace(pending, directory / REDERIVE_OUTPUT)


def _assemble(root: Path, table: str, day: str, derived: Any) -> tuple[int, int]:
    """Rewrite one day's partition: our rows become `derived`, other sources are kept.

    **No Hyperliquid row survives that is not ours.** This is only ever called for a day
    that has `fills_full`, so our derived rows are that day's complete Hyperliquid set,
    and any other Hyperliquid row in the partition is a second copy of an event we
    already hold: Tardis's on the twelve free first-of-month days, under the same `tid`,
    and the recorder's from the day it starts. `hl_s3` is the one source of Hyperliquid
    trades and liquidations; see docs/DATA_SOURCES.md, "Counting a trade once".

    The rule used to name Tardis specifically, which left a recorder-written Hyperliquid
    trade in `kept`. `kept` is concatenated ahead of ours and the sort is stable, so that
    row would sort first inside a shared millisecond: the same open-before-close failure
    02d exists to fix, waiting for the recorder to start.

    The rewrite is atomic, which it was not: it used to write `all_day.parquet` and only
    then unlink the hourly files it had read. A crash in that window, or one `unlink`
    failing on a mount that times out an open every few hours, left both, and the rerun
    then kept the old files' foreign rows *and* the copy of them already inside the new
    file, silently doubling every recorder and Tardis row in the partition.

    The order now is: write a hidden temp beside the output, check its row count, unlink
    every input, then `os.replace`. A crash anywhere in that window leaves the finished
    temp on disk, and `_finish_interrupted` promotes it on the next run, so the partition
    ends up with exactly one copy of the foreign rows whether the crash landed before the
    unlinks, between two of them, or after all of them.

    Returns (rows derived, rows in the partition). The two differ on any day the
    recorder or Tardis also wrote: 2026-03-01 is a Tardis free day, so its `trades`
    partition keeps 46.8M Binance, Bybit and OKX rows around our 3.4M.
    """
    import polars as pl

    from liqfade.data.schemas import schema_for, validate_table
    from liqfade.recorder.writer import lake_files, partition_dir

    directory = partition_dir(root, table, day)
    _finish_interrupted(directory)
    existing = sorted(lake_files(directory)) if directory.is_dir() else []
    columns = schema_for(table).names

    kept = []
    for path in existing:
        others = pl.read_parquet(path).filter(
            (pl.col("venue") != VENUE) & (pl.col("source") != SOURCE_HL_S3)
        )
        if others.height:
            kept.append(others.select(columns))

    from liqfade.data.derive.from_fills import stable_by_ts

    ours = derived.with_columns(source=pl.lit(SOURCE_HL_S3, pl.String)).select(columns)
    # One shared sort rule, stated once in `stable_by_ts`: on `ts_ms` alone, stably,
    # never on `tid`.
    merged = stable_by_ts(pl.concat([*kept, ours]))

    import os

    import pyarrow.parquet as pq

    arrow = merged.to_arrow().cast(schema_for(table))
    validate_table(table, arrow)  # schema only; the stamp below is footer metadata
    arrow = arrow.replace_schema_metadata(dict([REDERIVE_STAMP]))
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / REDERIVE_OUTPUT
    pending = directory / PENDING_OUTPUT

    from liqfade.recorder.writer import COMPRESSION

    try:
        pq.write_table(arrow, pending, compression=COMPRESSION)
        # The same check `compact_day` has, and for the same reason: a short output must
        # never replace its inputs. The expected count is the kept rows plus the derived
        # ones, not the inputs' own sum, because this is exactly where our old rows and
        # Tardis's Hyperliquid rows are meant to disappear.
        written = int(pq.read_metadata(pending).num_rows)
        if written != merged.height:
            raise BackfillError(
                f"{pending} has {written} rows, expected {merged.height}; leaving the "
                f"{len(existing)} input files in place"
            )
    except BaseException:
        pending.unlink(missing_ok=True)
        raise

    # From here the temp holds everything, so an interrupted unlink loses nothing:
    # `_finish_interrupted` promotes it on the next run.
    for path in existing:
        path.unlink()
    os.replace(pending, output)
    return int(ours.height), int(merged.height)


def rederive_day(data_dir: str, day: str, force: bool) -> tuple[str, dict[str, int], float]:
    """Rebuild one day. Returns a status, the rows derived per table, and its own seconds.

    The duration is measured here rather than in the parent, which submits all 365 days
    at once and would otherwise be timing each one from the start of the run.

    Module level and taking only picklable arguments, because this is the process pool's
    task. Status is `done`, `skipped` (already current) or `empty` (no archive for the
    day).

    The `fills_full` compaction runs detached alongside this and deletes a day's hourly
    files the moment it has folded them into one. That is the only race, and it is
    handled by listing again: if a read fails and the listing has changed underneath us,
    the day is redone from the new listing. If the listing is unchanged the read failure
    is real and is raised, which the detached retry loop turns into a restart.
    """
    import time

    import polars as pl

    from liqfade.data.derive.from_fills import (
        derive_liquidations_frame,
        derive_trades_frame,
        read_fills,
    )
    from liqfade.recorder.writer import lake_files, partition_dir

    root = Path(data_dir) / "parquet"
    fills_dir = partition_dir(root, "fills_full", day)
    began = time.monotonic()

    for _ in range(RELIST_ATTEMPTS):
        files = sorted(lake_files(fills_dir)) if fills_dir.is_dir() else []
        if not files:
            return "empty", {}, time.monotonic() - began
        if not force and _partition_is_current(root, day):
            return "skipped", {}, time.monotonic() - began
        try:
            fills = read_fills(files)
        # A bad read off this mount surfaces two ways, seen both within an hour of the
        # year run: OSError 60 (the mount timed out an open) and a polars PanicException
        # wrapping OSError 22. The panic is named explicitly because it derives from
        # BaseException, so no `except Exception` would ever reach it. Which of the two
        # it is says nothing; the listing does.
        except (OSError, pl.exceptions.PanicException) as exc:
            if sorted(lake_files(fills_dir)) != files:
                continue  # the compaction folded this day underneath us; list again
            # Every input is still there, so this is a real read failure. It is re-raised
            # as a BackfillError rather than as itself because a PanicException cannot be
            # pickled back to the parent process: the pool then reports a PicklingError
            # for `pyo3_runtime` and the actual cause never reaches the log.
            raise BackfillError(f"{day}: reading fills_full failed: {exc}") from None
        trades, trades_kept = _assemble(root, "trades", day, derive_trades_frame(fills))
        liquidations, liq_kept = _assemble(
            root, "liquidations", day, derive_liquidations_frame(fills)
        )
        return (
            "done",
            {
                "fills": int(fills.height),
                "trades": trades,
                "liquidations": liquidations,
                # Rows of other sources the rewrite preserved: the recorder's, and Tardis's
                # Binance, Bybit and OKX rows on the twelve free days.
                "kept": (trades_kept - trades) + (liq_kept - liquidations),
            },
            time.monotonic() - began,
        )
    raise BackfillError(f"{day}: fills_full kept changing under {RELIST_ATTEMPTS} listings")


def unconsumed(started: dict[Any, str], seen: set[str]) -> list[tuple[str, Any]]:
    """Days that finished but whose result nobody took, oldest submission first.

    `cancel_futures` cannot recall a day already handed to the pool's call queue, so when
    a failure stops the consuming loop a few more days run to completion during the
    shutdown. Their work is in the lake and stamped; this is what stops their row counts
    going unreported. The first year run was 26 days short in its own summary for exactly
    this reason, over a complete lake.
    """
    return [
        (day, future)
        for future, day in started.items()
        if day not in seen
        and future.done()
        and not future.cancelled()
        and future.exception() is None
    ]


def rederive_plan(data_dir: Path, days: list[str]) -> int:
    """Print what a rederive would read, rewrite and delete per day. Changes nothing.

    Sizes come from `stat`, not from the Parquet footers: the point is "how much am I
    about to rewrite", and reading a year of footers to answer it would cost more file
    opens than the run itself on this mount.
    """
    from liqfade.recorder.writer import lake_files, partition_dir

    root = Path(data_dir) / "parquet"
    read_gib, doomed, rewritten = 0.0, 0, 0
    for day in days:
        fills = lake_files(partition_dir(root, "fills_full", day))
        if not fills:
            print(f"{day}  {REDERIVE_STATUS['empty']}")
            continue
        if _partition_is_current(root, day):
            print(f"{day}  {REDERIVE_STATUS['skipped']}")
            continue
        deleting = [
            path
            for table in ("trades", "liquidations")
            for path in lake_files(partition_dir(root, table, day))
        ]
        gib = sum(path.stat().st_size for path in fills) / 1024**3
        read_gib += gib
        doomed += len(deleting)
        rewritten += 2
        print(
            f"{day}  read {len(fills):>4} fills_full files ({gib:6.2f} GiB)  "
            f"-> rewrite 2 partitions, delete {len(deleting):>4} files"
        )
    print(
        f"\nwould read {read_gib:.1f} GiB of fills_full, write {rewritten} partition files "
        f"and delete {doomed}. Nothing was changed."
    )
    return 0


def rederive(
    data_dir: Path, start: date, end: date, *, force: bool = False, workers: int = 2
) -> int:
    """Rebuild `liquidations` and `trades` from `fills_full` for [start, end], newest first.

    Newest first so this and the detached `fills_full` compaction, which walks oldest
    first, meet once in the middle instead of contending for the same day repeatedly.
    One day is one task; a worker holds a whole day, up to ~13M fills on a cascade day,
    which is what keeps the default worker count low.
    """
    import concurrent.futures
    import time

    days = [day.isoformat() for day in day_range(start, end, newest_first=True)]
    totals = {"fills": 0, "trades": 0, "liquidations": 0, "kept": 0}
    counts = {"done": 0, "skipped": 0, "empty": 0}
    began = time.monotonic()

    def report(day: str, status: str, rows: dict[str, int], seconds: float) -> None:
        counts[status] += 1
        for table, count in rows.items():
            totals[table] += count
        detail = (
            f"{rows['fills']:>10,} fills -> {rows['trades']:>9,} trades  "
            f"{rows['liquidations']:>7,} liquidations  {rows['kept']:>9,} kept"
            if status == "done"
            else REDERIVE_STATUS[status]
        )
        print(f"{day}  {detail}  {seconds:7.1f}s", flush=True)

    pool = concurrent.futures.ProcessPoolExecutor(max_workers=max(1, workers))
    started = {pool.submit(rederive_day, str(data_dir), day, force): day for day in days}
    seen: set[str] = set()

    def take(future: concurrent.futures.Future[Any], day: str) -> None:
        status, rows, seconds = future.result()
        seen.add(day)
        report(day, status, rows, seconds)
        log.info("rederived", date=day, status=status, seconds=round(seconds, 1), **rows)

    try:
        for future in concurrent.futures.as_completed(started):
            take(future, started[future])
    finally:
        # cancel_futures, so a failed day stops the run now instead of after the other
        # 364 have been worked through. The detached retry loop restarts it and the
        # resume rule skips everything already written.
        pool.shutdown(wait=True, cancel_futures=True)
        for day, future in unconsumed(started, seen):
            take(future, day)

    print(
        f"\n{counts['done']} days rederived, {counts['skipped']} already current, "
        f"{counts['empty']} with no archive, in {(time.monotonic() - began) / 60:.1f} min"
    )
    for table in ("trades", "liquidations"):
        print(f"  {table}: {totals[table]:,} rows derived from {totals['fills']:,} fills")
    print(f"  rows of other sources preserved in those partitions: {totals['kept']:,}")
    return 0
