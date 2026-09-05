"""Atomic writer for data/state.json.

Every reader (panel, CLI fallback, watchdog) reads this file rather than talking to the
trading loop, so a slow or wedged reader can never stall a decision tick. The write is
temp file plus os.replace, which is atomic within a filesystem, so a reader either sees
the previous complete snapshot or the new complete one and never a partial file.

Mode 0600, set on the temp file before the replace. The snapshot holds equity, open
positions and resting orders, so it gets the same treatment as the control socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

import orjson

from liqfade.control.protocol import StateSnapshot
from liqfade.log import get_logger

log = get_logger(__name__)

SnapshotSource = Callable[[], Awaitable[StateSnapshot]]


def write_state(path: Path, snapshot: StateSnapshot) -> None:
    """Write one snapshot atomically. Safe to call from anywhere, including tests."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # The temp file must be in the same directory so os.replace stays a rename, not a copy.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = orjson.dumps(snapshot.model_dump(mode="json"), option=orjson.OPT_INDENT_2)
    try:
        with open(tmp, "wb") as handle:
            # 0600 on the temp file, before the replace, so the snapshot is never readable
            # by anything but the service user. It carries equity, open positions and
            # resting orders and lands in a data dir the panel and rsync also see; the
            # default umask made it 0644 on the box.
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def read_state(path: Path) -> StateSnapshot:
    """Read and validate a snapshot. Raises FileNotFoundError or ValueError."""
    return StateSnapshot.model_validate(orjson.loads(Path(path).read_bytes()))


class StateWriter:
    """Calls a snapshot source on an interval and writes the result atomically.

    A failure to produce or write a snapshot is logged and the loop continues: a state
    file that goes stale is a monitoring problem, not a reason to take the trading loop
    down. Readers detect staleness from ts_ms against panel.state_stale_s.
    """

    def __init__(self, source: SnapshotSource, path: Path, interval_s: float = 1.0) -> None:
        self.source = source
        self.path = Path(path)
        self.interval_s = interval_s
        self._task: asyncio.Task[None] | None = None

    async def write_once(self) -> StateSnapshot:
        snapshot = await self.source()
        # The write fsyncs, which can stall for tens of milliseconds on a busy disk. The
        # live loop decides every 250ms, so it happens on a thread rather than inline.
        await asyncio.to_thread(write_state, self.path, snapshot)
        return snapshot

    async def run(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                await self.write_once()
            except Exception:
                log.exception("state_write_failed", path=str(self.path))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.interval_s)

    def start(self, stop: asyncio.Event | None = None) -> asyncio.Task[None]:
        self._task = asyncio.create_task(self.run(stop), name="state-writer")
        return self._task

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
