"""The parallel pull: staging, the serialized mover, resume and failure behavior."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import lz4.frame
import pyarrow.parquet as pq
import pytest

from liqfade.data.ingest import common
from liqfade.data.ingest.common import Manifest, merge_staged, parallel_pull, write_partitioned
from tests.conftest import FakeS3

BUCKET = "fake-bucket"


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


def _handler_writing(n_rows: int):
    """A handler that writes `n_rows` trades derived from the raw payload timestamp."""

    def handler(local: Path, key: str, stage_root: Path) -> dict[str, int]:
        base_ts = int(lz4.frame.decompress(local.read_bytes()))
        rows = [_row(base_ts + i) for i in range(n_rows)]
        written = write_partitioned(stage_root, "trades", "hyperliquid", "hl_s3", rows)
        return {"trades": written}

    return handler


def _objects(client: FakeS3) -> list[dict[str, Any]]:
    return [{"Key": key, "Size": len(data)} for key, data in sorted(client.objects.items())]


def _lake_rows(data_dir: Path) -> int:
    files = list((data_dir / "parquet" / "trades").rglob("*.parquet"))
    return sum(pq.read_metadata(path).num_rows for path in files)


# Timestamps on 2026-06-01, one per hour, so objects map to distinct partitions.
BASE = 1_780_300_800_000


@pytest.fixture
def client() -> FakeS3:
    objects = {
        f"job/2026-06-01/{i}.lz4": lz4.frame.compress(str(BASE + i * 3_600_000).encode())
        for i in range(8)
    }
    return FakeS3(objects)


def test_parallel_pull_completes_everything_and_the_manifest_agrees(
    client: FakeS3, tmp_path: Path
) -> None:
    manifest = Manifest.load(tmp_path, "t")
    fetched = parallel_pull(
        client, BUCKET, _objects(client), tmp_path, "t", manifest, _handler_writing(5), workers=4
    )
    assert fetched == 8
    assert len(manifest.objects) == 8
    assert manifest.total_rows() == {"trades": 40}
    assert _lake_rows(tmp_path) == 40
    # No staging or temp litter anywhere under the lake.
    leftovers = [p for p in tmp_path.rglob("*") if p.name.startswith(".") and p.is_file()]
    assert leftovers == []


def test_workers_actually_overlap(client: FakeS3, tmp_path: Path) -> None:
    """With 4 workers, at least two handlers must be in flight at once."""
    in_flight = 0
    peak = 0
    lock = threading.Lock()
    gate = threading.Event()
    inner = _handler_writing(1)

    def handler(local: Path, key: str, stage_root: Path) -> dict[str, int]:
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        if in_flight >= 2:
            gate.set()
        gate.wait(timeout=5.0)  # hold until a second worker arrives, or give up
        result = inner(local, key, stage_root)
        with lock:
            in_flight -= 1
        return result

    manifest = Manifest.load(tmp_path, "t")
    parallel_pull(client, BUCKET, _objects(client), tmp_path, "t", manifest, handler, workers=4)
    assert peak >= 2


def test_a_failing_object_stops_the_run_but_keeps_completed_work(
    client: FakeS3, tmp_path: Path
) -> None:
    bad_key = sorted(client.objects)[3]
    inner = _handler_writing(2)

    def handler(local: Path, key: str, stage_root: Path) -> dict[str, int]:
        if key == bad_key:
            raise RuntimeError("corrupt object")
        return inner(local, key, stage_root)

    manifest = Manifest.load(tmp_path, "t")
    with pytest.raises(RuntimeError, match="corrupt object"):
        parallel_pull(client, BUCKET, _objects(client), tmp_path, "t", manifest, handler, workers=2)
    # Whatever completed is in the manifest and on disk, consistently.
    reloaded = Manifest.load(tmp_path, "t")
    assert bad_key not in reloaded.objects
    assert _lake_rows(tmp_path) == reloaded.total_rows().get("trades", 0)


def test_a_rerun_skips_the_manifest_and_finishes_the_rest(client: FakeS3, tmp_path: Path) -> None:
    objects = _objects(client)
    manifest = Manifest.load(tmp_path, "t")
    parallel_pull(
        client, BUCKET, objects[:3], tmp_path, "t", manifest, _handler_writing(1), workers=2
    )
    client.downloaded.clear()

    manifest = Manifest.load(tmp_path, "t")
    todo = [obj for obj in objects if not manifest.done(str(obj["Key"]))]
    fetched = parallel_pull(
        client, BUCKET, todo, tmp_path, "t", manifest, _handler_writing(1), workers=2
    )
    assert fetched == 5
    assert sorted(client.downloaded) == sorted(str(o["Key"]) for o in objects[3:])
    assert _lake_rows(tmp_path) == 8


def test_low_disk_stops_cleanly_without_raising(
    client: FakeS3, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(common, "enough_disk", lambda *a, **k: False)
    manifest = Manifest.load(tmp_path, "t")
    fetched = parallel_pull(
        client, BUCKET, _objects(client), tmp_path, "t", manifest, _handler_writing(1), workers=3
    )
    assert fetched == 0
    assert "free disk is under the floor" in capsys.readouterr().out
    assert manifest.objects == {}


def test_keep_raw_lands_the_raw_file_under_the_lake(client: FakeS3, tmp_path: Path) -> None:
    objects = _objects(client)[:1]
    manifest = Manifest.load(tmp_path, "t")
    parallel_pull(
        client,
        BUCKET,
        objects,
        tmp_path,
        "t",
        manifest,
        _handler_writing(1),
        workers=1,
        keep_raw=True,
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
