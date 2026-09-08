"""The per-cloid order state machine, its sqlite registry, and idempotent submit.

`NEW -> SUBMITTED -> ACKED -> (PARTIAL)* -> FILLED | CANCELED | REJECTED`, from
docs/HYPERLIQUID.md. Every transition is written to `data/orders.sqlite` before it is
believed, so a process that dies between a submit and its ack restarts knowing an order
of that cloid is outstanding rather than placing a second one.

**The cloid is the identity, not the oid.** We choose it, so it exists before the venue
has heard of the order, which is the only thing that makes a submit idempotent: a retry
after a lost response carries the same cloid and the venue can recognise it. The oid is
recorded when it arrives and is used for nothing that the cloid can do.

**Retries.** Transport failures (a reset connection, a timeout, a 5xx) are retried with
jittered backoff up to `retries`, because the order may or may not have landed and the
same cloid is the safe way to find out. A `Reject` is never retried at all: the venue
understood the order and refused it, and sending it again just refuses it again while
burning address action budget. Those are the two cases; anything the transport cannot
classify is a `Reject`, because failing closed on an ambiguous refusal is cheaper than
looping on it.

**The venue is the truth.** `record_fill` accepts a fill on a cancelled order and moves it
back to PARTIAL, because a cancel that raced a fill did not stop the fill and pretending
otherwise means the position book and the venue disagree in the direction that hurts.

**The kill flag is re-checked at submit, not trusted from startup.** `load_signer`
refuses to build a signer while the flag is there, and a kill can land in the seconds
between that and an order. CLAUDE.md rule 5 says the flag wins, so `submit` asks again and
records a refusal rather than placing.

**Rounding happens here and nowhere else.** The strategy works in floats and does not
know an asset's `szDecimals`; `submit` is the last thing to touch a price before the wire,
so it is the only thing that rounds one, through `execution/rounding.py`. The rounded
values replace the asked-for ones on the intent, so the registry records what was actually
sent rather than what we wanted, and `wire_px`/`wire_size` carry the exact Decimal-derived
spelling for the transport. A coin with no `szDecimals` is refused rather than rounded on
a guessed grid, and so is a size that rounds away to zero.

Nothing here talks to Hyperliquid. `OrderTransport` is the seam, so the same manager runs
against the venue in live and testnet and against the backtest fill model in paper, and
paper orders get the same cloid, the same state machine and the same persistence.
"""

from __future__ import annotations

import asyncio
import random
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, Self

import aiosqlite

from liqfade.control.protocol import OrderState, now_ms
from liqfade.execution.ratelimit import ActionBudget, BudgetExhaustedError
from liqfade.execution.rounding import RoundingError, round_price, round_size
from liqfade.execution.tpsl import TpslLegs, tpsl_requests
from liqfade.log import get_logger

log = get_logger(__name__)

__all__ = [
    "LIVE",
    "Order",
    "OrderIntent",
    "OrderManager",
    "OrderStatus",
    "OrderStore",
    "OrderTransport",
    "RejectError",
    "TransientTransportError",
    "new_cloid",
]

CLOID_BYTES = 16  # docs/HYPERLIQUID.md: the `c` field is 16 bytes of hex


class OrderStatus(StrEnum):
    """Persisted as its own string, so a sqlite file stays readable without the code."""

    NEW = "NEW"
    SUBMITTED = "SUBMITTED"
    ACKED = "ACKED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


TERMINAL = frozenset({OrderStatus.FILLED, OrderStatus.REJECTED})
# What "open" means to reconciliation and to /status: anything the venue could still act
# on. CANCELED is not here, but a fill on a cancelled order still moves it back to PARTIAL.
LIVE = frozenset({OrderStatus.NEW, OrderStatus.SUBMITTED, OrderStatus.ACKED, OrderStatus.PARTIAL})


def new_cloid() -> str:
    """A fresh client order id: `0x` and 16 bytes of hex, unique per order."""
    return "0x" + secrets.token_hex(CLOID_BYTES)


