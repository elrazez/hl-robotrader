"""Shared fixtures."""

from __future__ import annotations

import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog

from liqfade.config import resolve
from liqfade.config.models import Settings
from liqfade.data.ingest import common as ingest_common


@pytest.fixture(autouse=True)
def restore_structlog() -> Iterator[None]:
    """Put structlog back the way the test found it.

    setup_logging configures structlog globally and binds contextvars, so a test that
    calls it changes the log format and the bound keys for every test that runs after.
    That is not hypothetical: test_cli's test_status_json reads the first stdout line
    starting with "{", which only works while structlog is unconfigured and rendering
    console output, so any earlier test configuring logging broke it from a distance.

    Restoring rather than forbidding, because testing what the logs actually contain is
    the only way to catch a secret reaching them.
    """
    configured = structlog.is_configured()
    config = structlog.get_config().copy()
    context = structlog.contextvars.get_contextvars()
    try:
        yield
    finally:
        if configured:
            structlog.configure(**config)
        else:
            # Never configured: configure() would itself mark it configured.
            structlog.reset_defaults()
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(**context)


@pytest.fixture(autouse=True)
def plenty_of_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide the host's real free space from the ingest disk floor.

    MIN_FREE_GIB is 30, tuned for the VPS lake drive; a GitHub runner has about 20 free,
    so every ingest test stopped at "free disk is under the floor" in CI and nowhere else.
    Patched at `free_gib`, the one place both floors read, so the copy of `enough_disk`
    imported into hl_asset_ctxs is covered too. Tests that want a full disk still
    monkeypatch `enough_disk` themselves, and that patch wins over this one.
    """
    monkeypatch.setattr(ingest_common, "free_gib", lambda path: 1024.0)


@pytest.fixture
def settings_factory() -> Callable[..., Settings]:
    """Build paper `Settings` with dotted-key overrides, for the strategy layer's tests.

    `settings_factory(**{"exhaustion.mode": "rule", "cascade.liq_z_min": 2.0})`. The models
    are frozen, so an override rebuilds the branch it touches rather than mutating it, and
    every override still goes through the same validation a YAML file would. Tests that
    want a different threshold must not hand-build a Settings: a model that skipped
    validation is a config the loader would have refused.
    """

    def make(**dotted: Any) -> Settings:
        merged, _ = resolve("paper")
        for key, value in dotted.items():
            node = merged
            parts = key.split(".")
            for part in parts[:-1]:
                node = node[part]
            node[parts[-1]] = value
        return Settings.model_validate(merged)

    return make


@pytest.fixture(scope="session")
def research_lake(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Four hours of synthetic tape with about fifty injected cascades.

    Built once per session rather than committed: a generator stays in step with
    `data/schemas.py` and keeps half a megabyte of Parquet out of the repo. See
    `tests/fixtures/research_lake.py` for what it does and does not prove.
    """
    sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
    import research_lake as builder

    root = tmp_path_factory.mktemp("lake")
    builder.build(root)
    return root


@pytest.fixture
def sock_dir() -> Iterator[Path]:
    """A short lived directory with a short path.

    pytest's tmp_path embeds the test name and easily exceeds the ~104 byte limit on
    sockaddr_un.sun_path, especially on macOS, so anything binding a unix socket uses
    this instead.
    """
    path = Path(tempfile.mkdtemp(prefix="lf"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class FakeS3:
    """A stand-in for a boto3 S3 client, holding objects in memory.

    Every method asserts the requester-pays header. A requester-pays bucket answers a
    request without it with 403, so a job that forgets it would burn a run against the
    real thing; here it fails in under a millisecond instead.
    """

    def __init__(self, objects: dict[str, bytes], *, page_size: int = 1000) -> None:
        self.objects = objects
        self.page_size = page_size
        self.list_calls = 0
        self.downloaded: list[str] = []

    @staticmethod
    def _require_payer(kwargs: dict[str, object]) -> None:
        assert kwargs.get("RequestPayer") == "requester", "requester-pays header missing"

    def list_objects_v2(self, **kwargs: object) -> dict[str, object]:
        self._require_payer(kwargs)
        self.list_calls += 1
        prefix = str(kwargs.get("Prefix", ""))
        keys = sorted(key for key in self.objects if key.startswith(prefix))
        start = (
            keys.index(str(kwargs["ContinuationToken"])) if kwargs.get("ContinuationToken") else 0
        )
        page = keys[start : start + self.page_size]
        truncated = start + self.page_size < len(keys)
        return {
            "Contents": [{"Key": key, "Size": len(self.objects[key])} for key in page],
            "IsTruncated": truncated,
            "NextContinuationToken": keys[start + self.page_size] if truncated else None,
        }

    def download_file(
        self, *, Bucket: str, Key: str, Filename: str, ExtraArgs: dict[str, object]
    ) -> None:
        self._require_payer(ExtraArgs)
        self.downloaded.append(Key)
        Path(Filename).parent.mkdir(parents=True, exist_ok=True)
        Path(Filename).write_bytes(self.objects[Key])
