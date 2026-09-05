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

import os
from pathlib import Path
from typing import Any

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

# How far an hour's first `ts_ms` may fall behind the previous hour's last before
# compaction refuses to write the file. The recorder partitions by `rx_ms` and sorts by
# `ts_ms`, so an event that happened at 10:59:59.9 and arrived at 11:00:00.1 is in hour
# 11's file and legitimately predates the end of hour 10 by the feed's latency. A minute
# is far more than any feed's latency and far less than a mis-parsed hour, which is the
# mistake worth catching. Backfilled rows all carry `rx_ms = 0` and partition on `ts_ms`,
# so for them the overlap is always zero.
ORDER_TOLERANCE_MS = 60_000


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


def lake_files(directory: Path) -> list[Path]:
    """The Parquet files of a partition, ignoring anything hidden.

    Hidden covers our own `.name.pid.tmp` and, on the owner's external drive, macOS's
    AppleDouble sidecars: copying onto a non-native filesystem leaves a 4 KiB `._x.parquet`
    next to every `x.parquet`, which is not Parquet at all. Readers that glob `*.parquet`
    have to skip them too.
    """
    return [p for p in directory.glob("*.parquet") if not p.name.startswith(".")]


def _hour_of(path: Path) -> int:
    """UTC hour from a partition file name, which is always `{venue}_{HH}[_...].parquet`.

    The suffixes come from the second flush of an hour (`_{n}`) and from the backfill's
    staging mover (`_{key stem}_{batch}`), so the hour is always the second field. An
    unrecognisable name raises: guessing hour 0 would put its rows at the front of a file
    that claims to be sorted by time, which is worse than refusing to compact the day.
    """
    fields = path.stem.split("_")
    if len(fields) >= 2 and len(fields[1]) == 2 and fields[1].isdigit() and int(fields[1]) < 24:
        return int(fields[1])
    raise RuntimeError(f"cannot read the hour from {path}; move it aside to compact this day")


def _compact_partition(table_name: str, directory: Path, inputs: list[Path]) -> int:
    """Fold one partition into one file, holding at most one hour in memory.

    Returns the row count. Hours are written in ascending order into a single
    `ParquetWriter`, one row group each. The recorder and the backfill both partition by
    the UTC hour of the row (liqfade/recorder/buffer.py), so an hour's rows all fall
    inside that hour and hour-ordered output is globally ordered by `ts_ms`. That is an
    assumption about the writers, so it is asserted rather than trusted.
    """
    venues = {path.name.split("_")[0] for path in inputs}
    name = "all" if (len(venues) > 1 or table_name in MULTI_VENUE_TABLES) else venues.pop()
    output = directory / f"{name}{DAY_SUFFIX}"

    by_hour: dict[int, list[Path]] = {}
    for path in inputs:
        by_hour.setdefault(_hour_of(path), []).append(path)
    expected = sum(int(pq.read_metadata(path).num_rows) for path in inputs)

    tmp = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    writer = pq.ParquetWriter(tmp, schema_for(table_name), compression=COMPRESSION)
    try:
        last_ts: int | None = None
        for hour in sorted(by_hour):
            # partitioning=None: the directory is named date=..., and hive discovery would
            # otherwise append a `date` column that is not in the canonical schema.
            arrow = pa.concat_tables(
                [pq.read_table(path, partitioning=None) for path in by_hour[hour]]
            )
            validate_table(table_name, arrow)
            if arrow.num_rows == 0:
                continue
            arrow = arrow.sort_by([("ts_ms", "ascending")])
            stamps = arrow.column("ts_ms")
            first = stamps[0].as_py()
            if last_ts is not None and first < last_ts:
                overlap = last_ts - first
                if overlap > ORDER_TOLERANCE_MS:
                    raise RuntimeError(
                        f"{directory}: hour {hour:02d} starts at ts_ms {first}, {overlap} ms "
                        f"before the end of the previous hour; refusing to write a "
                        f"misordered file"
                    )
                log.warning(
                    "compaction_hour_overlap", path=str(directory), hour=hour, overlap_ms=overlap
                )
            last_ts = stamps[-1].as_py()
            writer.write_table(arrow, row_group_size=arrow.num_rows)
        writer.close()
        # The row count is checked on the temp file first: a short output must never
        # replace the inputs, not even for the moment before the check fails.
        rows = pq.read_metadata(tmp).num_rows
        if rows != expected:
            raise RuntimeError(f"compacted {output} has {rows} rows, expected {expected}")
        os.replace(tmp, output)
    finally:
        writer.close()  # idempotent; the failure path needs it before the unlink
        if tmp.exists():
            tmp.unlink()

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

        rows = _compact_partition(table_name, directory, inputs)
        written[table_name] = rows
        log.info("compacted", table=table_name, date=date, rows=rows, files=len(inputs))
    return written
