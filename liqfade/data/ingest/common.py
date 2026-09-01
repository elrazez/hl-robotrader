"""Shared machinery for the backfill jobs: money guards, manifests, S3 and disk.

The rules this module enforces are the ones that cost real money if they are wrong, so
they live in one place and every job goes through them:

- **Estimate first.** `estimate()` sums the listed object sizes and prices them at
  `EGRESS_USD_PER_GB`. `check_cost_gate()` refuses anything over $5 unless the caller
  passed `--yes`. There is no way to download without listing first, because listing is
  what produces the byte count.
- **Requester pays.** Every S3 call goes through `list_objects` or `download_object`
  here, and both send `RequestPayer="requester"`. A requester-pays bucket answers a
  request without that header with 403, so a missing header is loud rather than silent.
- **Resume.** `Manifest` records every completed key with its size and row counts and is
  written atomically after each object, so a crash mid-pull loses at most one object and
  a re-run skips what is already done.
- **Disk.** `enough_disk()` stops a pull before the box fills up. Callers exit 0 with a
  warning rather than failing: a partial pull, newest first, is a useful pull.

S3 access is always through an injected client, never a module level one, so the tests
run offline against a fake and no test can accidentally spend money.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import orjson

from liqfade.data.schemas import validate_table  # noqa: F401 - re-exported for the jobs
from liqfade.log import get_logger
from liqfade.recorder.buffer import Buffers
from liqfade.recorder.writer import write_rows

log = get_logger(__name__)

# S3 data transfer out to the internet. AWS bills this per GiB despite writing "GB".
EGRESS_USD_PER_GB = 0.09
GIB = 1024**3
# Above this, a job refuses to run without --yes. From the hard money rules.
COST_GATE_USD = 5.0
# Below this much free space the pull stops and reports what it fetched.
MIN_FREE_GIB = 30.0
REQUESTER_PAYS = {"RequestPayer": "requester"}


class BackfillError(RuntimeError):
    """A backfill job cannot proceed. The CLI turns this into a message and exit 1."""


# --- dates --------------------------------------------------------------------


def parse_day(text: str) -> date:
    """YYYY-MM-DD to a date. Raises BackfillError with the offending string."""
    try:
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC).date()
    except ValueError:
        raise BackfillError(f"bad date {text!r}, expected YYYY-MM-DD") from None


def day_range(start: date, end: date, *, newest_first: bool = True) -> list[date]:
    """Every day from start to end inclusive.

    Newest first by default, which is the documented pull order: if the money or the
    disk runs out, the months you keep are the ones closest to today and therefore the
    ones the backtest cares most about.
    """
    if end < start:
        raise BackfillError(f"end {end} is before start {start}")
    days = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
    return list(reversed(days)) if newest_first else days


# --- cost estimate ------------------------------------------------------------


@dataclass(frozen=True)
class Estimate:
    """What a pull would cost, from the listed object sizes."""

    objects: int
    bytes: int

    @property
    def gib(self) -> float:
        return self.bytes / GIB

    @property
    def usd(self) -> float:
        return self.gib * EGRESS_USD_PER_GB

    def line(self) -> str:
        return (
            f"{self.objects} objects, {self.gib:.2f} GiB, "
            f"estimated ${self.usd:.2f} at ${EGRESS_USD_PER_GB:.2f}/GB"
        )


def estimate(objects: list[dict[str, Any]]) -> Estimate:
    """Cost of downloading every listed object. `objects` are S3 list entries."""
    return Estimate(objects=len(objects), bytes=sum(int(obj.get("Size", 0)) for obj in objects))


def check_cost_gate(est: Estimate, *, yes: bool) -> None:
    """Raise unless the pull is under the gate or the owner passed --yes."""
    if est.usd <= COST_GATE_USD or yes:
        return
    raise BackfillError(
        f"{est.line()}, which is over the ${COST_GATE_USD:.2f} gate. "
        "Re-run with --yes if that is the intended spend."
    )


# --- disk ---------------------------------------------------------------------


def free_gib(path: Path) -> float:
    """Free space on the filesystem holding `path`, walking up to an existing parent."""
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free / GIB


def enough_disk(path: Path, *, min_free_gib: float = MIN_FREE_GIB) -> bool:
    return free_gib(path) >= min_free_gib


# --- manifest -----------------------------------------------------------------


@dataclass
class Manifest:
    """Completed objects for one job, so a re-run resumes instead of re-buying bytes.

    Saved after every object. The write is temp file plus `os.replace`, the same pattern
    as the Parquet writer, so a crash leaves either the previous manifest or the new one
    and never a truncated file.
    """

    path: Path
    job: str
    objects: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, data_dir: Path, job: str) -> Manifest:
        path = Path(data_dir) / "ingest_state" / f"{job}.json"
        if not path.exists():
            return cls(path=path, job=job)
        payload = orjson.loads(path.read_bytes())
        return cls(path=path, job=job, objects=dict(payload.get("objects", {})))

    def done(self, key: str) -> bool:
        return key in self.objects

    def pending(self, keys: list[str]) -> list[str]:
        return [key for key in keys if not self.done(key)]

    def record(self, key: str, size: int, rows: dict[str, int]) -> None:
        self.objects[key] = {
            "size": int(size),
            "rows": {table: int(count) for table, count in rows.items()},
            "at_ms": int(datetime.now(UTC).timestamp() * 1000),
        }
        self.save()

    def total_bytes(self) -> int:
        return sum(int(entry["size"]) for entry in self.objects.values())

    def total_rows(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for entry in self.objects.values():
            for table, count in entry.get("rows", {}).items():
                totals[table] = totals.get(table, 0) + int(count)
        return totals

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"job": self.job, "objects": self.objects}
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2))
            os.replace(tmp, self.path)
        finally:
            if tmp.exists():
                tmp.unlink()


# --- S3 -----------------------------------------------------------------------


def s3_client() -> Any:
    """A boto3 S3 client from the ambient AWS credentials.

    Imported lazily so `liqfade --help` and every offline test stay free of boto3, and
    so a machine with no credentials only fails when it actually tries to spend.
    """
    import boto3

    return boto3.client("s3")


def list_objects(client: Any, bucket: str, prefix: str) -> list[dict[str, Any]]:
    """Every object under `prefix`, paginated. Requester pays, sorted by key.

    Listing is also how a job discovers the layout: keys are never constructed from a
    template, so a bucket that reorganises shows up as an empty listing rather than as a
    stream of 404s.
    """
    found: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, **REQUESTER_PAYS}
        if token:
            kwargs["ContinuationToken"] = token
        page = client.list_objects_v2(**kwargs)
        found.extend(page.get("Contents", []))
        token = page.get("NextContinuationToken") if page.get("IsTruncated") else None
        if not token:
            return sorted(found, key=lambda obj: str(obj["Key"]))


def download_object(client: Any, bucket: str, key: str, dest: Path) -> Path:
    """Fetch one object to `dest`. Requester pays."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    client.download_file(Bucket=bucket, Key=key, Filename=str(dest), ExtraArgs=dict(REQUESTER_PAYS))
    return dest


