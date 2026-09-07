"""The kill switch. Its own tests, and no path through strategy/.

CLAUDE.md rule 5: `/kill` on Telegram, or the presence of `data/KILL`, must within 5s
cancel every open order, close every position with IOC orders, stop the loop and post a
confirmation. Everything here is about that sentence, including the import test that keeps
the path free of the module most likely to be broken when the kill is needed.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from liqfade.execution.kill import KillSwitch, KillWatcher


class FakeExecutor:
    """Stands in for the order manager and the venue: records what the kill asked for."""

    def __init__(
        self,
        *,
        orders: list[str] | None = None,
        positions: dict[str, float] | None = None,
        slow_s: float = 0.0,
        fail: Exception | None = None,
    ) -> None:
        self.orders = list(orders or [])
        self.positions = dict(positions or {})
        self.cancelled: list[str] = []
        self.closed: list[tuple[str, float]] = []
        self.slow_s = slow_s
        self.fail = fail

    async def open_order_cloids(self) -> list[str]:
        return list(self.orders)

    async def cancel(self, cloid: str) -> None:
        if self.slow_s:
            await asyncio.sleep(self.slow_s)
        if self.fail:
            raise self.fail
        self.cancelled.append(cloid)

    async def open_positions(self) -> dict[str, float]:
        return dict(self.positions)

    async def close_position(self, coin: str, size: float) -> None:
        if self.slow_s:
            await asyncio.sleep(self.slow_s)
        self.closed.append((coin, size))


def a_switch(
    tmp_path: Path, executor: FakeExecutor, posted: list[str] | None = None, **kwargs: Any
) -> KillSwitch:
    async def alert(text: str) -> bool:
        if posted is not None:
            posted.append(text)
        return True

    return KillSwitch(
        kill_file=tmp_path / "KILL",
        cancel_all=executor.cancel,
        open_orders=executor.open_order_cloids,
        open_positions=executor.open_positions,
        close_position=executor.close_position,
        alert=alert,
        **kwargs,
    )


async def test_kill_cancels_every_order_and_closes_every_position(tmp_path: Path) -> None:
    executor = FakeExecutor(orders=["0xa", "0xb"], positions={"ETH": 1.0, "SOL": -20.0})
    posted: list[str] = []
    switch = a_switch(tmp_path, executor, posted)

    result = await switch.fire(reason="manual test")

    assert sorted(executor.cancelled) == ["0xa", "0xb"]
    assert sorted(executor.closed) == [("ETH", 1.0), ("SOL", -20.0)]
    assert result.killed is True
    assert result.canceled_orders == 2
    assert result.closed_positions == 2
    assert posted and "KILL" in posted[0]


async def test_the_flag_is_written_before_anything_is_cancelled(tmp_path: Path) -> None:
    """The durable half first. A crash halfway through must still leave a killed box."""
    kill_file = tmp_path / "KILL"
    seen: list[bool] = []

    class Watching(FakeExecutor):
        async def cancel(self, cloid: str) -> None:
            seen.append(kill_file.exists())
            await super().cancel(cloid)

    executor = Watching(orders=["0xa"])
    await a_switch(tmp_path, executor).fire(reason="ordering")

    assert seen == [True]
    assert kill_file.exists()


async def test_kill_finishes_within_five_seconds(tmp_path: Path) -> None:
    """The number in CLAUDE.md rule 5. Cancels and closes fan out rather than queueing."""
    executor = FakeExecutor(
        orders=[f"0x{i:02x}" for i in range(40)],
        positions={f"C{i}": 1.0 for i in range(20)},
        slow_s=0.05,
    )
    switch = a_switch(tmp_path, executor)

    started = time.monotonic()
    result = await switch.fire(reason="timing")
    elapsed = time.monotonic() - started

    assert elapsed < 5.0
    # Serialised, sixty calls at 50ms each is three seconds. Concurrent, it is two rounds.
    assert elapsed < 1.0
    assert result.canceled_orders == 40
    assert result.closed_positions == 20


async def test_a_cancel_that_fails_does_not_stop_the_close(tmp_path: Path) -> None:
    """A kill that gives up on the first error leaves the position it exists to close."""
    executor = FakeExecutor(
        orders=["0xa"], positions={"ETH": 1.0}, fail=RuntimeError("venue said no")
    )
    posted: list[str] = []
    switch = a_switch(tmp_path, executor, posted)

    result = await switch.fire(reason="partial failure")

    assert executor.closed == [("ETH", 1.0)]
    assert result.canceled_orders == 0
    assert result.killed is True
    assert "1 cancel" in (result.reason or "") or "failed" in (result.reason or "")


async def test_kill_is_idempotent_and_reports_the_second_call(tmp_path: Path) -> None:
    executor = FakeExecutor(orders=["0xa"])
    switch = a_switch(tmp_path, executor)

    await switch.fire(reason="first")
    executor.orders = []
    second = await switch.fire(reason="second")

    assert second.killed is True
    assert switch.fired is True
    assert executor.cancelled == ["0xa"]


async def test_a_flag_that_cannot_be_written_is_reported_not_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kill without its flag is undone by the unit's Restart=always three seconds later."""
    executor = FakeExecutor(orders=["0xa"])
    switch = a_switch(tmp_path, executor)
    monkeypatch.setattr("liqfade.execution.kill.write_kill_file", lambda path, reason=None: False)

    result = await switch.fire(reason="disk full")

    assert result.killed is False
    assert "NOT written" in (result.reason or "")
    # It still cancels. The half that works must run even when the half that persists did not.
    assert executor.cancelled == ["0xa"]


