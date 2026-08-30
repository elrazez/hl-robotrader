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
