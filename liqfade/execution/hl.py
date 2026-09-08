"""Speaking to Hyperliquid: the signing transport, the info reader, and the parsers.

Three things, and they are separate on purpose.

**The parsers are pure functions on the raw payloads**, so every shape docs/HYPERLIQUID.md
describes has an offline test. Parsing is where the bugs that cannot be found on testnet
live: a `szi` of "0.0" read as an open position, an order placed by hand in the UI with no
`cloid` key, a `crossed` flag read the wrong way round and every fill booked at the wrong
fee.

**`classify_order_response` decides retry or refuse**, which is the one decision in this
file that costs money if it is wrong. It fails closed: a shape it does not recognise is a
`RejectError`, never a retry, because an ambiguous answer retried in a loop burns the
address action budget that docs/HYPERLIQUID.md warns about and ends with the account
unable to place anything at all. Only a transport level failure, where the request may
never have reached the venue, is transient.

**`HyperliquidTransport` wraps the official SDK**, which is synchronous, on a thread. It
holds a `Signer` and never sees a key. `InfoReader` needs no key at all: `/info` is public,
so reconciliation reads positions and orders over plain httpx rather than through the SDK.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
import orjson

from liqfade.execution.orders import OrderIntent, RejectError, TransientTransportError
from liqfade.execution.signer import Signer
from liqfade.log import get_logger

log = get_logger(__name__)

__all__ = [
    "AccountSnapshot",
    "HyperliquidTransport",
    "InfoReader",
    "VenueFill",
    "VenueOrder",
    "VenuePosition",
    "as_transport_error",
    "classify_order_response",
    "parse_clearinghouse_state",
    "parse_open_orders",
    "parse_user_fills",
]

# Exceptions that mean the request may or may not have reached the venue. Anything else
# raised by the SDK is treated as a refusal, because we cannot tell it apart from one.
TRANSIENT_EXCEPTIONS = (
    TimeoutError,
    ConnectionError,
    OSError,
    httpx.TransportError,
)


@dataclass(frozen=True, slots=True)
class VenuePosition:
    """One position as the venue sees it. `size` is signed: positive long, negative short."""

    coin: str
    size: float
    entry_px: float
    unrealized_pnl: float = 0.0


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """`clearinghouseState`, parsed. The venue's answer to "what do I actually hold"."""

    equity: float
    margin_used: float
    withdrawable: float
    positions: dict[str, VenuePosition]


@dataclass(frozen=True, slots=True)
class VenueOrder:
    """One resting order. `cloid` is None for anything placed outside this bot."""

    oid: int
    coin: str
    is_buy: bool
    px: float
    size: float
    ts_ms: int
    cloid: str | None = None


@dataclass(frozen=True, slots=True)
class VenueFill:
    """One fill from `userFills`."""

    coin: str
    px: float
    size: float
    is_buy: bool
    ts_ms: int
    oid: int
    fee: float
    maker: bool
    closed_pnl: float = 0.0
    cloid: str | None = None
    liquidation: bool = False


