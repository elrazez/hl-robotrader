"""The watchdog: what happens when the loop stops breathing.

It is a separate process on purpose, so its tests drive the two things it can see, the
heartbeat file and the control socket, and never the loop itself.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from liqfade.execution.watchdog import (
    Watchdog,
    heartbeat_age_s,
    read_heartbeat,
    write_heartbeat_atomic,
)


def write_heartbeat(path: Path, ms: int) -> None:
    path.write_text(f"{ms}\n")


def test_a_missing_heartbeat_is_not_a_young_one(tmp_path: Path) -> None:
    """Absent must never read as fresh: that is the failure the watchdog exists for."""
    assert read_heartbeat(tmp_path / "heartbeat") is None
    assert heartbeat_age_s(tmp_path / "heartbeat", now_ms=1_000) is None


def test_an_unreadable_heartbeat_is_also_none(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"
    path.write_text("not a number\n")
    assert read_heartbeat(path) is None


def test_the_age_is_seconds_since_the_stamp(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"
    write_heartbeat(path, 10_000)
    assert heartbeat_age_s(path, now_ms=13_500) == pytest.approx(3.5)


async def test_a_fresh_heartbeat_raises_nothing(tmp_path: Path) -> None:
    posted: list[str] = []
    dog = _a_watchdog(tmp_path, posted, stale_s=10.0)
    write_heartbeat(tmp_path / "heartbeat", _now())

    assert await dog.check() is True
    assert posted == []
    assert dog.alerts == 0


async def test_a_stale_heartbeat_alerts_once_not_every_tick(tmp_path: Path) -> None:
    """A wedged loop is one outage. Five hundred identical messages is not monitoring."""
    posted: list[str] = []
    dog = _a_watchdog(tmp_path, posted, stale_s=1.0)
    write_heartbeat(tmp_path / "heartbeat", _now() - 60_000)

    assert await dog.check() is False
    assert await dog.check() is False

    assert len(posted) == 1
    assert "heartbeat" in posted[0]
    assert dog.alerts == 1


async def test_it_says_so_when_the_loop_comes_back(tmp_path: Path) -> None:
    posted: list[str] = []
    dog = _a_watchdog(tmp_path, posted, stale_s=1.0)
    write_heartbeat(tmp_path / "heartbeat", _now() - 60_000)
    await dog.check()

    write_heartbeat(tmp_path / "heartbeat", _now())
    assert await dog.check() is True

    assert len(posted) == 2
    assert "recovered" in posted[1].lower()


async def test_a_missing_heartbeat_alerts_like_a_stale_one(tmp_path: Path) -> None:
    posted: list[str] = []
    dog = _a_watchdog(tmp_path, posted, stale_s=1.0)

    assert await dog.check() is False
    assert "no heartbeat" in posted[0].lower()


async def test_it_never_alerts_while_the_kill_flag_is_there(tmp_path: Path) -> None:
    """A killed box has a dead loop on purpose. Paging about it is noise at 3am."""
    posted: list[str] = []
    dog = _a_watchdog(tmp_path, posted, stale_s=1.0)
    (tmp_path / "KILL").write_text("killed_at: deliberately\n")

    assert await dog.check() is True
    assert posted == []


async def test_the_run_loop_polls_until_stopped(tmp_path: Path) -> None:
    posted: list[str] = []
    dog = _a_watchdog(tmp_path, posted, stale_s=1.0, poll_s=0.02)
    write_heartbeat(tmp_path / "heartbeat", _now())
    stop = asyncio.Event()

    task = asyncio.create_task(dog.run(stop))
    await asyncio.sleep(0.1)
    stop.set()
    await task

    assert dog.checks >= 3


def _now() -> int:
    import time

    return int(time.time() * 1000)


def _a_watchdog(tmp_path: Path, posted: list[str], **kwargs: float) -> Watchdog:
    async def alert(text: str) -> bool:
        posted.append(text)
        return True

    return Watchdog(
        heartbeat=tmp_path / "heartbeat",
        kill_file=tmp_path / "KILL",
        alert=alert,
        **kwargs,  # type: ignore[arg-type]
    )


def a_dog(tmp_path: Path, posted: list[str] | None = None, **kwargs: object) -> Watchdog:
    async def alert(text: str) -> bool:
        if posted is not None:
            posted.append(text)
        return True

    return Watchdog(
        heartbeat=tmp_path / "heartbeat",
        kill_file=tmp_path / "KILL",
        alert=alert,
        stale_s=10.0,
        **kwargs,  # type: ignore[arg-type]
    )


async def test_a_stale_heartbeat_writes_the_kill_flag(tmp_path: Path) -> None:
    """The unit says it cancels everything. Writing the flag is how it does that.

    The watchdog has no key and cannot cancel at the venue. What it can do is put the flag
    where the loop's own watcher and the unit's ExecStartPre both look, which cancels,
    flattens and stops the restart, and needs nothing but a filesystem.
    """
    posted: list[str] = []
    write_heartbeat(tmp_path / "heartbeat", 1_000)
    dog = a_dog(tmp_path, posted)

    assert await dog.check(now_ms=60_000) is False

    assert (tmp_path / "KILL").exists()
    assert "stale" in (tmp_path / "KILL").read_text().lower()
    assert any("KILL" in text for text in posted)


async def test_a_missing_heartbeat_writes_the_kill_flag_too(tmp_path: Path) -> None:
    """Never having breathed is staler than having stopped."""
    dog = a_dog(tmp_path)

    assert await dog.check(now_ms=60_000) is False

    assert (tmp_path / "KILL").exists()


async def test_a_healthy_loop_is_never_killed(tmp_path: Path) -> None:
    write_heartbeat(tmp_path / "heartbeat", 59_000)
    dog = a_dog(tmp_path)

    assert await dog.check(now_ms=60_000) is True

    assert not (tmp_path / "KILL").exists()


async def test_the_flag_is_written_once_not_every_poll(tmp_path: Path) -> None:
    """The edge is what is worth acting on. Rewriting the flag would move its timestamp."""
    write_heartbeat(tmp_path / "heartbeat", 1_000)
    dog = a_dog(tmp_path)

    await dog.check(now_ms=60_000)
    first = (tmp_path / "KILL").read_text()
    await dog.check(now_ms=61_000)

    assert (tmp_path / "KILL").read_text() == first


def test_the_stale_threshold_comes_from_the_config_not_an_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One definition of it. `risk.watchdog_stale_s` is validated against heartbeat_s."""
    from liqfade.config import load_settings
    from liqfade.execution.watchdog import build_watchdog

    monkeypatch.setenv("LIQFADE_WATCH_STALE_S", "999")
    settings = load_settings("paper")

    dog = build_watchdog(settings)

    assert dog.stale_s == settings.risk.watchdog_stale_s
    assert dog.stale_s != 999.0


