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

import concurrent.futures
import multiprocessing
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Iterator
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


def log_egress(job: str, manifest: Manifest) -> str:
    """Log what this job has spent so far and return the line for its run summary.

    Both S3 jobs printed GiB and nothing else, and the "$25 for the year" figure lived
    only in docs/DECISIONS.md. The Phase 2 definition of done says egress spend is
    logged, so it goes to the journal as a structured `event=egress` line carrying the
    byte count as well as to the console.
    """
    total = manifest.total_bytes()
    usd = total / GIB * EGRESS_USD_PER_GB
    log.info("egress", job=job, bytes=total, gib=round(total / GIB, 3), usd=round(usd, 2))
    return f"  egress so far: {total / GIB:.2f} GiB, ${usd:.2f} at ${EGRESS_USD_PER_GB:.2f}/GB"


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


# --- parallel pull ------------------------------------------------------------
#
# Measured on the owner's machine, twice. Sequential: Python at 4% CPU, the fuse-t NTFS
# layer for the lake drive choking on small writes, ~3 GiB/hour. Threaded workers with
# local staging: the drive problem gone, Python pegged at 100% of ONE core, still
# ~3 GiB/hour, because orjson parsing holds the GIL and threads cannot spread it over
# cores. So the workers are processes. Each worker process downloads and parses on the
# local SSD; the parent alone moves finished files onto the lake disk one at a time
# (a USB drive is good at sequential copies and bad at concurrent small writes) and is
# the only writer of the manifest.

STAGE_MIN_FREE_GIB = 5.0  # floor for the local staging disk, distinct from the lake floor

# Every worker pool in the ingest package uses this, not the platform default, which is
# fork on Linux. fork copies the parent's locks without the threads holding them, and the
# workers all read or write Parquet through pyarrow and polars, whose thread pools are
# exactly that kind of lock: a child can block on one nothing will ever release. It cost
# two CI runs that hung for 20 minutes where macOS, which already defaults to spawn, was
# green. Pickling by reference is the contract these workers were written to anyway.
MP_CONTEXT = multiprocessing.get_context("spawn")

# (local raw file, key, staging parquet root) -> rows per table. Runs inside a worker
# process, so it must be a module level function (pickled by reference) and must write
# only under the staging root it was given.
ObjectHandler = Callable[[Path, str, Path], dict[str, int]]
# Runs in the PARENT on the staged parquet root before it is merged, for jobs that need
# to look at what a worker produced (the fills side rule check). May be a closure.
StagedInspector = Callable[[Path, str], None]

_WORKER_CLIENT: Any = None


def _worker_client(factory: Callable[[], Any]) -> Any:
    """One S3 client per worker process, created on first use."""
    global _WORKER_CLIENT
    if _WORKER_CLIENT is None:
        _WORKER_CLIENT = factory()
    return _WORKER_CLIENT


def _stage_object(
    client: Any,
    bucket: str,
    key: str,
    size: int,
    stage_base: str,
    handler: ObjectHandler,
) -> tuple[str, int, str, dict[str, int]]:
    """Download one object and parse it into staged Parquet.

    Returns (key, size, scratch dir, rows per table). The caller owns everything after
    that: inspection, the move onto the lake, the manifest, and deleting the scratch.
    """
    scratch = Path(tempfile.mkdtemp(dir=stage_base))
    local = download_object(client, bucket, key, scratch / "raw" / Path(key).name)
    rows = handler(local, key, scratch / "parquet")
    return key, size, str(scratch), rows


def _pull_one(
    factory: Callable[[], Any],
    bucket: str,
    key: str,
    size: int,
    stage_base: str,
    handler: ObjectHandler,
) -> tuple[str, int, str, dict[str, int]]:
    """Worker process body: _stage_object with the per-process cached client."""
    return _stage_object(_worker_client(factory), bucket, key, size, stage_base, handler)


def staged_name(key: str, staged: Path) -> str:
    """The lake name for one staged file: `{venue}_{HH}_{token}_{batch}.parquet`.

    The staged name is already `{venue}_{HH}` plus `_{n}` for the second and later
    batches of an hour, which is the order the parser wrote them in. Two things are added
    here, both of which the old `uuid4().hex[:6]` suffix broke:

    - **It sorts in write order.** `sorted(lake_files(...))` has to return an hour's
      batches in the order the parser emitted them, because the whole 02d repair rests on
      a partition's file order being the exchange's event order, and `..._07_1_9f0e`
      sorts before `..._07_ab12`. The batch index is zero padded and always present.
    - **It is stable.** A crash between `merge_staged` and `manifest.record` used to make
      a re-run of the same object append a second copy of its rows under different names,
      undetectably. The same object now lands on the same file names and overwrites.

    `token` is the S3 key's own stem, which is what keeps two objects apart when both
    write the same partition and hour: the coin for the L2 archive, and the neighbouring
    hour's object for a fill whose `time` falls just outside its own.
    """
    fields = staged.stem.split("_")  # {venue}_{HH}[_{batch}]
    batch = int(fields[2]) if len(fields) > 2 and fields[2].isdigit() else 0
    token = re.sub(r"[^0-9A-Za-z-]", "-", Path(key).stem) or "x"
    return f"{fields[0]}_{fields[1]}_{token}_{batch:04d}.parquet"


