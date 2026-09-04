"""Shared fixtures."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog


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