def test_a_reader_never_catches_the_heartbeat_half_written(tmp_path: Path) -> None:
    """The watchdog reads this file while the loop writes it, with no lock between them.

    `Path.write_text` truncates in place, so a read landing between the truncate and the
    flush sees an empty file. `read_heartbeat` maps every failure to None and the watchdog
    reads None as a dead loop, so a torn read kills a healthy process. This is not
    hypothetical: it is what wrote data/KILL on 2026-09-07 at 15:07:10 UTC, four
    milliseconds after the loop stamped the very heartbeat the watchdog could not read.
    """
    import threading
    import time

    path = tmp_path / "heartbeat"
    write_heartbeat_atomic(path, 1)
    stop = threading.Event()

    def writer() -> None:
        while not stop.is_set():
            write_heartbeat_atomic(path, int(time.time() * 1000))
            time.sleep(0.001)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        unreadable = sum(1 for _ in range(20_000) if read_heartbeat(path) is None)
    finally:
        stop.set()
        thread.join()

    assert unreadable == 0


def test_the_stale_limit_must_be_several_heartbeats_not_one_and_a_bit() -> None:
    """A limit one beat above the interval trips on the first missed write.

    The validator only asked for `>`, so a config could set the heartbeat to 9.9s and the
    stale limit to 10.0s and be accepted, leaving 0.1s of headroom for a poll interval, a
    write, a page fault and a busy box.
    """
    from pydantic import ValidationError

    from liqfade.config import load_settings
    from liqfade.config.models import Risk

    fields = load_settings("paper", environ={}).risk.model_dump()
    with pytest.raises(ValidationError, match="heartbeat"):
        Risk.model_validate({**fields, "heartbeat_s": 9.9, "watchdog_stale_s": 10.0})


async def test_clearing_the_flag_gives_the_loop_time_to_start_beating(tmp_path: Path) -> None:
    """Drilled on 2026-09-08: clearing a deliberate kill killed the box again at once.

    A kill stops the loop, so its heartbeat file stops moving and ages for as long as the
    flag is up. Remove the flag and the watchdog's very next poll reads a heartbeat six
    minutes old and writes the flag straight back, before the loop it is judging has had a
    chance to start. The unit then failed to start 131 times.

    The watchdog exists to notice a loop that stopped, not to judge one that has not
    started. Clearing the flag starts a grace period of one stale limit.
    """
    posted: list[str] = []
    # A long grace on purpose: the test asserts the grace is *in force*, and a one second
    # window can expire between two checks on a loaded machine, which would make this pass
    # or fail on how busy the box is rather than on the behaviour.
    dog = _a_watchdog(tmp_path, posted, stale_s=30.0)
    kill = tmp_path / "KILL"
    kill.write_text("killed_at: deliberately\n")
    write_heartbeat(tmp_path / "heartbeat", _now() - 300_000)

    assert await dog.check() is True, "silent while the flag is up"

    kill.unlink()

    assert await dog.check() is True, "the loop is starting, not dead"
    assert not kill.exists(), "and the flag stays cleared"
    assert posted == []


async def test_the_grace_runs_out_if_the_loop_never_comes_back(tmp_path: Path) -> None:
    """A grace that never ends is not a watchdog."""
    posted: list[str] = []
    dog = _a_watchdog(tmp_path, posted, stale_s=0.05)
    kill = tmp_path / "KILL"
    kill.write_text("killed_at: deliberately\n")
    write_heartbeat(tmp_path / "heartbeat", _now() - 300_000)
    await dog.check()
    kill.unlink()
    await dog.check()

    await asyncio.sleep(0.12)

    assert await dog.check() is False, "grace expired and the loop is still not beating"
    assert kill.exists()
