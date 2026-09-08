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


class Hanging(FakeExecutor):
    """A venue that accepted the request and will never answer. The worst case."""

    async def cancel(self, cloid: str) -> None:
        await asyncio.Event().wait()

    async def close_position(self, coin: str, size: float) -> None:
        await asyncio.Event().wait()


async def test_a_hung_venue_does_not_cost_the_kill_its_budget(tmp_path: Path) -> None:
    """CLAUDE.md rule 5 promises five seconds. An untimed venue call promises nothing."""
    executor = Hanging(orders=["0xa", "0xb"], positions={"ETH": 1.0})
    posted: list[str] = []
    switch = a_switch(tmp_path, executor, posted, budget_s=0.4)

    started = time.monotonic()
    result = await switch.fire(reason="hung venue")
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert result.killed is True
    assert (tmp_path / "KILL").exists()
    assert result.canceled_orders == 0
    assert result.closed_positions == 0
    # Abandoned, and named. A kill that silently gave up on an order is worse than one
    # that says which order it gave up on.
    assert "0xa" in (result.reason or "")
    assert "ETH" in (result.reason or "")
    assert posted and "0xa" in posted[0]


async def test_a_hung_cancel_does_not_stop_the_close_from_being_tried(tmp_path: Path) -> None:
    """The close is the half that carries the risk, so it gets its own share of the budget."""

    class HangingCancel(FakeExecutor):
        async def cancel(self, cloid: str) -> None:
            await asyncio.Event().wait()

    executor = HangingCancel(orders=["0xa"], positions={"ETH": 1.0})
    switch = a_switch(tmp_path, executor, budget_s=0.4)

    result = await switch.fire(reason="hung cancel")

    assert executor.closed == [("ETH", 1.0)]
    assert result.closed_positions == 1
    assert result.canceled_orders == 0


async def test_a_second_kill_during_one_joins_it_rather_than_waiting_for_it(
    tmp_path: Path,
) -> None:
    """A retried /kill during a slow kill must answer with the one in flight.

    The lock made the second caller wait for the first sweep and then run a second, which
    is two budgets for one question, from a caller who has already
    typed /kill twice.
    """
    executor = FakeExecutor(orders=["0xa"], positions={"ETH": 1.0}, slow_s=0.2)
    switch = a_switch(tmp_path, executor, budget_s=2.0)

    started = time.monotonic()
    first, second = await asyncio.gather(switch.fire(reason="one"), switch.fire(reason="two"))
    elapsed = time.monotonic() - started

    assert first == second
    assert elapsed < 0.6  # one sweep, not two queued behind each other
    assert executor.cancelled == ["0xa"]
    assert executor.closed == [("ETH", 1.0)]


def test_the_kill_budget_has_to_leave_room_inside_the_deadline() -> None:
    """The budget bounds the venue calls; the deadline is what rule 5 promises the owner."""
    from pydantic import ValidationError

    from liqfade.config import resolve
    from liqfade.config.models import Settings

    merged, _ = resolve("paper")
    assert Settings.model_validate(merged).risk.kill_budget_s < merged["risk"]["kill_deadline_s"]

    merged["risk"]["kill_budget_s"] = merged["risk"]["kill_deadline_s"]
    with pytest.raises(ValidationError, match="kill_budget_s"):
        Settings.model_validate(merged)


async def test_the_confirmation_names_a_few_abandoned_targets_and_counts_the_rest(
    tmp_path: Path,
) -> None:
    """The drill of 2026-09-07 found this one.

    Sixty hanging calls put sixty 34 character cloids in the Telegram confirmation, which
    is two thousand characters of hex in the one message that has to be read at a glance.
    The count is the number that matters and a few examples are what makes it actionable;
    the full list stays in the log line, where a machine reads it.
    """
    executor = Hanging(orders=[f"0x{i:032x}" for i in range(40)])
    posted: list[str] = []
    switch = a_switch(tmp_path, executor, posted, budget_s=0.2)

    result = await switch.fire(reason="many")

    assert "40" in (result.reason or "")
    assert posted
    assert len(posted[0]) < 400, f"confirmation is {len(posted[0])} characters"
    assert "0x" + "0" * 32 in posted[0]  # still names some of them


# --- the venue decides whether a kill worked, not the counters --------------------


class VenueBook:
    """A venue that only really lets go of things when told to."""

    def __init__(
        self, *, positions: dict[str, float], orders: list[str], stubborn: int | None = 0
    ) -> None:
        self.positions = dict(positions)
        self.orders = list(orders)
        self.close_calls: list[str] = []
        # How many close attempts to ignore before working. None never works, which is
        # a venue that accepts every close and holds the position anyway.
        self.stubborn = stubborn

    async def open_orders(self) -> list[str]:
        return list(self.orders)

    async def cancel(self, cloid: str) -> None:
        if cloid in self.orders:
            self.orders.remove(cloid)

    async def open_positions(self) -> dict[str, float]:
        return dict(self.positions)

    async def close(self, coin: str, size: float = 0.0) -> None:
        self.close_calls.append(coin)
        if self.stubborn is None:
            return  # accepted and did nothing, forever
        if self.stubborn > 0:
            self.stubborn -= 1
            return  # accepted and did nothing, which is what the refused Ioc did
        self.positions.pop(coin, None)

    async def venue_state(self) -> tuple[dict[str, float], list[str]]:
        return dict(self.positions), list(self.orders)