class TransientTransportError(RuntimeError):
    """The request may or may not have reached the venue. Retry with the same cloid."""


class RejectError(RuntimeError):
    """The venue understood the order and refused it. Never retried."""


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """What to place. Immutable, so a retry sends the same thing it sent the first time.

    Args:
        cloid: our client order id, chosen before the venue exists to us.
        coin: the perp symbol.
        is_buy: direction on the wire, not the strategy's side.
        px: limit price, already rounded by `execution.rounding`.
        size: size in coin units, already rounded to szDecimals.
        tif: `Alo`, `Gtc` or `Ioc`, or the trigger description for a stop.
        reduce_only: true for every exit leg.
        tag: `entry`, `target`, `stop` or `time_stop`, carried into the trade log.
        trigger_px: set for a trigger order, None for a plain limit.
        tpsl: `tp` or `sl` when this is a trigger order.
        wire_px: the rounded price as the venue spells it, set by `OrderManager.submit`.
            Empty until then, and empty on an intent read back from the registry, because
            a persisted order is never submitted a second time.
        wire_size: the same for the size.
    """

    cloid: str
    coin: str
    is_buy: bool
    px: float
    size: float
    tif: str
    reduce_only: bool = False
    tag: str = "entry"
    trigger_px: float | None = None
    tpsl: str | None = None
    wire_px: str = ""
    wire_size: str = ""

    @property
    def wire(self) -> tuple[str, str]:
        """Price and size as the venue spells them. Raises if nothing rounded them.

        A transport calls this rather than reading `px` and `size`, so an intent that
        reached the wire without going through `OrderManager.submit` is a loud failure
        rather than an order on the wrong grid.
        """
        if not self.wire_px or not self.wire_size:
            raise RejectError(
                f"{self.cloid} was not rounded: OrderManager.submit is the only door to a venue"
            )
        return self.wire_px, self.wire_size


@dataclass(slots=True)
class Order:
    """One order and everything known about it. Mutable; the store holds the truth."""

    intent: OrderIntent
    status: OrderStatus = OrderStatus.NEW
    oid: int | None = None
    filled: float = 0.0
    avg_px: float = 0.0
    fees: float = 0.0
    # What the venue said this order realised, summed over its fills. In memory only: it
    # is read once, by whoever books the round trip, and the registry's job is which
    # orders exist rather than what they earned.
    closed_pnl: float = 0.0
    reject_reason: str | None = None
    submitted_ms: int = field(default_factory=now_ms)
    updated_ms: int = field(default_factory=now_ms)
    # How many times this entry has been moved, and when its current price became
    # visible. `updated_ms` cannot serve as the clock: a partial fill touches it, which
    # would hand a half filled order a fresh TTL it has not earned.
    reprices: int = 0
    priced_ms: int = field(default_factory=now_ms)

    @property
    def cloid(self) -> str:
        return self.intent.cloid

    @property
    def coin(self) -> str:
        return self.intent.coin

    @property
    def remaining(self) -> float:
        return max(self.intent.size - self.filled, 0.0)

    def to_state(self) -> OrderState:
        """The control plane's view, for `status` and the panel."""
        return OrderState(
            cloid=self.cloid,
            oid=self.oid,
            coin=self.coin,
            side="buy" if self.intent.is_buy else "sell",
            px=self.intent.px,
            size=self.intent.size,
            filled=self.filled,
            tif=self.intent.tif,
            reduce_only=self.intent.reduce_only,
            status=str(self.status),
            submitted_ms=self.submitted_ms,
        )


class OrderTransport(Protocol):
    """What the manager needs from a venue. Two implementations: Hyperliquid and paper.

    `place` returns the exchange order id. It raises `Reject` for a refusal and
    `TransientTransportError` for anything that might have landed.
    """

    async def place(self, intent: OrderIntent) -> int: ...

    async def cancel(self, coin: str, cloid: str) -> None: ...

    async def modify(self, intent: OrderIntent) -> int: ...

    async def place_group(self, requests: list[dict[str, Any]]) -> list[str]: ...


SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    cloid       TEXT PRIMARY KEY,
    coin        TEXT NOT NULL,
    is_buy      INTEGER NOT NULL,
    px          REAL NOT NULL,
    size        REAL NOT NULL,
    tif         TEXT NOT NULL,
    reduce_only INTEGER NOT NULL,
    tag         TEXT NOT NULL,
    trigger_px  REAL,
    tpsl        TEXT,
    status      TEXT NOT NULL,
    oid         INTEGER,
    filled      REAL NOT NULL DEFAULT 0,
    avg_px      REAL NOT NULL DEFAULT 0,
    fees        REAL NOT NULL DEFAULT 0,
    reject_reason TEXT,
    submitted_ms INTEGER NOT NULL,
    updated_ms   INTEGER NOT NULL,
    reprices     INTEGER NOT NULL DEFAULT 0,
    priced_ms    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS orders_status ON orders(status);
CREATE TABLE IF NOT EXISTS fills (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    cloid  TEXT NOT NULL,
    ts_ms  INTEGER NOT NULL,
    px     REAL NOT NULL,
    size   REAL NOT NULL,
    fee    REAL NOT NULL DEFAULT 0,
    maker  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS fills_cloid ON fills(cloid);
"""


class OrderStore:
    """Every order and fill, on disk. The registry that survives a restart.

    WAL, because the panel reads this file while the trading loop writes it and the
    default rollback journal makes a reader block a writer.
    """

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    @classmethod
    async def open(cls, path: Path | str) -> Self:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=FULL")
        await db.executescript(SCHEMA)
        # CREATE TABLE IF NOT EXISTS does nothing to a table that already exists, so a
        # registry written by an earlier version keeps its old columns and every read of
        # a new one raises. The box has one of those, holding real orders.
        async with db.execute("PRAGMA table_info(orders)") as cursor:
            columns = {row["name"] for row in await cursor.fetchall()}
        for name, ddl in (
            ("reprices", "INTEGER NOT NULL DEFAULT 0"),
            ("priced_ms", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in columns:
                await db.execute(f"ALTER TABLE orders ADD COLUMN {name} {ddl}")
                log.info("order_store_migrated", column=name)
        await db.commit()
        return cls(db)

    async def close(self) -> None:
        await self._db.close()

    async def upsert(self, order: Order) -> None:
        """Write one order's whole row. Called on every transition, before it is believed."""
        intent = order.intent
        await self._db.execute(
            """
            INSERT INTO orders (cloid, coin, is_buy, px, size, tif, reduce_only, tag,
                                trigger_px, tpsl, status, oid, filled, avg_px, fees,
                                reject_reason, submitted_ms, updated_ms, reprices, priced_ms)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(cloid) DO UPDATE SET
                status=excluded.status, oid=excluded.oid, filled=excluded.filled,
                avg_px=excluded.avg_px, fees=excluded.fees,
                reject_reason=excluded.reject_reason, updated_ms=excluded.updated_ms,
                -- px moves on a reprice, and the count and the clock move with it. Every
                -- other column of the intent is still immutable.
                px=excluded.px, reprices=excluded.reprices, priced_ms=excluded.priced_ms
            """,
            (
                intent.cloid,
                intent.coin,
                int(intent.is_buy),
                intent.px,
                intent.size,
                intent.tif,
                int(intent.reduce_only),
                intent.tag,
                intent.trigger_px,
                intent.tpsl,
                str(order.status),
                order.oid,
                order.filled,
                order.avg_px,
                order.fees,
                order.reject_reason,
                order.submitted_ms,
                order.updated_ms,
                order.reprices,
                order.priced_ms,
            ),
        )
        await self._db.commit()

    async def add_fill(
        self, cloid: str, *, ts_ms: int, px: float, size: float, fee: float, maker: bool
    ) -> None:
        await self._db.execute(
            "INSERT INTO fills (cloid, ts_ms, px, size, fee, maker) VALUES (?,?,?,?,?,?)",
            (cloid, ts_ms, px, size, fee, int(maker)),
        )
        await self._db.commit()

    async def get(self, cloid: str) -> Order:
        async with self._db.execute("SELECT * FROM orders WHERE cloid = ?", (cloid,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise KeyError(cloid)
        return _order_from_row(row)

    async def open_orders(self) -> list[Order]:
        """Everything the venue could still act on, oldest first."""
        # The interpolated part is a run of "?" and nothing else; the statuses go in
        # as bound parameters below.
        placeholders = ",".join("?" for _ in LIVE)
        async with self._db.execute(
            f"SELECT * FROM orders WHERE status IN ({placeholders}) ORDER BY submitted_ms",  # noqa: S608
            tuple(sorted(str(s) for s in LIVE)),
        ) as cursor:
            return [_order_from_row(row) for row in await cursor.fetchall()]

    async def all_orders(self) -> list[Order]:
        async with self._db.execute("SELECT * FROM orders ORDER BY submitted_ms") as cursor:
            return [_order_from_row(row) for row in await cursor.fetchall()]

    async def fills(self, cloid: str | None = None) -> list[aiosqlite.Row]:
        query = "SELECT * FROM fills"
        args: tuple[str, ...] = ()
        if cloid is not None:
            query += " WHERE cloid = ?"
            args = (cloid,)
        async with self._db.execute(query + " ORDER BY id", args) as cursor:
            return list(await cursor.fetchall())


def _order_from_row(row: aiosqlite.Row) -> Order:
    return Order(
        intent=OrderIntent(
            cloid=row["cloid"],
            coin=row["coin"],
            is_buy=bool(row["is_buy"]),
            px=row["px"],
            size=row["size"],
            tif=row["tif"],
            reduce_only=bool(row["reduce_only"]),
            tag=row["tag"],
            trigger_px=row["trigger_px"],
            tpsl=row["tpsl"],
        ),
        status=OrderStatus(row["status"]),
        oid=row["oid"],
        filled=row["filled"],
        avg_px=row["avg_px"],
        fees=row["fees"],
        reject_reason=row["reject_reason"],
        submitted_ms=row["submitted_ms"],
        updated_ms=row["updated_ms"],
        reprices=row["reprices"],
        # A row written before this column existed reads 0, which would hand an old order
        # an infinitely fresh price. Fall back to when it was submitted.
        priced_ms=row["priced_ms"] or row["submitted_ms"],
    )


# What the venue says when a modify lost its race. docs/HYPERLIQUID.md probe 7.
ALREADY_GONE = "cannot modify canceled or filled order"


def _already_gone(exc: BaseException) -> bool:
    """Did this refusal mean the order is finished rather than that the request was bad?"""
    return ALREADY_GONE in str(exc).lower()


class OrderManager:
    """Submits, cancels and tracks. One per process, holding the only writable registry."""

    def __init__(
        self,
        store: OrderStore,
        transport: OrderTransport,
        *,
        sz_decimals: Mapping[str, int],
        killed: Callable[[], bool],
        retries: int = 3,
        backoff_s: float = 0.25,
        budget: ActionBudget | None = None,
        on_ack_ms: Callable[[float], None] | None = None,
    ) -> None:
        """
        Args:
            store: the sqlite registry, already opened.
            transport: the venue, or the paper simulator.
            sz_decimals: coin to `meta.universe[i].szDecimals`, which is what both
                rounding rules are computed from. A coin missing from it is refused: a
                guessed grid is how a bug becomes an order.
            killed: True when the kill flag is present. Asked on every submit, never
                cached, because the flag can appear between one order and the next.
                `Settings.kill_switch_present` is what the loop passes, and it fails
                closed on an unreadable data dir.
            retries: retries after the first attempt, on transient errors only. From
                `execution.transient_retries`.
            backoff_s: base delay; each retry waits `backoff_s * 2**attempt` times a
                random factor in [0.5, 1.5). Jittered because two coins rejected by the
                same rate limit would otherwise retry in lockstep forever.
            on_ack_ms: called with each order's submit-to-ack milliseconds. The panel
                plots the series; `submit_ack_p50` and `p95` were fields nothing fed.
            budget: the address action budget from `execution.action_budget_per_hour`.
                None counts nothing, which is what the backtest and the paper fill model
                want: neither sends an action to an address.
        """
        self.store = store
        self.transport = transport
        self.sz_decimals = dict(sz_decimals)
        self.killed = killed
        self.retries = retries
        self.backoff_s = backoff_s
        self.budget = budget
        self._on_ack_ms = on_ack_ms
        self._orders: dict[str, Order] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def actions_remaining(self) -> int | None:
        """What `RiskState.actions_remaining` reports, or None when nothing is counted."""
        return None if self.budget is None else self.budget.remaining(now_ms=now_ms())

    def _spend(self, *, reducing: bool) -> None:
        """Take one action from the budget. Raises `BudgetExhaustedError` when it adds risk."""
        if self.budget is not None:
            self.budget.take(now_ms=now_ms(), reducing=reducing)

    async def load(self) -> list[Order]:
        """Read the registry back into memory. Called on startup, before anything trades."""
        orders = await self.store.all_orders()
        self._orders = {order.cloid: order for order in orders}
        live = [order for order in orders if order.status in LIVE]
        log.info("order_registry_loaded", orders=len(orders), live=len(live))
        return live

    def get(self, cloid: str) -> Order:
        return self._orders[cloid]

    async def open_orders(self) -> list[Order]:
        return [order for order in self._orders.values() if order.status in LIVE]

    async def submit(self, intent: OrderIntent) -> Order:
        """Place `intent`, or return the order already placed under that cloid.

        Idempotent by cloid, under a per-cloid lock so two tasks racing on one intent
        place once rather than twice: without the lock the check and the place are two
        steps and the second task passes the check while the first is still in flight.

        Returns:
            The order, ACKED on success or REJECTED when the venue refused it, when the
            kill flag is on, or when it could not be rounded onto the venue's grid.

        Raises:
            TransientTransportError: every retry failed. The order may still be resting;
                reconciliation is what settles that, which is why this is raised rather
                than swallowed.
        """
        lock = self._locks.setdefault(intent.cloid, asyncio.Lock())
        async with lock:
            existing = self._orders.get(intent.cloid)
            if existing is not None:
                log.info(
                    "order_submit_deduped",
                    cloid=intent.cloid,
                    coin=existing.coin,
                    status=str(existing.status),
                )
                return existing

            # A reduce-only order cannot open or increase a position, so the flag has
            # nothing to protect against. It must go through, because the kill switch
            # writes the flag and *then* sends the reduce-only Ioc that flattens: drilled
            # on testnet, the kill reported "closed 1" and its own gate had refused the
            # order, leaving the position open. Same principle as the action budget: risk
            # going down is never refused.
            if self.killed() and not intent.reduce_only:
                reason = "kill switch is on; refusing to place"
                order = Order(intent=intent, status=OrderStatus.REJECTED, reject_reason=reason)
                self._orders[intent.cloid] = order
                await self._persist(order, "order_rejected", reason=reason, source="kill_switch")
                return order

            try:
                intent = self._rounded(intent)
            except RoundingError as exc:
                order = Order(intent=intent, status=OrderStatus.REJECTED, reject_reason=str(exc))
                self._orders[intent.cloid] = order
                await self._persist(order, "order_rejected", reason=str(exc), source="rounding")
                return order

            order = Order(intent=intent, status=OrderStatus.SUBMITTED)
            self._orders[intent.cloid] = order
            await self._persist(
                order,
                "order_submitted",
                px=intent.px,
                size=intent.size,
                tif=intent.tif,
                reduce_only=intent.reduce_only,
                tag=intent.tag,
            )
            try:
                self._spend(reducing=intent.reduce_only)
            except BudgetExhaustedError as exc:
                # Refused here rather than at the venue, because a rejected order costs an
                # action there too: hitting the real limit is how an account loses the
                # ability to cancel.
                order.reject_reason = str(exc)
                await self._transition(
                    order, OrderStatus.REJECTED, "order_budget_exhausted", reason=str(exc)
                )
                return order
            order.priced_ms = now_ms()
            return await self._place_with_retries(order)

    def _rounded(self, intent: OrderIntent) -> OrderIntent:
        """The same intent on the venue's grid. The one place rounding happens.

        The rounded values replace the asked-for ones, so the registry row and every
        reconciliation of it are about the order that was actually sent.

        Raises:
            RoundingError: the coin has no `szDecimals`, a price will not round, or the
                size rounds away to zero. Zero is a size the venue would take and nobody
                meant, so it is refused rather than sent.
        """
        decimals = self.sz_decimals.get(intent.coin)
        if decimals is None:
            raise RoundingError(
                f"no szDecimals for {intent.coin}: refusing to round it onto a guessed grid"
            )
        px = round_price(intent.px, decimals)
        size = round_size(intent.size, decimals)
        if size <= 0:
            raise RoundingError(
                f"{intent.coin} size {intent.size} rounds to zero at szDecimals {decimals}"
            )
        trigger = round_price(intent.trigger_px, decimals) if intent.trigger_px else None
        return replace(
            intent,
            px=float(px),
            size=float(size),
            trigger_px=float(trigger) if trigger is not None else None,
            wire_px=str(px),
            wire_size=str(size),
        )

    async def _place_with_retries(self, order: Order) -> Order:
        last: TransientTransportError | None = None
        for attempt in range(self.retries + 1):
            try:
                oid = await self.transport.place(order.intent)
            except RejectError as exc:
                order.reject_reason = str(exc)
                await self._transition(
                    order, OrderStatus.REJECTED, "order_rejected", reason=str(exc), attempt=attempt
                )
                return order
            except TransientTransportError as exc:
                last = exc
                log.warning(
                    "order_submit_retry",
                    cloid=order.cloid,
                    coin=order.coin,
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt < self.retries:
                    await asyncio.sleep(self._delay(attempt))
                continue
            order.oid = oid
            ack_ms = now_ms() - order.submitted_ms
            if self._on_ack_ms is not None:
                self._on_ack_ms(float(ack_ms))
            await self._transition(order, OrderStatus.ACKED, "order_acked", oid=oid, ack_ms=ack_ms)
            return order

        assert last is not None
        log.error(
            "order_submit_failed",
            cloid=order.cloid,
            coin=order.coin,
            attempts=self.retries + 1,
            error=str(last),
        )
        raise last

    def _delay(self, attempt: int) -> float:
        return self.backoff_s * (2.0**attempt) * (0.5 + random.random())  # noqa: S311

    async def cancel(self, cloid: str) -> Order:
        """Cancel by cloid. Idempotent: a cancelled or finished order is left alone."""
        order = self._orders[cloid]
        if order.status not in LIVE:
            log.info("order_cancel_skipped", cloid=cloid, status=str(order.status))
            return order
        # Counted, never refused: see execution/ratelimit.py.
        self._spend(reducing=True)
        await self.transport.cancel(order.coin, cloid)
        await self._transition(
            order,
            OrderStatus.CANCELED,
            "order_canceled",
            filled=order.filled,
            remaining=order.remaining,
        )
        return order

    async def submit_bracket(
        self, *, coin: str, is_long: bool, size: float, tp_px: float, sl_px: float
    ) -> TpslLegs:
        """Attach a take profit and a stop to a position as one `positionTpsl` group.

        Both legs are registered here before they are placed, and persisted, because a
        trigger order the venue holds and the registry does not is a stop nobody is
        tracking: reconciliation would report it as a stranger's order for as long as it
        rests, and a restart would not know to look for it.

        The group is one action to us and one refusal if it fails. A group half accepted
        would leave a position that looks protected and is not, so a refusal marks both
        legs REJECTED rather than leaving one live.

        Returns:
            The two cloids. `execution/tpsl.py` explains why they are the only handle:
            the venue answers `waitingForTrigger` with no oid, and `isPositionTpsl` reads
            false on both legs of the group that created them.
        """
        legs = TpslLegs(tp_cloid=new_cloid(), sl_cloid=new_cloid())
        orders = []
        for cloid, px, tag in (
            (legs.tp_cloid, tp_px, "target"),
            (legs.sl_cloid, sl_px, "stop"),
        ):
            intent = OrderIntent(
                cloid=cloid,
                coin=coin,
                is_buy=not is_long,
                px=px,
                size=size,
                tif="Ioc",  # isMarket on the trigger: a gap must not leave a stop unfilled
                reduce_only=True,
                tag=tag,
                trigger_px=px,
                tpsl="tp" if tag == "target" else "sl",
            )
            order = Order(intent=self._rounded(intent), status=OrderStatus.NEW)
            self._orders[cloid] = order
            orders.append(order)
            await self._persist(order, "bracket_leg_registered", tag=tag, px=px)

        requests = tpsl_requests(
            coin=coin,
            is_long=is_long,
            size=orders[0].intent.size,
            tp_px=orders[0].intent.px,
            sl_px=orders[1].intent.px,
            tp_cloid=legs.tp_cloid,
            sl_cloid=legs.sl_cloid,
        )
        try:
            self._spend(reducing=True)  # a bracket reduces risk; never refused
            statuses = await self.transport.place_group(requests)
        except RejectError as exc:
            for order in orders:
                order.reject_reason = str(exc)
                await self._transition(
                    order, OrderStatus.REJECTED, "bracket_rejected", reason=str(exc)
                )
            return legs

        for order in orders:
            await self._transition(order, OrderStatus.ACKED, "bracket_acked", statuses=statuses)
        return legs

    async def reprice(self, cloid: str, px: float) -> Order:
        """Move a resting order to `px`, keeping its cloid. The entry policy's one action.

        Measured on testnet 2026-09-08 (docs/HYPERLIQUID.md): `modify` answers with a
        **new oid** and the same cloid, so the venue is doing a cancel-and-replace and
        queue priority is not preserved. What it buys is one round trip instead of two and
        an identity that survives the move, which is why the registry keys on the cloid
        and merely records whichever oid the order is wearing now.

        A price is rounded here exactly as `submit` rounds one: a reprice is an order, and
        an unrounded price is refused rather than sent onto a grid the venue would reject.

        Returns:
            The order. Unchanged and still live when the venue said the order had already
            been cancelled or filled, which is not a rejection to record but a race the
            next reconciliation settles: marking it REJECTED would say "this did not
            happen" about an order that may have filled, and that is the one direction the
            registry must never be wrong in.

        Raises:
            TransientTransportError: every retry failed, same as `submit`.
        """
        order = self._orders[cloid]
        async with self._locks.setdefault(cloid, asyncio.Lock()):
            if order.status not in LIVE:
                log.info("order_reprice_skipped", cloid=cloid, status=str(order.status))
                return order
            try:
                intent = self._rounded(replace(order.intent, px=px))
            except RoundingError as exc:
                log.error("order_reprice_unroundable", cloid=cloid, px=px, error=str(exc))
                return order

            try:
                self._spend(reducing=False)
            except BudgetExhaustedError as exc:
                # The order stays where it is, still live. A reprice is the most
                # discretionary action the loop takes and the first one to give up.
                log.warning("order_reprice_over_budget", cloid=cloid, px=px, reason=str(exc))
                return order

            was_px = order.intent.px
            try:
                oid = await self.transport.modify(intent)
            except RejectError as exc:
                if _already_gone(exc):
                    log.info("order_reprice_lost_race", cloid=cloid, px=px, reason=str(exc))
                    return order
                order.reject_reason = str(exc)
                await self._transition(
                    order, OrderStatus.REJECTED, "order_reprice_rejected", reason=str(exc)
                )
                return order

            order.intent = intent
            order.oid = oid
            order.reprices += 1
            order.priced_ms = now_ms()
            await self._transition(
                order,
                order.status,
                "order_repriced",
                oid=oid,
                was_px=was_px,
                px=intent.px,
                reprices=order.reprices,
            )
            return order

    async def record_fill(
        self,
        cloid: str,
        *,
        size: float,
        px: float,
        ts_ms: int,
        fee: float = 0.0,
        maker: bool = False,
        closed_pnl: float = 0.0,
    ) -> Order:
        """Apply one fill. Raises KeyError for a cloid we never placed.

        `closed_pnl` is Hyperliquid's own `closedPnl` for this fill, zero on a fill that
        opened rather than closed. It is kept because it is the only realised PnL the venue
        path has: the exit price alone would need an entry price to be netted against, and
        the venue already did that arithmetic against the lot it actually matched.

        An unknown cloid is never invented into an order: a fill for something we did not
        place is a reconciliation event and a halt, not a row in our book.
        """
        order = self._orders.get(cloid)
        if order is None:
            log.error("fill_for_unknown_cloid", cloid=cloid, size=size, px=px)
            raise KeyError(cloid)

        # Size weighted average, so a partial at one price and the rest at another give
        # the price the position actually cost.
        order.avg_px = (order.avg_px * order.filled + px * size) / (order.filled + size)
        order.filled += size
        order.fees += fee
        order.closed_pnl += closed_pnl
        await self.store.add_fill(cloid, ts_ms=ts_ms, px=px, size=size, fee=fee, maker=maker)

        complete = order.remaining <= 1e-12
        status = OrderStatus.FILLED if complete else OrderStatus.PARTIAL
        await self._transition(
            order,
            status,
            "order_filled",
            px=px,
            size=size,
            fee=fee,
            closed_pnl=closed_pnl,
            maker=maker,
            filled=order.filled,
            remaining=order.remaining,
        )
        return order

    async def record_reject(self, cloid: str, reason: str) -> Order:
        """A reject that arrived asynchronously, on `orderUpdates` rather than on submit."""
        order = self._orders[cloid]
        order.reject_reason = reason
        return await self._transition(
            order, OrderStatus.REJECTED, "order_rejected", reason=reason, source="orderUpdates"
        )

    async def record_cancel(self, cloid: str, reason: str = "venue") -> Order:
        """A cancel the venue told us about: `marginCanceled`, a self trade, a TP/SL pair."""
        order = self._orders[cloid]
        return await self._transition(
            order, OrderStatus.CANCELED, "order_canceled", reason=reason, filled=order.filled
        )

    async def record_ack(self, cloid: str, oid: int) -> Order:
        order = self._orders[cloid]
        order.oid = oid
        return await self._transition(order, OrderStatus.ACKED, "order_acked", oid=oid)

    async def _transition(
        self, order: Order, status: OrderStatus, event: str, **fields: object
    ) -> Order:
        previous = order.status
        # A fill on a cancelled order moves it back to PARTIAL on purpose: the venue is
        # the truth and the cancel plainly lost the race. A fill after FILLED or REJECTED
        # would be a different bug and is left to reconciliation to shout about.
        order.status = status
        order.updated_ms = now_ms()
        await self._persist(order, event, previous=str(previous), **fields)
        return order

    async def _persist(self, order: Order, event: str, **fields: object) -> None:
        await self.store.upsert(order)
        log.info(event, cloid=order.cloid, coin=order.coin, status=str(order.status), **fields)
