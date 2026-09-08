"""The WebSocket user channel: fills and order updates pushed, instead of polled.

`InfoReader` can read `userFills` and `openOrders` over REST, and reconciliation does
exactly that on its interval. That leaves a fill unknown for up to `execution.reconcile_s`,
which is a minute of the loop believing it has a resting order that is already a position.

**Keyed on the cloid and never the oid.** Measured on testnet 2026-09-08 (probe 1): every
`orderUpdates` frame carries the cloid. It has to be the key, because `modify` answers with
a *new* oid, so a client keyed on oid loses an order the moment the entry policy reprices
it. An event whose cloid we do not recognise is dropped: another tool on the same account,
or an order from a process that came before this one, is not ours to move.

**The opening snapshot is not a set of new fills.** `userFills` pushes `isSnapshot: true`
on subscribe and again on every reconnect. Booking those would double the registry's fills
once per reconnect. History is reconciliation's job; this stream carries what is new.

**This does not replace reconciliation.** A socket that was away missed frames, and a
socket that is up can still be behind. `Reconciler` keeps running on its interval and on
every reconnect, which is CLAUDE.md rule 4, and this only makes it usually find nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import orjson

from liqfade.execution.orders import LIVE, OrderManager
from liqfade.log import get_logger
from liqfade.recorder.streams.base import WSStream

log = get_logger(__name__)

__all__ = ["UserEvent", "UserStream", "apply_event", "parse_user_frame", "subscribe_frames"]


@dataclass(frozen=True, slots=True)
class UserEvent:
    """One thing that happened to one of our orders, from either channel."""

    kind: str  # open, canceled, filled, rejected, fill
    cloid: str
    oid: int
    coin: str
    px: float = 0.0
    size: float = 0.0
    fee: float = 0.0
    maker: bool = True
    ts_ms: int = 0
    closed_pnl: float = 0.0  # the venue's `closedPnl`; non-zero only on a closing fill


def subscribe_frames(address: str) -> list[dict[str, Any]]:
    """The two subscriptions this client opens, in the order it sends them."""
    return [
        {"method": "subscribe", "subscription": {"type": "orderUpdates", "user": address}},
        {"method": "subscribe", "subscription": {"type": "userFills", "user": address}},
    ]


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_user_frame(message: Any) -> list[UserEvent]:
    """Turn one decoded socket frame into events. Never raises: this runs in a receive loop."""
    if not isinstance(message, dict):
        return []
    channel = message.get("channel")
    data = message.get("data")

    if channel == "orderUpdates":
        events = []
        for update in data or []:
            order = update.get("order") or {}
            cloid = order.get("cloid")
            if not cloid:
                continue
            events.append(
                UserEvent(
                    kind=str(update.get("status") or "unknown"),
                    cloid=str(cloid),
                    oid=int(order.get("oid") or 0),
                    coin=str(order.get("coin") or ""),
                    px=_f(order.get("limitPx")),
                    size=_f(order.get("sz")),
                    ts_ms=int(update.get("statusTimestamp") or 0),
                )
            )
        return events

    if channel == "userFills":
        if not isinstance(data, dict) or data.get("isSnapshot"):
            return []
        events = []
        for fill in data.get("fills") or []:
            cloid = fill.get("cloid")
            if not cloid:
                continue
            events.append(
                UserEvent(
                    kind="fill",
                    cloid=str(cloid),
                    oid=int(fill.get("oid") or 0),
                    coin=str(fill.get("coin") or ""),
                    px=_f(fill.get("px")),
                    size=_f(fill.get("sz")),
                    fee=_f(fill.get("fee")),
                    # `crossed` true means we took, so maker is its inverse.
                    maker=not bool(fill.get("crossed", True)),
                    ts_ms=int(fill.get("time") or 0),
                    closed_pnl=_f(fill.get("closedPnl")),
                )
            )
        return events

    return []


async def apply_event(manager: OrderManager, event: UserEvent) -> None:
    """Push one event into the registry. Unknown cloids and bad states are logged, not raised.

    A receive loop that dies on somebody else's order stops delivering our own.
    """
    try:
        order = manager.get(event.cloid)
    except KeyError:
        log.debug("user_event_unknown_cloid", cloid=event.cloid, kind=event.kind)
        return

    try:
        if event.kind == "fill":
            await manager.record_fill(
                event.cloid,
                px=event.px,
                size=event.size,
                fee=event.fee,
                maker=event.maker,
                ts_ms=event.ts_ms,
                closed_pnl=event.closed_pnl,
            )
        elif event.kind == "canceled" and order.status in LIVE:
            if order.oid is not None and event.oid and event.oid != order.oid:
                # A reprice is a cancel-and-replace at the venue: it retires the old oid
                # and rests a new one under the same cloid. The retirement frame arrives
                # after we already hold the new oid, and taking it at face value marked a
                # live order dead. Drilled on 2026-09-08: a repriced entry survived a kill
                # because the registry believed it was already gone.
                log.debug("stale_cancel_ignored", cloid=event.cloid, oid=event.oid)
                return
            await manager.record_cancel(event.cloid, reason="venue")
        elif event.kind == "rejected" and order.status in LIVE:
            await manager.record_reject(event.cloid, reason="venue")
        elif event.kind == "open" and order.status in LIVE and order.oid != event.oid:
            # LIVE only. Hyperliquid sends `open` before the fill, and on a fast Ioc the
            # two can be applied out of order: acking a filled order puts it back in the
            # live set, where reconciliation reads it as an order the venue has lost and
            # the entry policy may try to reprice it. A fill is terminal and a late `open`
            # is old news.
            await manager.record_ack(event.cloid, event.oid)
    except Exception:
        log.exception("user_event_failed", cloid=event.cloid, kind=event.kind)


class UserStream:
    """The account's socket, reconnecting forever, feeding the registry.

    Built on `recorder/streams/base.WSStream` rather than a second socket loop, so it
    inherits the reconnect backoff, the ping cadence and the rotation that
    `docs/DECISIONS.md` 2026-09-06 measured Hyperliquid needing: a connection replaced
    before the venue expires it, rather than after. It also inherits `on_reconnect`, which
    is what CLAUDE.md rule 4 wants wired to reconciliation.

    Two tasks: the socket receives and enqueues, a drain applies. The sink runs inside the
    receive loop and must not block, and applying an event writes sqlite, so a queue
    between them is what keeps a slow disk from stalling the socket.
    """

    def __init__(
        self,
        *,
        url: str,
        address: str,
        manager: OrderManager,
        cfg: Any,
        on_reconnect: Callable[[str], None] | None = None,
        on_fill: Callable[[UserEvent], Awaitable[None]] | None = None,
    ) -> None:
        """
        Args:
            on_fill: called after a fill has been written to the registry, so the session
                can bracket the position it opened. After, not before: a bracket placed
                against a fill the registry has not recorded would be a stop for a
                position that a restart does not know about.
        """
        self.manager = manager
        self._on_fill = on_fill
        self.queue: asyncio.Queue[UserEvent] = asyncio.Queue()
        self.applied = 0
        self.stream = WSStream(
            "hyperliquid_user",
            url,
            parse=self._parse,
            sink=self._sink,
            cfg=cfg,
            on_connect=lambda _symbols: subscribe_frames(address),
            on_reconnect=on_reconnect,
        )

    def _parse(self, raw: bytes, rx_ms: int) -> list[tuple[str, dict[str, Any] | None]]:
        try:
            message = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return []
        return [("user_event", {"event": event}) for event in parse_user_frame(message)]

    def _sink(self, table: str, venue: str, row: dict[str, Any]) -> None:
        self.queue.put_nowait(row["event"])

    async def drain(self, stop: asyncio.Event) -> None:
        """Apply queued events until stopped. One at a time: the registry is the truth."""
        while not stop.is_set():
            try:
                event = await asyncio.wait_for(self.queue.get(), timeout=0.5)
            except TimeoutError:
                continue
            await apply_event(self.manager, event)
            self.applied += 1
            if event.kind == "fill" and self._on_fill is not None:
                try:
                    await self._on_fill(event)
                except Exception:
                    log.exception("user_fill_hook_failed", cloid=event.cloid)

    async def run(self, stop: asyncio.Event) -> None:
        """The socket and the drain together, until stopped."""
        drain = asyncio.create_task(self.drain(stop))
        try:
            await self.stream.run(stop)
        finally:
            stop.set()
            await drain