def merge_staged(stage_root: Path, lake_root: Path, key: str) -> None:
    """Move every staged Parquet file into the lake, keeping the partition layout.

    `key` is the S3 key the staged files were parsed from; see `staged_name` for what
    the name has to guarantee. The copy goes to a temp name on the destination
    filesystem then `os.replace`, the atomicity rule every lake write follows.
    """
    if not stage_root.exists():
        return
    for staged in sorted(stage_root.rglob("*.parquet")):
        relative = staged.relative_to(stage_root)
        final = lake_root / relative.parent / staged_name(key, staged)
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.with_name(f".{final.name}.tmp")
        try:
            shutil.copyfile(staged, tmp)
            os.replace(tmp, final)
        finally:
            if tmp.exists():
                tmp.unlink()
        staged.unlink()


def parallel_pull(
    client: Any,
    bucket: str,
    todo: list[dict[str, Any]],
    data_dir: Path,
    job: str,
    manifest: Manifest,
    handler: ObjectHandler,
    *,
    workers: int = 1,
    keep_raw: bool = False,
    inspect: StagedInspector | None = None,
    client_factory: Callable[[], Any] | None = None,
) -> int:
    """Fetch and process `todo`; returns objects completed this run.

    workers == 1 runs everything in this process with `client`, which is also what the
    offline tests inject. workers > 1 spawns worker processes that each build their own
    client from `client_factory` (default: real boto3), because a client cannot cross a
    process boundary. Either way the parent alone merges staged files onto the lake and
    records the manifest, so a crash or a failed object leaves the manifest holding
    exactly the completed work and a re-run resumes. Low disk on the lake or the staging
    SSD stops the pull with a warning and exit-as-success, the documented behavior.
    """
    lake_root = Path(data_dir) / "parquet"
    stage_base = Path(tempfile.mkdtemp(prefix=f"liqfade-{job}-"))
    fetched = 0
    low_disk = False

    def disk_ok() -> bool:
        return enough_disk(data_dir) and enough_disk(stage_base, min_free_gib=STAGE_MIN_FREE_GIB)

    def finish(key: str, size: int, scratch: str, rows: dict[str, int]) -> None:
        nonlocal fetched
        scratch_path = Path(scratch)
        staged_parquet = scratch_path / "parquet"
        if inspect is not None:
            inspect(staged_parquet, key)
        merge_staged(staged_parquet, lake_root, key)
        if keep_raw:
            raws = list((scratch_path / "raw").glob("*"))
            if raws:
                kept = raw_path(data_dir, job, key)
                kept.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(raws[0]), kept)
        shutil.rmtree(scratch_path, ignore_errors=True)
        manifest.record(key, size, rows)
        fetched += 1
        log.info("backfill_object", job=job, key=key, **rows)

    try:
        if workers <= 1:
            for obj in todo:
                if not disk_ok():
                    low_disk = True
                    break
                key, size = str(obj["Key"]), int(obj.get("Size", 0))
                finish(*_stage_object(client, bucket, key, size, str(stage_base), handler))
        else:
            factory = client_factory or s3_client
            with concurrent.futures.ProcessPoolExecutor(
                max_workers=workers, mp_context=MP_CONTEXT
            ) as pool:
                futures = [
                    pool.submit(
                        _pull_one,
                        factory,
                        bucket,
                        str(obj["Key"]),
                        int(obj.get("Size", 0)),
                        str(stage_base),
                        handler,
                    )
                    for obj in todo
                ]
                try:
                    for future in concurrent.futures.as_completed(futures):
                        finish(*future.result())
                        if not disk_ok():
                            low_disk = True
                            break
                finally:
                    pool.shutdown(wait=True, cancel_futures=True)
    finally:
        shutil.rmtree(stage_base, ignore_errors=True)

    if low_disk:
        # Logged as well as printed: a detached run's console scrollback is gone by the
        # time anyone asks why the pull is short, and the journal is not.
        log.warning("backfill_low_disk", job=job, fetched=fetched, todo=len(todo))
        print(
            f"WARNING: stopping, free disk is under the floor. Fetched {fetched} of "
            f"{len(todo)} objects. Re-run to continue where this left off."
        )
    return fetched
