"""The parallel pull: staging, the serialized mover, resume and failure behavior.

Handlers and client factories live at module level because the workers>1 path pickles
them into spawned processes; that constraint is part of the contract being tested.
"""

from __future__ import annotations

import time
from functools import partial
from pathlib import Path
from typing import Any

import lz4.frame
import pyarrow.parquet as pq
import pytest

from liqfade.data.ingest import common
from liqfade.data.ingest.common import Manifest, merge_staged, parallel_pull, write_partitioned
from tests.conftest import FakeS3

BUCKET = "fake-bucket"
# Timestamps on 2026-06-01, one per hour, so objects map to distinct partitions.
BASE = 1_780_300_800_000


def _row(ts_ms: int) -> dict[str, Any]:
    return {
        "venue": "hyperliquid",
        "coin": "BTC",
        "ts_ms": ts_ms,
        "rx_ms": 0,
        "px": 100.0,
        "sz": 1.0,
        "side": "buy",
        "tid": ts_ms,
    }


def _write(local: Path, stage_root: Path, n_rows: int) -> dict[str, int]:
    base_ts = int(lz4.frame.decompress(local.read_bytes()))
    rows = [_row(base_ts + i) for i in range(n_rows)]
    return {"trades": write_partitioned(stage_root, "trades", "hyperliquid", "hl_s3", rows)}


def handler_one(local: Path, key: str, stage_root: Path) -> dict[str, int]:
    return _write(local, stage_root, 1)


def handler_five(local: Path, key: str, stage_root: Path) -> dict[str, int]:
    return _write(local, stage_root, 5)


def handler_slow(local: Path, key: str, stage_root: Path) -> dict[str, int]:
    time.sleep(0.5)
    return _write(local, stage_root, 1)


def handler_failing_on_3(local: Path, key: str, stage_root: Path) -> dict[str, int]:
    if key.endswith("3.lz4"):
        raise RuntimeError("corrupt object")
    return _write(local, stage_root, 2)


def _payloads() -> dict[str, bytes]:
    return {
        f"job/2026-06-01/{i}.lz4": lz4.frame.compress(str(BASE + i * 3_600_000).encode())
        for i in range(8)
    }


def _objects(client: FakeS3) -> list[dict[str, Any]]:
    return [{"Key": key, "Size": len(data)} for key, data in sorted(client.objects.items())]


def _lake_rows(data_dir: Path) -> int:
    files = list((data_dir / "parquet" / "trades").rglob("*.parquet"))
    return sum(pq.read_metadata(path).num_rows for path in files)


@pytest.fixture
def client() -> FakeS3:
    return FakeS3(_payloads())


# partial(FakeS3, payloads) pickles into a spawned worker; each worker builds its own
# fake, exactly as production workers each build their own boto3 client.
FACTORY = partial(FakeS3, _payloads())


def test_process_pool_completes_everything_and_the_manifest_agrees(
    client: FakeS3, tmp_path: Path
) -> None:
    manifest = Manifest.load(tmp_path, "t")
    fetched = parallel_pull(
        client,
        BUCKET,
        _objects(client),
        tmp_path,
        "t",
        manifest,
        handler_five,
        workers=3,
        client_factory=FACTORY,
    )
    assert fetched == 8
    assert len(manifest.objects) == 8
    assert manifest.total_rows() == {"trades": 40}
    assert _lake_rows(tmp_path) == 40
    leftovers = [p for p in tmp_path.rglob("*") if p.name.startswith(".") and p.is_file()]
    assert leftovers == []


def test_workers_run_concurrently_by_wall_clock(client: FakeS3, tmp_path: Path) -> None:
    """8 objects sleeping 0.5s each: sequential is 4s of sleep, 4 workers must beat it."""
    manifest = Manifest.load(tmp_path, "t")
    started = time.monotonic()
    parallel_pull(
        client,
        BUCKET,
        _objects(client),
        tmp_path,
        "t",
        manifest,
        handler_slow,
        workers=4,
        client_factory=FACTORY,
    )
    elapsed = time.monotonic() - started
    assert len(manifest.objects) == 8
    assert elapsed < 3.5, f"took {elapsed:.1f}s, workers are not overlapping"


def test_a_failing_object_stops_the_run_but_keeps_completed_work(
    client: FakeS3, tmp_path: Path
) -> None:
    manifest = Manifest.load(tmp_path, "t")
    with pytest.raises(RuntimeError, match="corrupt object"):
        parallel_pull(
            client,
            BUCKET,
            _objects(client),
            tmp_path,
            "t",
            manifest,
            handler_failing_on_3,
            workers=2,
            client_factory=FACTORY,
        )
    reloaded = Manifest.load(tmp_path, "t")
    assert "job/2026-06-01/3.lz4" not in reloaded.objects
    assert _lake_rows(tmp_path) == reloaded.total_rows().get("trades", 0)


def test_a_rerun_skips_the_manifest_and_finishes_the_rest(client: FakeS3, tmp_path: Path) -> None:
    objects = _objects(client)
    manifest = Manifest.load(tmp_path, "t")
    parallel_pull(client, BUCKET, objects[:3], tmp_path, "t", manifest, handler_one, workers=1)
    client.downloaded.clear()

    manifest = Manifest.load(tmp_path, "t")
    todo = [obj for obj in objects if not manifest.done(str(obj["Key"]))]
    fetched = parallel_pull(client, BUCKET, todo, tmp_path, "t", manifest, handler_one, workers=1)
    assert fetched == 5
    assert sorted(client.downloaded) == sorted(str(o["Key"]) for o in objects[3:])
    assert _lake_rows(tmp_path) == 8


def test_low_disk_stops_cleanly_without_raising(
    client: FakeS3, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(common, "enough_disk", lambda *a, **k: False)
    manifest = Manifest.load(tmp_path, "t")
    fetched = parallel_pull(
        client, BUCKET, _objects(client), tmp_path, "t", manifest, handler_one, workers=1
    )
    assert fetched == 0
    assert "free disk is under the floor" in capsys.readouterr().out
    assert manifest.objects == {}


def test_keep_raw_lands_the_raw_file_under_the_lake(client: FakeS3, tmp_path: Path) -> None:
    objects = _objects(client)[:1]
    manifest = Manifest.load(tmp_path, "t")
    parallel_pull(
        client, BUCKET, objects, tmp_path, "t", manifest, handler_one, workers=1, keep_raw=True
    )
    key = str(objects[0]["Key"])
    assert (tmp_path / "raw" / "t" / key).exists()


def test_merge_staged_never_collides_on_identical_names(tmp_path: Path) -> None:
    lake = tmp_path / "parquet"
    rows = [_row(BASE)]
    stage_a = tmp_path / "sa"
    stage_b = tmp_path / "sb"
    write_partitioned(stage_a, "trades", "hyperliquid", "hl_s3", rows)
    write_partitioned(stage_b, "trades", "hyperliquid", "hl_s3", rows)
    merge_staged(stage_a, lake)
    merge_staged(stage_b, lake)
    files = list((lake / "trades").rglob("*.parquet"))
    assert len(files) == 2
    assert len({f.name for f in files}) == 2
    assert sum(pq.read_metadata(f).num_rows for f in files) == 2