def raw_path(data_dir: Path, job: str, key: str) -> Path:
    """Where one downloaded object lands under data/raw/{job}/.

    The S3 key becomes the relative path, so two objects from different days cannot
    collide and `--keep-raw` leaves something a human can navigate.
    """
    return Path(data_dir) / "raw" / job / key.lstrip("/")


def read_lz4_lines(path: Path) -> Iterator[bytes]:
    """Yield the lines of an LZ4 framed file, streaming rather than reading it whole.

    The Hyperliquid archives are newline delimited JSON inside one LZ4 frame, and a
    single hour of fills expands to several hundred MB, so nothing here holds the
    decompressed file in memory.
    """
    import lz4.frame

    with lz4.frame.open(path, "rb") as handle:
        for line in handle:
            if line.strip():
                yield line


# --- writing ------------------------------------------------------------------

# Buffers is reused only to group rows into (date, hour) partitions, so the limit is
# effectively off: a backfill writes whatever one source object produced, in one go.
_NO_ROW_LIMIT = 1_000_000_000


def write_partitioned(
    root: Path, table: str, venue: str, source: str, rows: list[dict[str, Any]]
) -> int:
    """Write rows into the canonical layout, split by the UTC day and hour of the event.

    Returns the number of rows written. The file name keeps the venue, as the recorder's
    files do; provenance is the `source` column, which survives compaction.
    """
    if not rows:
        return 0
    buffers = Buffers(max_rows=_NO_ROW_LIMIT)
    for row in rows:
        buffers.add(table, venue, row)
    written = 0
    for key, group in buffers.take_all():
        write_rows(root, key, group, source=source)
        written += len(group)
    return written


def replace_source_rows(
    root: Path, table: str, day: str, venue: str, source: str, rows: list[dict[str, Any]]
) -> int:
    """Rewrite one day's partition so `source`'s rows are exactly `rows`.

    This is what makes a rederive idempotent. Compaction folds a day into one file per
    table, so recorded rows and backfilled rows share a file and the old rows cannot be
    dropped by deleting a file: they are read back, filtered by `source`, and written
    alongside the new ones.
    """
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    from liqfade.recorder.writer import partition_dir, write_atomic

    directory = partition_dir(root, table, day)
    existing = sorted(directory.glob("*.parquet")) if directory.is_dir() else []

    kept = []
    for path in existing:
        arrow = pq.read_table(path, partitioning=None)
        others = arrow.filter(pc.not_equal(arrow.column("source"), source))
        if others.num_rows:
            kept.append(others)

    # Everything is read before anything is deleted, so a failure above leaves the
    # partition untouched rather than half rewritten.
    for path in existing:
        path.unlink()

    written = write_partitioned(root, table, venue, source, rows)
    if kept:
        merged = pa.concat_tables(kept).sort_by([("ts_ms", "ascending")])
        directory.mkdir(parents=True, exist_ok=True)
        write_atomic(merged, directory / "kept_00.parquet")
    return written