async def test_a_kill_confirms_against_the_venue_not_its_own_counters(tmp_path: Path) -> None:
    """Drilled on testnet: "cancelled 2, closed 1" while the venue held all of it.

    The counters say what was attempted. Only a read of clearinghouseState and openOrders
    says what is true, and that read is the pass criterion for every kill from here on.
    """
    venue = VenueBook(positions={"ETH": 0.064}, orders=["0xa", "0xb"])
    switch = KillSwitch(
        kill_file=tmp_path / "KILL",
        open_orders=venue.open_orders,
        cancel_all=venue.cancel,
        open_positions=venue.open_positions,
        close_position=venue.close,
        venue_state=venue.venue_state,
        budget_s=2.0,
    )

    result = await switch.fire("drill")

    assert result.venue_flat is True
    assert result.venue_positions == {}
    assert result.venue_orders == 0


async def test_a_kill_that_cannot_flatten_says_so_and_retries(tmp_path: Path) -> None:
    """A close the venue accepts and ignores must not read as done.

    This is exactly what the refused reduce-only order looked like: accepted, counted,
    and the position still there. The sweep retries within its budget and, if the venue
    still holds something, the result says NOT flat and names what is left.
    """
    posted: list[str] = []

    async def alert(text: str) -> bool:
        posted.append(text)
        return True

    venue = VenueBook(positions={"ETH": 0.064}, orders=[], stubborn=1)
    switch = KillSwitch(
        kill_file=tmp_path / "KILL",
        open_orders=venue.open_orders,
        cancel_all=venue.cancel,
        open_positions=venue.open_positions,
        close_position=venue.close,
        venue_state=venue.venue_state,
        alert=alert,
        budget_s=3.0,
    )

    result = await switch.fire("drill")

    assert venue.close_calls[:2] == ["ETH", "ETH"], "it retried the close it could not confirm"
    assert result.venue_flat is True
    assert "flat" in posted[0].lower()


async def test_a_kill_that_never_flattens_reports_what_is_left(tmp_path: Path) -> None:
    posted: list[str] = []

    async def alert(text: str) -> bool:
        posted.append(text)
        return True

    venue = VenueBook(positions={"ETH": 0.064}, orders=[], stubborn=None)
    switch = KillSwitch(
        kill_file=tmp_path / "KILL",
        open_orders=venue.open_orders,
        cancel_all=venue.cancel,
        open_positions=venue.open_positions,
        close_position=venue.close,
        venue_state=venue.venue_state,
        alert=alert,
        budget_s=1.0,
    )

    result = await switch.fire("drill")

    assert result.venue_flat is False
    assert result.venue_positions == {"ETH": 0.064}
    assert "NOT flat" in posted[0] or "not flat" in posted[0].lower()
    assert "ETH" in posted[0]


async def test_a_kill_closes_what_the_venue_holds_not_what_we_remember(tmp_path: Path) -> None:
    """Drilled: "closed 0" while Hyperliquid held two positions.

    The local book is refreshed on reconciliation, so it lags by up to
    `execution.reconcile_s`, and a position opened since the last one is invisible to it.
    A kill that closes only what it remembers leaves the rest on.
    """
    venue = VenueBook(positions={"ETH": 0.018, "SOL": 0.37}, orders=[])

    async def stale_mirror() -> dict[str, float]:
        return {}  # what the loop believed

    switch = KillSwitch(
        kill_file=tmp_path / "KILL",
        open_orders=venue.open_orders,
        cancel_all=venue.cancel,
        open_positions=stale_mirror,
        close_position=venue.close,
        venue_state=venue.venue_state,
        budget_s=2.0,
    )

    result = await switch.fire("drill")

    assert sorted(venue.close_calls) == ["ETH", "SOL"]
    assert result.venue_flat is True


def test_the_first_reason_survives_a_second_writer(tmp_path: Path) -> None:
    """The reason in the flag belongs to whoever killed first.

    Drilled on 2026-09-08: the watchdog wrote "paper loop is stale", the trading loop saw
    the flag and rewrote it with "kill file at data/KILL", and the only record of why the
    box died was gone before anyone read it.
    """
    from liqfade.control.backend import write_kill_file

    flag = tmp_path / "KILL"
    assert write_kill_file(flag, "watchdog: paper loop is stale") is True
    first = flag.read_text()

    assert write_kill_file(flag, "kill file at data/KILL") is True
    assert flag.read_text() == first
    assert "watchdog" in flag.read_text()
