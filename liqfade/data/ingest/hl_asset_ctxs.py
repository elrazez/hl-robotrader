"""A year of Hyperliquid asset contexts from the requester-pays archive. Cents, not dollars.

Layout, listed and verified on 2026-09-04:
`s3://hyperliquid-archive/asset_ctxs/{YYYYMMDD}.csv.lz4`, one file per UTC day from
2023-05-20 through two days ago, about 11 MB compressed a day in 2026. The year
2025-09-01 to 2026-08-30 is 4.05 GB, roughly $0.36 of egress.

Columns, in this order: `time` (ISO 8601 UTC), `coin`, `funding`, `open_interest`,
`prev_day_px`, `day_ntl_vlm`, `premium`, `oracle_px`, `mark_px`, `mid_px`,
`impact_bid_px`, `impact_ask_px`. One row per coin per minute, 1440 per coin per day; 229
perps on 2026-03-01.

Why this is worth pulling: Phase 3 calibrates the live cascade detector, whose gates read
open interest and mark-versus-mid from `activeAssetCtx`. The lake has no `asset_ctx`
history and the recorder has not started, so without this the detector would be
calibrated on trade prints alone.

`prev_day_px` is the one archive column the canonical schema has no home for. It is the
previous day's close, recoverable from `candles_1m`, so it is dropped rather than given a
column nothing else can fill.

One file per day, `asset_ctx/date=YYYY-MM-DD/hyperliquid_day.parquet`, sorted by `ts_ms`
and already named like a compacted partition, so `liqfade compact` leaves it alone. The
work is one 11 MB object per day parsed straight into Arrow, so this job runs serially
and needs none of the staging the fills and L2 pulls do.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa

from liqfade.data.ingest.common import (
    BackfillError,
    Manifest,
    check_cost_gate,
    day_range,
    enough_disk,
    estimate,
    list_objects,
    log_egress,
)
from liqfade.data.schemas import SOURCE_HL_ARCHIVE, schema_for, validate_table
from liqfade.log import get_logger
from liqfade.recorder.writer import partition_dir, write_atomic

log = get_logger(__name__)

JOB = "s3-asset-ctxs"
BUCKET = "hyperliquid-archive"
PREFIX = "asset_ctxs"
TABLE = "asset_ctx"
VENUE = "hyperliquid"
OUTPUT_NAME = f"{VENUE}_day.parquet"

# The archive's own column for each canonical one. Anything not named here is null in
# every row, and `prev_day_px` is the archive column deliberately without a home.
ARCHIVE_COLUMNS = {
    "coin": "coin",
    "mark_px": "mark_px",
    "oracle_px": "oracle_px",
    "mid_px": "mid_px",
    "funding": "funding",
    "open_interest": "open_interest",
    "day_ntl_vlm": "day_ntl_vlm",
    "premium": "premium",
    "impact_bid_px": "impact_bid_px",
    "impact_ask_px": "impact_ask_px",
}
DROPPED_COLUMNS = ("prev_day_px",)
TIME_COLUMN = "time"


def object_key(day: date) -> str:
    return f"{PREFIX}/{day:%Y%m%d}.csv.lz4"


def day_of_key(key: str) -> str:
    """`asset_ctxs/20260301.csv.lz4` -> `2026-03-01`, the partition it belongs in."""
    stem = key.rsplit("/", 1)[-1].removesuffix(".csv.lz4")
    return f"{stem[0:4]}-{stem[4:6]}-{stem[6:8]}"


def _ts_ms(column: pa.ChunkedArray | pa.Array) -> pa.Array:
    """The `time` column as integer milliseconds UTC, whatever pyarrow inferred it as.

    The CSV reader usually infers ISO 8601 into a timestamp; a trailing offset or an
    unexpected precision leaves it a string, and the cast handles that too.
    """
    if pa.types.is_string(column.type) or pa.types.is_large_string(column.type):
        column = column.cast(pa.timestamp("ms"))
    return column.cast(pa.timestamp("ms")).cast(pa.int64())


def parse_day(payload: Path | Any) -> pa.Table:
    """One archive day as canonical `asset_ctx` rows, sorted by `ts_ms`.

    `payload` is the local `.csv.lz4`. It is decompressed as the CSV reader pulls, and
    everything stays in Arrow: a day is 330,000 rows over twelve columns and never
    becomes a Python object.
    """
    import lz4.frame
    import pyarrow.csv as pa_csv

    with lz4.frame.open(payload, "rb") as handle:
        raw = pa_csv.read_csv(handle)

    missing = [name for name in (TIME_COLUMN, "coin") if name not in raw.schema.names]
    if missing:
        raise BackfillError(
            f"{payload}: the archive file has no {', '.join(missing)} column; its columns "
            f"are {raw.schema.names}. The layout changed; see docs/DATA_SOURCES.md."
        )

    schema = schema_for(TABLE)
    nulls = pa.nulls(raw.num_rows, pa.float64())
    columns = []
    for field in schema:
        if field.name == "ts_ms":
            columns.append(_ts_ms(raw.column(TIME_COLUMN)))
        elif field.name == "rx_ms":
            columns.append(pa.array([0] * raw.num_rows, pa.int64()))
        elif field.name == "source":
            columns.append(pa.array([SOURCE_HL_ARCHIVE] * raw.num_rows, pa.string()))
        else:
            source = ARCHIVE_COLUMNS.get(field.name)
            found = raw.column(source) if source in raw.schema.names else nulls
            columns.append(found.cast(field.type))

    table = pa.Table.from_arrays(columns, schema=schema)
    validate_table(TABLE, table)
    return table.sort_by([("ts_ms", "ascending")])


def write_day(root: Path, day: str, table: pa.Table) -> int:
    """One file per day, named like a compacted partition so `compact` leaves it alone."""
    directory = partition_dir(root, TABLE, day)
    directory.mkdir(parents=True, exist_ok=True)
    write_atomic(table, directory / OUTPUT_NAME)
    return int(table.num_rows)


def run(
    client: Any,
    data_dir: Path,
    start: date,
    end: date,
    *,
    estimate_only: bool = False,
    yes: bool = False,
    keep_raw: bool = False,
) -> int:
    """Pull the daily asset-context files for [start, end], newest day first."""
    import shutil
    import tempfile

    from liqfade.data.ingest.common import download_object, raw_path

    manifest = Manifest.load(data_dir, JOB)
    wanted = {object_key(day) for day in day_range(start, end)}
    listed = list_objects(client, BUCKET, f"{PREFIX}/")
    objects = [obj for obj in listed if str(obj["Key"]) in wanted]

    absent = sorted(wanted - {str(obj["Key"]) for obj in objects})
    if absent:
        log.warning("asset_ctxs_days_absent", days=len(absent), first=absent[0], last=absent[-1])
        print(
            f"WARNING: {len(absent)} of {len(wanted)} days are not in the archive "
            f"({absent[0]} to {absent[-1]}). It starts 2023-05-20 and lags two days."
        )

    todo = [obj for obj in objects if not manifest.done(str(obj["Key"]))]
    est = estimate(todo)
    skipped = len(objects) - len(todo)
    print(f"{est.line()}" + (f" ({skipped} already done, skipped)" if skipped else ""))
    if estimate_only:
        return 0
    check_cost_gate(est, yes=yes)

    root = Path(data_dir) / "parquet"
    scratch = Path(tempfile.mkdtemp(prefix=f"liqfade-{JOB}-"))
    fetched = rows = 0
    try:
        for obj in reversed(todo):  # newest first, as every S3 job pulls
            if not enough_disk(data_dir):
                print(
                    f"WARNING: stopping, free disk is under the floor. Fetched {fetched} "
                    f"of {len(todo)} days. Re-run to continue where this left off."
                )
                break
            key = str(obj["Key"])
            local = download_object(client, BUCKET, key, scratch / Path(key).name)
            day = day_of_key(key)
            written = write_day(root, day, parse_day(local))
            if keep_raw:
                kept = raw_path(data_dir, JOB, key)
                kept.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(local), kept)
            else:
                local.unlink(missing_ok=True)
            manifest.record(key, int(obj.get("Size", 0)), {TABLE: written})
            fetched += 1
            rows += written
            log.info("backfill_object", job=JOB, key=key, asset_ctx=written)
            print(f"{day}  {written:>9,} rows", flush=True)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    print(f"\nfetched {fetched} days this run, {len(manifest.objects)} in the manifest")
    print(f"  {TABLE}: {rows:,} rows this run, {manifest.total_rows().get(TABLE, 0):,} in total")
    print(f"  dropped, no canonical column: {', '.join(DROPPED_COLUMNS)}")
    print(log_egress(JOB, manifest))
    return 0