def _f(value: Any, default: float = 0.0) -> float:
    """Hyperliquid sends every number as a string. One place that stops being surprising."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def classify_order_response(response: Any) -> int:
    """The exchange order id, or the reason there is not one.

    Args:
        response: the decoded body of a signed `order` action.

    Returns:
        The oid, whether the order rested or filled immediately.

    Raises:
        RejectError: the venue refused the order, or answered in a shape this function
            does not recognise. Never retried by the order manager.
    """
    if not isinstance(response, dict):
        raise RejectError(f"unrecognised order response: {response!r}")
    if response.get("status") != "ok":
        raise RejectError(str(response.get("response") or response))

    data = response.get("response", {})
    statuses = data.get("data", {}).get("statuses") if isinstance(data, dict) else None
    if not statuses:
        raise RejectError(f"order response carried no status: {response!r}")

    status = statuses[0]
    if isinstance(status, dict):
        if "error" in status:
            raise RejectError(str(status["error"]))
        for key in ("resting", "filled"):
            if key in status and "oid" in status[key]:
                return int(status[key]["oid"])
    raise RejectError(f"unrecognised order status: {status!r}")


def as_transport_error(exc: BaseException) -> Exception:
    """Classify an exception the SDK raised. Transient only when it may not have landed."""
    if isinstance(exc, TRANSIENT_EXCEPTIONS):
        return TransientTransportError(f"{type(exc).__name__}: {exc}")
    return RejectError(f"{type(exc).__name__}: {exc}")


def parse_clearinghouse_state(state: Any) -> AccountSnapshot:
    """Positions and equity from `clearinghouseState`.

    A coin the account is flat in stays in `assetPositions` with `szi` of "0.0". It is
    dropped here, because a zero position kept in the map is a difference the reconciler
    would report against a book that is correct.
    """
    summary = state.get("marginSummary", {}) if isinstance(state, dict) else {}
    positions: dict[str, VenuePosition] = {}
    for entry in (state or {}).get("assetPositions", []):
        position = entry.get("position", {})
        size = _f(position.get("szi"))
        if size == 0.0:
            continue
        coin = str(position.get("coin"))
        positions[coin] = VenuePosition(
            coin=coin,
            size=size,
            entry_px=_f(position.get("entryPx")),
            unrealized_pnl=_f(position.get("unrealizedPnl")),
        )
    return AccountSnapshot(
        equity=_f(summary.get("accountValue")),
        margin_used=_f(summary.get("totalMarginUsed")),
        withdrawable=_f((state or {}).get("withdrawable")),
        positions=positions,
    )


def parse_open_orders(payload: Any) -> list[VenueOrder]:
    """`openOrders`, parsed. `side` is "B" for a bid and "A" for an ask."""
    orders = []
    for row in payload or []:
        orders.append(
            VenueOrder(
                oid=int(row.get("oid", 0)),
                coin=str(row.get("coin")),
                is_buy=row.get("side") == "B",
                px=_f(row.get("limitPx")),
                size=_f(row.get("sz")),
                ts_ms=int(row.get("timestamp", 0)),
                cloid=row.get("cloid"),
            )
        )
    return orders


def parse_user_fills(payload: Any) -> list[VenueFill]:
    """`userFills`, parsed. `crossed` true means we were the taker, so maker is its inverse."""
    fills = []
    for row in payload or []:
        fills.append(
            VenueFill(
                coin=str(row.get("coin")),
                px=_f(row.get("px")),
                size=_f(row.get("sz")),
                is_buy=row.get("side") == "B",
                ts_ms=int(row.get("time", 0)),
                oid=int(row.get("oid", 0)),
                fee=_f(row.get("fee")),
                maker=not bool(row.get("crossed", True)),
                closed_pnl=_f(row.get("closedPnl")),
                cloid=row.get("cloid"),
                liquidation=bool(row.get("liquidation")),
            )
        )
    return fills


class InfoReader:
    """Read only `/info` queries. No key, no signing, one client for the process."""

    def __init__(self, base_url: str, *, timeout_s: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout_s)
        self.last_rtt_ms: float | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def post(self, body: dict[str, Any]) -> Any:
        loop = asyncio.get_running_loop()
        started = loop.time()
        response = await self._client.post(f"{self.base_url}/info", json=body)
        self.last_rtt_ms = (loop.time() - started) * 1000.0
        response.raise_for_status()
        return orjson.loads(response.content)

    async def account(self, address: str) -> AccountSnapshot:
        return parse_clearinghouse_state(
            await self.post({"type": "clearinghouseState", "user": address})
        )

    async def open_orders(self, address: str) -> list[VenueOrder]:
        return parse_open_orders(await self.post({"type": "openOrders", "user": address}))

    async def user_fills(self, address: str) -> list[VenueFill]:
        return parse_user_fills(await self.post({"type": "userFills", "user": address}))


class HyperliquidTransport:
    """`OrderTransport` against the real venue. The SDK is sync, so every call is a thread.

    One `Exchange` for the process, per docs/HYPERLIQUID.md: nonces are millisecond
    timestamps that must increase per signer, so a second signing client in the same
    process is a nonce collision waiting to happen.
    """

    def __init__(self, signer: Signer) -> None:
        self.signer = signer
        self._exchange = signer.exchange()

    async def place(self, intent: OrderIntent) -> int:
        """Send one already rounded intent. Nothing here rounds anything.

        `intent.wire` is the Decimal-derived spelling `OrderManager.submit` produced, and
        reading it is what makes an unrounded intent a loud refusal rather than an order
        on a grid the venue will reject. The SDK's signature takes floats and does its own
        `float_to_wire`, so the strings are parsed back one line before the call; the
        value is the venue's, not a binary fraction of the strategy's.
        """
        from hyperliquid.utils.types import Cloid

        wire_px, wire_size = intent.wire
        px, size = float(wire_px), float(wire_size)

        order_type: dict[str, Any]
        if intent.trigger_px is not None:
            order_type = {
                "trigger": {
                    "triggerPx": intent.trigger_px,
                    "isMarket": intent.tif == "Ioc",
                    "tpsl": intent.tpsl or "sl",
                }
            }
        else:
            order_type = {"limit": {"tif": intent.tif}}

        try:
            response = await asyncio.to_thread(
                self._exchange.order,
                intent.coin,
                intent.is_buy,
                size,
                px,
                order_type,
                intent.reduce_only,
                Cloid.from_str(intent.cloid),
            )
        except Exception as exc:
            raise as_transport_error(exc) from exc
        return classify_order_response(response)

    async def modify(self, intent: OrderIntent) -> int:
        """Move a resting order to a new price, addressed by its cloid.

        Addressed by **cloid and not oid**, because a modify answers with a new oid
        (docs/HYPERLIQUID.md probe 7): after one reprice the oid we were holding is dead,
        and a second reprice by it answers "Cannot modify canceled or filled order" while
        the live order sits untouched. The cloid is the identity that survives the move.

        The replacement carries the same cloid on purpose, so the venue and the registry
        agree on what this order is called for its whole life.
        """
        from hyperliquid.utils.types import Cloid

        wire_px, wire_size = intent.wire
        cloid = Cloid.from_str(intent.cloid)
        try:
            response = await asyncio.to_thread(
                self._exchange.modify_order,
                cloid,
                intent.coin,
                intent.is_buy,
                float(wire_size),
                float(wire_px),
                {"limit": {"tif": intent.tif}},
                intent.reduce_only,
                cloid,
            )
        except Exception as exc:
            raise as_transport_error(exc) from exc
        return classify_order_response(response)

    async def cancel(self, coin: str, cloid: str) -> None:
        from hyperliquid.utils.types import Cloid

        try:
            response = await asyncio.to_thread(
                self._exchange.cancel_by_cloid, coin, Cloid.from_str(cloid)
            )
        except Exception as exc:
            raise as_transport_error(exc) from exc
        if isinstance(response, dict) and response.get("status") != "ok":
            # A cancel that failed because the order is already gone is not an error worth
            # raising on: reconciliation is what settles what is actually resting.
            log.warning("cancel_not_ok", coin=coin, cloid=cloid, response=response)
