"""The kill switch: cancel everything, close everything, stop, confirm. Within 5 seconds.

CLAUDE.md rule 5, and the module it names as having to stand on its own. Two things follow
from "this path must not depend on the strategy module", and the second is the one that
matters:

1. Nothing here imports `strategy/`, and a test walks the import graph to prove it rather
   than reading the file, because that dependency arrives through a helper two modules away.
2. Nothing here needs the trading loop to be healthy. The switch is a handful of
   callables handed in at construction, so it fires from the Telegram bot, from the file
   watcher and from a wedged loop's own signal handler with the same code.

**Order of operations, and why.** The flag is written first, before a single cancel. The
live systemd unit carries `Restart=always` and gates on the flag in `ExecStartPre`, so a
kill that only stopped the process is undone three seconds later, and a crash halfway
through flattening leaves nothing behind at all. Then cancels and closes, all of them
fanned out concurrently: sixty serialised round trips at a Tokyo RTT is most of the five
second budget spent queueing, and the budget is the requirement.

**Every failure is survived, not raised.** One venue error must not stop the close that
the whole switch exists to perform, so each call is awaited independently, failures are
counted, and the confirmation says what actually happened. A kill that reports success it
did not achieve is worse than one that failed loudly.

**The budget is enforced, not assumed.** A venue that accepted the request and never
answers is the case the five seconds exist for, and an untimed call promises nothing, so
every venue call runs under a deadline taken from `risk.kill_budget_s`. A call that
overruns is abandoned, named by cloid or coin in the confirmation, and the next step runs
anyway. Cancels get at most half the budget so a hung cancel cannot eat the closes' share:
a resting order that survives a kill is harmless next to a position that does.

A second `/kill` while one is in flight joins it rather than queueing behind it. The
caller is someone who typed it twice, and answering them with a
second full sweep that starts when the first ends is two budgets for one question.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from liqfade.control.backend import kill_not_persisted, write_kill_file
from liqfade.control.protocol import KillResult
from liqfade.log import get_logger

log = get_logger(__name__)

__all__ = ["KillSwitch", "KillWatcher"]

DEFAULT_POLL_S = 1.0  # the flag is checked this often; the budget is 5s and this leaves room
# Seconds the whole sweep gets. Under `risk.kill_deadline_s`, which is the promise, so the
# confirmation still lands inside it after the alert and the logging.
DEFAULT_BUDGET_S = 4.0
# Abandoned targets named in the confirmation before it says "and N more". The count is
# the number that matters at a glance; a few cloids are what make it actionable. The full
# list goes in the log line, where a machine reads it.
NAMED_IN_CONFIRMATION = 5


class KillSwitch:
    """Fires once, from any trigger, and reports exactly what it managed to do."""

    def __init__(
        self,
        *,
        kill_file: Path,
        open_orders: Callable[[], Awaitable[Sequence[str]]],
        cancel_all: Callable[[str], Awaitable[None]],
        open_positions: Callable[[], Awaitable[Mapping[str, float]]],
        close_position: Callable[[str, float], Awaitable[None]],
        alert: Callable[[str], Awaitable[bool]] | None = None,
        stop: asyncio.Event | None = None,
        budget_s: float = DEFAULT_BUDGET_S,
    ) -> None:
        """
        Args:
            kill_file: the flag. Written first, and never removed by this class.
            open_orders: cloids of everything that could still fill.
            cancel_all: cancel one cloid.
            open_positions: coin to signed size, positive long.
            close_position: close one coin's signed size with an IOC reduce-only order.
            alert: Telegram. Optional, so the switch works with Telegram down.
            stop: the process shutdown event, set after the flattening, so the loop stops
                only once there is nothing left for it to manage.
            budget_s: seconds the whole sweep gets, from `risk.kill_budget_s`. Every venue
                call runs under a deadline drawn from it.
        """
        self.kill_file = Path(kill_file)
        self._open_orders = open_orders
        self._cancel = cancel_all
        self._open_positions = open_positions
        self._close = close_position
        self._alert = alert
        self._stop = stop
        self.budget_s = budget_s
        self.fired = False
        self._inflight: asyncio.Task[KillResult] | None = None

    async def fire(self, reason: str | None = None) -> KillResult:
        """Kill. Safe to call repeatedly; a later call re-runs the sweep and reports.

        Deliberately not a no-op once fired: an order that arrived between the first kill
        and the second is exactly what a second `/kill` is for. A call made *while* a kill
        is still running joins that one instead, and both callers get its answer.
        """
        inflight = self._inflight
        if inflight is not None and not inflight.done():
            log.warning("kill_joined", reason=reason)
            return await asyncio.shield(inflight)
        task = asyncio.get_running_loop().create_task(self._sweep(reason))
        self._inflight = task
        # Shielded so a cancelled caller does not cancel the kill. Whatever brought the
        # caller down is the reason the kill has to finish.
        return await asyncio.shield(task)

    async def _sweep(self, reason: str | None) -> KillResult:
        started = time.monotonic()
        written = write_kill_file(self.kill_file, reason)
        self.fired = True

        # Cancels get half, closes get whatever is left of the whole budget, which is at
        # least the other half. A hung cancel therefore cannot spend the closes' share.
        cancelled, cancel_failures, stuck_cancels = await self._cancel_everything(
            started + self.budget_s / 2.0
        )
        closed, close_failures, stuck_closes = await self._close_everything(started + self.budget_s)

        if self._stop is not None:
            self._stop.set()

        parts = [reason] if reason else []
        if cancel_failures:
            parts.append(f"{cancel_failures} cancel(s) failed")
        if stuck_cancels:
            parts.append(f"cancel abandoned on {_some(stuck_cancels)}")
        if close_failures:
            parts.append(f"{close_failures} close(s) failed")
        if stuck_closes:
            parts.append(f"close abandoned on {_some(stuck_closes)}")
        detail = "; ".join(parts) or None
        if not written:
            detail = kill_not_persisted(self.kill_file, detail)

        log.warning(
            "kill_fired",
            reason=reason,
            flag_written=written,
            canceled=cancelled,
            closed=closed,
            cancel_failures=cancel_failures,
            close_failures=close_failures,
            abandoned=[*stuck_cancels, *stuck_closes],
            elapsed_s=round(time.monotonic() - started, 3),
            budget_s=self.budget_s,
        )
        result = KillResult(
            killed=written,
            canceled_orders=cancelled,
            closed_positions=closed,
            reason=detail,
        )
        await self._confirm(result)
        return result

    async def _cancel_everything(self, deadline: float) -> tuple[int, int, list[str]]:
        try:
            cloids = list(await _before(self._open_orders(), deadline))
        except TimeoutError:
            log.error(
                "kill_open_orders_timed_out", deadline_in_s=round(deadline - time.monotonic(), 3)
            )
            return 0, 1, ["openOrders"]
        except Exception as exc:
            log.error("kill_open_orders_failed", error=f"{type(exc).__name__}: {exc}")
            return 0, 1, []
        if not cloids:
            return 0, 0, []
        outcomes = await asyncio.gather(
            *(_before(self._cancel(cloid), deadline) for cloid in cloids),
            return_exceptions=True,
        )
        return _count(outcomes, "kill_cancel_failed", cloids)

    async def _close_everything(self, deadline: float) -> tuple[int, int, list[str]]:
        try:
            positions = dict(await _before(self._open_positions(), deadline))
        except TimeoutError:
            log.error("kill_open_positions_timed_out")
            return 0, 1, ["clearinghouseState"]
        except Exception as exc:
            log.error("kill_open_positions_failed", error=f"{type(exc).__name__}: {exc}")
            return 0, 1, []
        if not positions:
            return 0, 0, []
        coins = list(positions)
        outcomes = await asyncio.gather(
            *(_before(self._close(coin, positions[coin]), deadline) for coin in coins),
            return_exceptions=True,
        )
        return _count(outcomes, "kill_close_failed", coins)

    async def _confirm(self, result: KillResult) -> None:
        if self._alert is None:
            return
        text = (
            f"KILL fired. Cancelled {result.canceled_orders} order(s), "
            f"closed {result.closed_positions} position(s). "
            f"{'Flag written.' if result.killed else 'FLAG NOT WRITTEN.'}"
        )
        if result.reason:
            text += f" {result.reason}"
        with contextlib.suppress(Exception):
            # Telegram being down cannot be allowed to raise into a kill that worked.
            await self._alert(text)


def _some(targets: Sequence[str]) -> str:
    """A few of them by name, then how many more. See `NAMED_IN_CONFIRMATION`."""
    named = ", ".join(targets[:NAMED_IN_CONFIRMATION])
    extra = len(targets) - NAMED_IN_CONFIRMATION
    return f"{named} and {extra} more" if extra > 0 else named


async def _before(awaitable: Awaitable[Any], deadline: float) -> Any:
    """Await `awaitable`, abandoning it at `deadline`. Raises TimeoutError on overrun.

    `wait_for` cancels what it abandons, which for an SDK call already running on a
    thread releases us without stopping the thread. That is the intended meaning of
    abandoned: we stop waiting, we say so, and we move on to the next step.
    """
    return await asyncio.wait_for(awaitable, timeout=max(0.0, deadline - time.monotonic()))


def _count(
    outcomes: Sequence[object], event: str, labels: Sequence[str]
) -> tuple[int, int, list[str]]:
    """Successes, failures, and the labels of the calls that ran out of budget."""
    done = failures = 0
    abandoned: list[str] = []
    for label, outcome in zip(labels, outcomes, strict=True):
        if isinstance(outcome, TimeoutError):
            failures += 1
            abandoned.append(label)
            log.error(event, target=label, error="ran out of budget, abandoned")
        elif isinstance(outcome, BaseException):
            failures += 1
            log.error(event, target=label, error=f"{type(outcome).__name__}: {outcome}")
        else:
            done += 1
    return done, failures, abandoned


class KillWatcher:
    """Polls for the kill flag and fires the switch when it appears.

    Polling rather than inotify: one `stat` a second costs nothing, works over every
    filesystem the data dir might live on, and has no watch to lose on a remount. The
    switch itself is idempotent, so a flag that is still there next second changes nothing.
    """

    def __init__(self, switch: KillSwitch, *, poll_s: float = DEFAULT_POLL_S) -> None:
        self.switch = switch
        self.poll_s = poll_s

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if not self.switch.fired and self._present():
                log.warning("kill_file_seen", path=str(self.switch.kill_file))
                await self.switch.fire(reason=f"kill file at {self.switch.kill_file}")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.poll_s)

    def _present(self) -> bool:
        """True when the flag is there, and true when we cannot tell.

        Same rule as `Settings.kill_switch_present`: the kill switch fails closed, so an
        unreadable data dir counts as killed rather than as clear.
        """
        try:
            self.switch.kill_file.stat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return True
