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
    SOURCE_RECORDER,
    rows_to_table,
    validate_table,
)
from liqfade.log import get_logger
from liqfade.recorder.buffer import PartitionKey

log = get_logger(__name__)

COMPRESSION = "zstd"
DAY_SUFFIX = "_day.parquet"


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


def _write_atomic(table: pa.Table, path: Path) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        pq.write_table(table, tmp, compression=COMPRESSION)
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
    _write_atomic(arrow, path)
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


def compact_day(root: Path, date: str) -> dict[str, int]:
    """Fold every hourly file of `date` into one file per table. Returns table -> rows.

    The hourly files are deleted only after the compacted file exists and its row count
    matches the sum of the inputs. A table whose partition holds rows from several venues
    compacts to `all_day.parquet`; a single venue table keeps its venue in the name.
    """
    root = Path(root)
    written: dict[str, int] = {}
    if not root.exists():
        return written

    for table_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        table_name = table_dir.name
        directory = table_dir / f"date={date}"
        inputs = sorted(directory.glob("*.parquet")) if directory.is_dir() else []
        if not inputs:
            continue
        if len(inputs) == 1 and inputs[0].name.endswith(DAY_SUFFIX):
            continue  # already compacted, and re-running must be a no-op

        venues = {path.name.split("_")[0] for path in inputs}
        name = "all" if (len(venues) > 1 or table_name in MULTI_VENUE_TABLES) else venues.pop()
        output = directory / f"{name}{DAY_SUFFIX}"

        expected = sum(pq.read_metadata(path).num_rows for path in inputs)
        # partitioning=None: the directory is named date=..., and hive discovery would
        # otherwise append a `date` column that is not in the canonical schema.
        arrow = pa.concat_tables([pq.read_table(path, partitioning=None) for path in inputs])
        validate_table(table_name, arrow)
        arrow = arrow.sort_by([("ts_ms", "ascending")])
        if arrow.num_rows != expected:  # pragma: no cover - concat cannot lose rows
            raise RuntimeError(
                f"compaction row mismatch for {directory}: {arrow.num_rows} != {expected}"
            )

        _write_atomic(arrow, output)
        after = pq.read_metadata(output).num_rows
        if after != expected:  # pragma: no cover - a torn write would have raised first
            raise RuntimeError(f"compacted {output} has {after} rows, expected {expected}")
        for path in inputs:
            if path != output:
                path.unlink()
        written[table_name] = expected
        log.info("compacted", table=table_name, date=date, rows=expected, path=str(output))
    return written
