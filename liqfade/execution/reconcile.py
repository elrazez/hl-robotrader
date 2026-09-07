"""Comparing our book against the venue's, on startup, on reconnect and every 60s.

**Nothing here corrects anything.** `reconcile` is a pure function of two snapshots and
returns what it found; the caller decides what to do and logs the decision. That split is
the point: a reconciler that quietly cancelled and adopted would make the two books agree
while hiding the bug that made them differ, and the bug is the thing worth knowing about.
CLAUDE.md rule 4 asks for reconciliation, and docs/HYPERLIQUID.md says the bot never
adopts a position it did not open.

Each difference carries a `Severity`, which is a recommendation and not an action:

- `CANCEL` for an order resting at the venue that we have no record of. Cancelling it is
  what docs/HYPERLIQUID.md prescribes, and the caller does it explicitly and logs it.
- `INVESTIGATE` for the order level disagreements that a missed `orderUpdates` explains:
  an order gone from the venue, or one whose remaining size shrank. Both mean we probably
  missed a fill, which the fills query settles.
- `HALT` for anything about a position. A position we did not open, one that vanished, or
  one whose size or side does not match is a difference in the thing that carries the
  risk, and no automatic action on it is safe.

Sizes are signed, positive long and negative short, so a side flip is a size difference
rather than a special case: the two books disagreeing about direction is the same finding
as disagreeing about magnitude, and the worst one there is.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Protocol

from liqfade.execution.hl import AccountSnapshot, VenueOrder, VenuePosition
from liqfade.execution.orders import LIVE, Order
from liqfade.log import get_logger

log = get_logger(__name__)

__all__ = ["BookReader", "Difference", "Reconciler", "Reconciliation", "Severity", "reconcile"]


class BookReader(Protocol):
    """The second opinion. `InfoReader` is one; the paper loop's own engine is the other.

    Narrow on purpose: the reconciler needs two questions answered and nothing else, so
    paper can answer them from the simulated book without an account at the venue.
    """

    async def account(self, address: str) -> AccountSnapshot: ...

    async def open_orders(self, address: str) -> Sequence[VenueOrder]: ...


# Positions are compared with a tolerance: szDecimals rounding, and the venue's own
# arithmetic, make the last digit disagree on a book that is perfectly correct.
SIZE_TOLERANCE = 1e-6


class Severity(IntEnum):
    """Ordered, so `max` picks the worst finding without a lookup table."""

    INVESTIGATE = 1
    CANCEL = 2
    HALT = 3


@dataclass(frozen=True, slots=True)
class Difference:
    """One disagreement, in the words that go into the log line and the Telegram message."""

    kind: str
    severity: Severity
    detail: str
    coin: str | None = None
    cloid: str | None = None
    oid: int | None = None
    ours: float | None = None
    theirs: float | None = None


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """What one comparison found. Empty is the normal answer and the one worth counting."""

    differences: list[Difference] = field(default_factory=list)
    checked_orders: int = 0
    checked_positions: int = 0

    @property
    def clean(self) -> bool:
        return not self.differences

    @property
    def worst(self) -> Severity | None:
        return max((d.severity for d in self.differences), default=None)

    def summary(self) -> str:
        """One line naming every kind found, for Telegram. Empty string when clean."""
        if self.clean:
            return ""
        counts = Counter(d.kind for d in self.differences)
        parts = ", ".join(f"{kind} x{count}" for kind, count in sorted(counts.items()))
        return f"reconcile: {parts}"


def reconcile(
    *,
    our_orders: Sequence[Order],
    venue_orders: Sequence[VenueOrder],
    our_positions: Mapping[str, float],
    venue_positions: Mapping[str, VenuePosition],
) -> Reconciliation:
    """Compare the two books and report every disagreement.

    Args:
        our_orders: the registry's orders. Only the live ones are expected at the venue.
        venue_orders: `openOrders`, parsed.
        our_positions: coin to signed size, positive long, from our own fills.
        venue_positions: `clearinghouseState`, parsed.

    Returns:
        A `Reconciliation`. `clean` is true when the two books agree.
    """
    differences: list[Difference] = []
    ours_live = {order.cloid: order for order in our_orders if order.status in LIVE}
    theirs_by_cloid = {order.cloid: order for order in venue_orders if order.cloid}

    for venue_order in venue_orders:
        if venue_order.cloid and venue_order.cloid in ours_live:
            continue
        differences.append(
            Difference(
                kind="unknown_order",
                severity=Severity.CANCEL,
                coin=venue_order.coin,
                oid=venue_order.oid,
                cloid=venue_order.cloid,
                theirs=venue_order.size,
                detail=(
                    f"{venue_order.coin} oid {venue_order.oid} rests at the venue for "
                    f"{venue_order.size} and is not in our registry"
                    + (f" (cloid {venue_order.cloid})" if venue_order.cloid else " (no cloid)")
                ),
            )
        )

    for cloid, order in ours_live.items():
        theirs = theirs_by_cloid.get(cloid)
        if theirs is None:
            differences.append(
                Difference(
                    kind="missing_order",
                    severity=Severity.INVESTIGATE,
                    coin=order.coin,
                    cloid=cloid,
                    oid=order.oid,
                    ours=order.remaining,
                    detail=(
                        f"{order.coin} {cloid} is {order.status} for us with "
                        f"{order.remaining} left, and the venue has no such order"
                    ),
                )
            )
            continue
        if abs(theirs.size - order.remaining) > SIZE_TOLERANCE:
            differences.append(
                Difference(
                    kind="order_size",
                    severity=Severity.INVESTIGATE,
                    coin=order.coin,
                    cloid=cloid,
                    oid=order.oid,
                    ours=order.remaining,
                    theirs=theirs.size,
                    detail=(
                        f"{order.coin} {cloid} has {order.remaining} left for us and "
                        f"{theirs.size} at the venue, which is a fill we did not see"
                    ),
                )
            )

    coins = sorted(set(our_positions) | set(venue_positions))
    for coin in coins:
        ours = our_positions.get(coin, 0.0)
        theirs_position = venue_positions.get(coin)
        theirs_size = theirs_position.size if theirs_position else 0.0
        if abs(ours - theirs_size) <= SIZE_TOLERANCE:
            continue
        if abs(ours) <= SIZE_TOLERANCE:
            kind, detail = (
                "unknown_position",
                f"the venue holds {theirs_size} {coin} and we hold none. "
                f"The bot does not adopt a position it did not open.",
            )
        elif abs(theirs_size) <= SIZE_TOLERANCE:
            kind, detail = (
                "missing_position",
                f"we hold {ours} {coin} and the venue holds none",
            )
        else:
            kind, detail = (
                "position_size",
                f"{coin}: we hold {ours}, the venue holds {theirs_size}",
            )
        differences.append(
            Difference(
                kind=kind,
                severity=Severity.HALT,
                coin=coin,
                ours=ours,
                theirs=theirs_size,
                detail=detail,
            )
        )

    return Reconciliation(
        differences=differences,
        checked_orders=len(ours_live) + len(venue_orders),
        checked_positions=len(coins),
    )


class Reconciler:
    """Runs `reconcile` on startup, on every reconnect and every `execution.reconcile_s`.

    It queries, compares, logs one line per difference and posts one message per run that
    found something. It does not cancel and does not halt: it hands the `Reconciliation`
    back and the caller acts, so the decision to cancel a stranger's order or to halt on
    an unknown position is always something a human can find in the log as a decision.

    A failed query is logged and the run is skipped rather than reported as clean. An
    empty answer from a timed out request looks exactly like a flat account, and "the
    venue holds nothing" is the one wrong conclusion that would flatten nothing and halt
    nothing while the account carries a position.
    """

    def __init__(
        self,
        *,
        reader: BookReader,
        address: str,
        orders: Callable[[], Awaitable[Sequence[Order]]],
        positions: Callable[[], Mapping[str, float]] = dict,
        interval_s: float = 60.0,
        alert: Callable[[str], Awaitable[bool]] | None = None,
        compare_positions: bool = True,
    ) -> None:
        """
        Args:
            reader: the second opinion. `InfoReader` against the venue, the engine's own
                working orders in paper.
            address: the main wallet, passed to the reader. Ignored by a paper reader.
            orders: our registry's orders.
            positions: our positions, coin to signed size.
            interval_s: seconds between checks, from `execution.reconcile_s`.
            alert: Telegram. Optional.
            compare_positions: False in paper, where there is no venue account and so no
                second source for a position. Comparing against nothing would report every
                open position as one the venue lost, which is a HALT, every interval. Not
                checked is the honest answer, and `checked_positions` then says zero.
        """
        self.reader = reader
        self.address = address
        self._orders = orders
        self._positions = positions
        self.interval_s = interval_s
        self._alert = alert
        self.compare_positions = compare_positions
        self.runs = 0
        self.failures = 0
        self.differences = 0
        self.last: Reconciliation | None = None

    async def check(self, trigger: str) -> Reconciliation | None:
        """One comparison. Returns None when the venue could not be read."""
        try:
            venue_orders = await self.reader.open_orders(self.address)
            account = await self.reader.account(self.address) if self.compare_positions else None
        except Exception as exc:
            self.failures += 1
            log.error(
                "reconcile_failed",
                trigger=trigger,
                error=f"{type(exc).__name__}: {exc}",
                failures=self.failures,
            )
            return None

        result = reconcile(
            our_orders=await self._orders(),
            venue_orders=venue_orders,
            our_positions=self._positions() if account is not None else {},
            venue_positions=account.positions if account is not None else {},
        )
        self.runs += 1
        self.differences += len(result.differences)
        self.last = result

        if result.clean:
            log.info(
                "reconcile_clean",
                trigger=trigger,
                orders=result.checked_orders,
                positions=result.checked_positions,
                equity=account.equity if account is not None else None,
            )
            return result

        for difference in result.differences:
            log.warning(
                "reconcile_difference",
                trigger=trigger,
                kind=difference.kind,
                severity=difference.severity.name,
                coin=difference.coin,
                cloid=difference.cloid,
                oid=difference.oid,
                ours=difference.ours,
                theirs=difference.theirs,
                detail=difference.detail,
            )
        if self._alert is not None:
            lines = [f"RECONCILE {trigger}: {result.summary()}"]
            lines += [f"- {d.severity.name}: {d.detail}" for d in result.differences[:10]]
            await self._alert("\n".join(lines))
        return result

    async def run(
        self,
        stop: asyncio.Event,
        *,
        reconnected: asyncio.Event | None = None,
    ) -> None:
        """Check now, then on every reconnect and every `interval_s` until `stop`."""
        await self.check("startup")
        while not stop.is_set():
            waiters = [asyncio.ensure_future(stop.wait())]
            if reconnected is not None:
                waiters.append(asyncio.ensure_future(reconnected.wait()))
            _done, pending = await asyncio.wait(
                waiters, timeout=self.interval_s, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            if stop.is_set():
                return
            trigger = "interval"
            if reconnected is not None and reconnected.is_set():
                reconnected.clear()
                trigger = "reconnect"
            await self.check(trigger)
