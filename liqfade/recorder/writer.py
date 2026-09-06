"""Parquet writing and nightly compaction for the data lake.

Layout, per docs/DATA_SOURCES.md:

    data/parquet/{table}/date=YYYY-MM-DD/{venue}_{hour}.parquet

Every write is temp file plus `os.replace`, the same pattern as control/state.py, so a
reader (duckdb, the panel, an rsync from the laptop) either sees the whole file or no
file, never a half written one. Compression is zstd: the lake is mostly repeated coin
strings and near-identical prices, and the recorder has idle CPU while it has finite
disk.

A buffer that fills before the hour is out writes a second file for the same hour,
`{venue}_{hour}_1.parquet`. Nothing downstream cares (readers glob the partition and
compaction folds the day into one file), and the alternative, rewriting the hour file,
would break the atomicity guarantee above.
"""

from __future__ import annotations

import heapq
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from itertools import batched
from pathlib import Path
from typing import Any

import orjson
import pyarrow as pa
import pyarrow.parquet as pq

from liqfade.data.schemas import (
    MULTI_VENUE_TABLES,
    SCHEMAS,
    SOURCE_RECORDER,
    rows_to_table,
    schema_for,
    validate_table,
)
from liqfade.log import get_logger
from liqfade.recorder.buffer import PartitionKey

log = get_logger(__name__)

COMPRESSION = "zstd"
DAY_SUFFIX = "_day.parquet"
QUARANTINE_DIR = "quarantine"

# Rows held at once while merging: one batch per input file being read, plus one chunk
# being written. A day is at most a few dozen files, so the ceiling is a few hundred
# thousand rows however long the day is or how far behind the feed fell.
MERGE_READ_ROWS = 8_192
MERGE_WRITE_ROWS = 65_536


def partition_dir(root: Path, table: str, date: str) -> Path:
    return Path(root) / table / f"date={date}"


def _free_path(directory: Path, venue: str, hour: int) -> Path:
    base = directory / f"{venue}_{hour:02d}.parquet"
    if not base.exists():
        return base
    for seq in range(1, 10_000):
        candidate = directory / f"{venue}_{hour:02d}_{seq}.parquet"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"more than 10000 files for {base}")  # pragma: no cover


