"""Shared fixtures."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


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