async def test_the_file_watcher_fires_the_switch(tmp_path: Path) -> None:
    """The other trigger: a KILL file appearing, from a human or from the panel's absence."""
    executor = FakeExecutor(orders=["0xa"], positions={"ETH": 1.0})
    switch = a_switch(tmp_path, executor)
    stop = asyncio.Event()
    watcher = KillWatcher(switch, poll_s=0.02)
    task = asyncio.create_task(watcher.run(stop))

    await asyncio.sleep(0.05)
    assert not switch.fired
    (tmp_path / "KILL").write_text("killed_at: by hand\n")

    await asyncio.sleep(0.2)  # ten poll intervals, so a miss is a bug and not a race
    stop.set()
    await task

    assert switch.fired is True

    assert executor.cancelled == ["0xa"]
    assert executor.closed == [("ETH", 1.0)]


async def test_the_watcher_fires_once_for_one_file(tmp_path: Path) -> None:
    executor = FakeExecutor(orders=["0xa"])
    switch = a_switch(tmp_path, executor)
    (tmp_path / "KILL").write_text("already there\n")
    stop = asyncio.Event()
    watcher = KillWatcher(switch, poll_s=0.01)
    task = asyncio.create_task(watcher.run(stop))

    await asyncio.sleep(0.1)
    stop.set()
    await task

    assert executor.cancelled == ["0xa"]


def test_the_kill_path_does_not_import_strategy() -> None:
    """CLAUDE.md rule 5: this path must not depend on the strategy module.

    Walks the real import graph rather than reading the file, because the dependency that
    breaks this rule arrives through a helper two modules away. In a subprocess, because
    the only way to measure a fresh import graph in-process is to purge sys.modules, and
    that breaks every later test that monkeypatched a module object.
    """
    import subprocess
    import sys

    program = (
        "import sys, liqfade.execution.kill;"
        "print(','.join(sorted(n for n in sys.modules if n.startswith('liqfade.strategy'))))"
    )
    leaked = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert leaked == "", f"the kill path imported {leaked}"


def test_a_flag_we_cannot_overwrite_but_that_exists_still_counts_as_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The VPS drill of 2026-09-06 found this one.

    The owner drops data/KILL by hand as root, the service user cannot overwrite it, and
    the kill reported "NOT written, a restart will not stay killed" about a file sitting
    right there. What matters is that the flag is on disk, not who put it there.
    """
    from liqfade.control.backend import write_kill_file

    flag = tmp_path / "KILL"
    flag.write_text("killed_at: by hand\n")

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "write_text", refuse)
    assert write_kill_file(flag, "kill file appeared") is True


def test_a_flag_that_is_neither_writable_nor_present_is_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from liqfade.control.backend import write_kill_file

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "write_text", refuse)
    assert write_kill_file(tmp_path / "KILL", "nowhere to write") is False