def write_atomic(table: pa.Table, path: Path) -> None:
    """Write one Parquet file so a reader sees all of it or none of it.

    The temp file's footer is read back and its row count checked before the `os.replace`,
    so a short write never lands. `compact_day` and `hl_s3._assemble` have carried that
    check since they were written, for a reason that applies to every lake write: the
    owner's drive is an NTFS mount that has timed out mid-write and left a Parquet with no
    footer (docs/DECISIONS.md, 2026-09-04). Without it a truncated day file replaces a
    good one and the job that wrote it records a full row count in its manifest, so the
    loss is silent and the resume rule never revisits the day.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        pq.write_table(table, tmp, compression=COMPRESSION)
        written = int(pq.read_metadata(tmp).num_rows)
        if written != table.num_rows:
            raise RuntimeError(
                f"{path}: wrote {written} rows, expected {table.num_rows}; leaving the "
                "previous file in place"
            )
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def write_rows(
    root: Path,
    key: PartitionKey,
    rows: list[dict[str, Any]],
    *,
    source: str = SOURCE_RECORDER,
) -> Path | None:
    """Write one buffer to its Parquet file. Returns the path, or None for no rows.

    `source` is stamped on every row of the file. It defaults to the recorder because
    the recorder is the only caller that writes live feeds; the ingestion jobs in
    liqfade/data/ingest/ reuse this function and pass their own.
    """
    if not rows:
        return None
    table_name, venue, date, hour = key
    arrow = rows_to_table(table_name, rows, source)
    validate_table(table_name, arrow)
    directory = partition_dir(root, table_name, date)
    directory.mkdir(parents=True, exist_ok=True)
    path = _free_path(directory, venue, hour)
    write_atomic(arrow, path)
    log.info(
        "parquet_written",
        table=table_name,
        venue=venue,
        date=date,
        hour=hour,
        rows=len(rows),
        path=str(path),
    )
    return path


def quarantine_rows(
    data_dir: Path, reason: str, table: str, venue: str, rows: list[dict[str, Any]]
) -> Path | None:
    """Append rows the lake cannot take to `data/quarantine/date=.../{table}_{venue}.jsonl`.

    One place to look, whatever refused them: a row with no usable timestamp (which has no
    partition to go in) and the rows a shutdown flush could not write (which have nowhere
    left to be retried). JSON lines rather than Parquet on purpose, because the reason a
    row is here may be that it does not fit the canonical schema at all, and a quarantine
    file that itself fails to write would be the last thing anyone needs.

    Appended, not replaced, and never deleted by anything in this repo. Returns the path,
    or None for no rows.
    """
    if not rows:
        return None
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    directory = Path(data_dir) / QUARANTINE_DIR / f"date={day}"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{table}_{venue}.jsonl"
    payload = b"".join(
        orjson.dumps({"reason": reason, "at_ms": int(datetime.now(UTC).timestamp() * 1000), **row})
        + b"\n"
        for row in rows
    )
    with open(path, "ab") as handle:
        handle.write(payload)
    log.error(
        "rows_quarantined", reason=reason, table=table, venue=venue, rows=len(rows), path=str(path)
    )
    return path


def lake_files(directory: Path) -> list[Path]:
    """The Parquet files of a partition, ignoring anything hidden.

    Hidden covers our own `.name.pid.tmp` and, on the owner's external drive, macOS's
    AppleDouble sidecars: copying onto a non-native filesystem leaves a 4 KiB `._x.parquet`
    next to every `x.parquet`, which is not Parquet at all. Readers that glob `*.parquet`
    have to skip them too.
    """
    return [p for p in directory.glob("*.parquet") if not p.name.startswith(".")]


def _sorted_copy(table_name: str, path: Path, tmp_dir: Path) -> Path:
    """A `ts_ms`-ordered copy of one input file, so the merge below can stream it.

    One input file at a time is held in memory, which is what the previous compaction
    already did per hour. Refusing a null `ts_ms` is the only refusal compaction has
    left: such a row has no place in a file that claims to be sorted by that column and
    no honest place to put it, and it means the writer that produced the file was wrong.
    """
    # partitioning=None: the directory is named date=..., and hive discovery would
    # otherwise append a `date` column that is not in the canonical schema.
    arrow = pq.read_table(path, partitioning=None)
    validate_table(table_name, arrow)
    if arrow.column("ts_ms").null_count:
        raise RuntimeError(f"{path}: ts_ms is null on some rows, so the file cannot be ordered")
    out = tmp_dir / f".{path.stem}.sorted.{os.getpid()}.tmp"
    pq.write_table(arrow.sort_by([("ts_ms", "ascending")]), out, compression=COMPRESSION)
    return out


def _merged_rows(paths: list[Path]) -> Iterator[dict[str, Any]]:
    """Every row of already-sorted files, in one global `ts_ms` order.

    `heapq.merge` is the k-way merge, and each file is pulled one row group at a time, so
    a day that arrived hours late costs no more memory than a day that arrived on time.

    ponytail: rows go through Python dicts, which is a few seconds a day for a job that
    runs once a night. Merge arrow slices instead if compaction ever shows up in a
    profile.
    """

    def rows_of(path: Path) -> Iterator[dict[str, Any]]:
        for batch in pq.ParquetFile(path).iter_batches(batch_size=MERGE_READ_ROWS):
            yield from batch.to_pylist()

    return heapq.merge(*(rows_of(path) for path in paths), key=lambda row: row["ts_ms"])


def _compact_partition(table_name: str, directory: Path, inputs: list[Path]) -> int:
    """Fold one partition into one file ordered by `ts_ms`, streaming. Returns the rows.

    A k-way merge rather than hour after hour, because the recorder partitions by `rx_ms`
    and an event delivered late is filed in the hour it *arrived*: Hyperliquid replayed
    DOGE trades 356s old after a reconnect on 2026-09-05, so hour 23's file began before
    hour 22's ended and the day would not compact at all. There is no bound on how late a
    venue can be, so there is no tolerance to tune; the merge is right at any lateness.
    """
    # `stem`, not `name`: a file with no hour suffix at all is now compactable, and
    # `"hyperliquid.parquet".split("_")[0]` kept the extension and read as a second venue.
    venues = {path.stem.split("_")[0] for path in inputs}
    name = "all" if (len(venues) > 1 or table_name in MULTI_VENUE_TABLES) else venues.pop()
    output = directory / f"{name}{DAY_SUFFIX}"
    expected = sum(int(pq.read_metadata(path).num_rows) for path in inputs)
    schema = schema_for(table_name)

    tmp = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    writer = pq.ParquetWriter(tmp, schema, compression=COMPRESSION)
    sorted_inputs: list[Path] = []
    try:
        sorted_inputs = [_sorted_copy(table_name, path, directory) for path in inputs]
        for chunk in batched(_merged_rows(sorted_inputs), MERGE_WRITE_ROWS):
            writer.write_table(pa.Table.from_pylist(list(chunk), schema=schema))
        writer.close()
        # The row count is checked on the temp file first: a short output must never
        # replace the inputs, not even for the moment before the check fails.
        rows = pq.read_metadata(tmp).num_rows
        if rows != expected:
            raise RuntimeError(f"compacted {output} has {rows} rows, expected {expected}")
        os.replace(tmp, output)
    finally:
        writer.close()  # idempotent; the failure path needs it before the unlink
        for path in [tmp, *sorted_inputs]:
            if path.exists():
                path.unlink()

    after = pq.read_metadata(output).num_rows
    if after != expected:
        raise RuntimeError(f"compacted {output} has {after} rows, expected {expected}")
    for path in inputs:
        if path != output:
            path.unlink()
    return expected


def compact_day(root: Path, date: str, *, tables: list[str] | None = None) -> dict[str, int]:
    """Fold every hourly file of `date` into one file per table. Returns table -> rows.

    The hourly files are deleted only after the compacted file exists and its row count
    matches the sum of the inputs. A table whose partition holds rows from several venues
    compacts to `all_day.parquet`; a single venue table keeps its venue in the name.
    `tables` restricts the work to those table names; None means every canonical table in
    the lake.

    A directory with no canonical schema is skipped, never attempted. That is the two
    Tardis side tables, which keep Tardis's own columns by design and so have no schema
    to write and no `ts_ms` to sort on, and which the job already writes one file per
    venue per day. The rule lives here rather than in the CLI because
    `liqfade record --compact-only --date ...`, the command docs/DATA_SOURCES.md tells the
    owner to run, calls this with `tables=None` and used to raise on any of the twelve
    Tardis days. `liqfade compact --tables` still refuses a side table by name, so asking
    for one explicitly says so rather than quietly doing nothing.
    """
    root = Path(root)
    written: dict[str, int] = {}
    if not root.exists():
        return written
    wanted = set(tables) if tables is not None else None

    for table_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        table_name = table_dir.name
        if table_name not in SCHEMAS:
            continue
        if wanted is not None and table_name not in wanted:
            continue
        directory = table_dir / f"date={date}"
        inputs = sorted(lake_files(directory)) if directory.is_dir() else []
        if not inputs:
            continue
        if len(inputs) == 1 and inputs[0].name.endswith(DAY_SUFFIX):
            continue  # already compacted, and re-running must be a no-op
        if any(path.name.endswith(DAY_SUFFIX) for path in inputs):
            # A compacted file beside loose ones. Compaction does not dedupe, and the day
            # file is already the fold of a day, so folding it again with files that may
            # hold the same rows writes them twice. `liqfade derive candles` leaves
            # exactly this: its `hyperliquid_day.parquet` next to the `hl_api` bars of the
            # same minutes. This is what `_hour_of` used to refuse by accident, said out
            # loud. Move the day file aside to fold genuinely new rows into it.
            raise RuntimeError(
                f"{directory}: a compacted day file beside "
                f"{sorted(p.name for p in inputs if not p.name.endswith(DAY_SUFFIX))}; "
                f"compaction does not dedupe, so move it aside before folding these in"
            )

        rows = _compact_partition(table_name, directory, inputs)
        written[table_name] = rows
        log.info("compacted", table=table_name, date=date, rows=rows, files=len(inputs))
    return written
