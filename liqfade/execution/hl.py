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
import time
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
    "NonceGate",
    "VenueFill",
    "VenueOrder",
    "VenuePosition",
    "as_transport_error",
    "classify_group_response",
    "classify_order_response",
    "parse_asset_meta",
    "parse_clearinghouse_state",
    "parse_open_orders",
    "parse_trigger_orders",
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
    # The USDC sitting in the spot wallet, which is collateral the perps summary does not
    # count. Included in `equity`; kept separately so the panel can show where it is.
    spot_usdc: float = 0.0


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


def classify_group_response(response: Any) -> list[str]:
    """The per-leg statuses, or the reason one of them was refused.

    Raises:
        RejectError: the group was refused, or any single leg was. One leg refused leaves
            the position with half a bracket, which is worse than none because it looks
            protected. The caller cancels the survivor and reports.
    """
    if not isinstance(response, dict) or response.get("status") != "ok":
        raise RejectError(str((response or {}).get("response") or response))
    data = response.get("response", {})
    statuses = data.get("data", {}).get("statuses") if isinstance(data, dict) else None
    if not statuses:
        raise RejectError(f"tpsl group carried no status: {response!r}")
    for status in statuses:
        if isinstance(status, dict) and "error" in status:
            raise RejectError(str(status["error"]))
    return [str(status) for status in statuses]


def as_transport_error(exc: BaseException) -> Exception:
    """Classify an exception the SDK raised. Transient only when it may not have landed."""
    if isinstance(exc, TRANSIENT_EXCEPTIONS):
        return TransientTransportError(f"{type(exc).__name__}: {exc}")
    return RejectError(f"{type(exc).__name__}: {exc}")


def parse_clearinghouse_state(state: Any, *, spot: Any = None) -> AccountSnapshot:
    """Positions and equity from `clearinghouseState`, plus the spot USDC behind it.

    A coin the account is flat in stays in `assetPositions` with `szi` of "0.0". It is
    dropped here, because a zero position kept in the map is a difference the reconciler
    would report against a book that is correct.

    **Equity is the perps account value plus the spot USDC.** Measured on testnet
    2026-09-08: a flat account whose USDC sits in the spot wallet reports
    `accountValue: 0.0`, `withdrawable: 0.0` and no margin used, and opens positions
    perfectly well. Reading equity as `accountValue` alone made sizing refuse every entry
    for `risk_budget`, which is indistinguishable from a strategy finding nothing to
    trade. The same would happen on mainnet to an owner who leaves their USDC in spot.

    Args:
        spot: a `spotClearinghouseState` payload, or None to leave equity as the perps
            account value alone, which is what callers that never ask for spot expect.
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
    spot_usdc = 0.0
    for balance in (spot or {}).get("balances", []):
        if balance.get("coin") == "USDC":
            spot_usdc = _f(balance.get("total"))
            break
    return AccountSnapshot(
        equity=_f(summary.get("accountValue")) + spot_usdc,
        margin_used=_f(summary.get("totalMarginUsed")),
        withdrawable=_f((state or {}).get("withdrawable")),
        positions=positions,
        spot_usdc=spot_usdc,
    )


@dataclass(frozen=True, slots=True)
class AssetRules:
    """What the venue will accept for one asset. Read from `meta`, never assumed."""

    coin: str
    sz_decimals: int
    max_leverage: float


def parse_asset_meta(payload: Any) -> dict[str, AssetRules]:
    """`meta.universe` into the rounding rules, delisted assets dropped.

    `szDecimals` decides whether an order is accepted at all, so it comes from here and
    not from a default. `backtest/engine.py`'s `_resolve_meta` answers 5 for every coin,
    which is fine for a replay and wrong at a venue: ETH is 4, and an order with five
    decimals is refused with "Order has invalid size".
    """
    rules: dict[str, AssetRules] = {}
    for asset in (payload or {}).get("universe", []):
        if asset.get("isDelisted"):
            continue
        name = str(asset.get("name"))
        decimals = asset.get("szDecimals")
        if not name or decimals is None:
            continue
        rules[name] = AssetRules(
            coin=name,
            sz_decimals=int(decimals),
            max_leverage=_f(asset.get("maxLeverage"), 1.0),
        )
    return rules


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


@dataclass(frozen=True, slots=True)
class TriggerOrder:
    """One resting trigger order, from `frontendOpenOrders`.

    Trigger orders do not appear in `openOrders` at all, so reconciliation that reads only
    that endpoint sees a position with no protection on it.
    """

    cloid: str | None
    coin: str
    oid: int
    trigger_px: float
    is_stop: bool
    size: float
    reduce_only: bool


def parse_trigger_orders(payload: Any) -> list[TriggerOrder]:
    """The trigger orders in a `frontendOpenOrders` payload, plain limits skipped.

    `orderType` is the discriminator, spelled "Stop Market" and "Take Profit Market" in
    the measured payload. `isPositionTpsl` is deliberately not read: it is false on both
    legs of a `positionTpsl` group, so filtering on it finds nothing.
    """
    orders = []
    for row in payload or []:
        if not row.get("isTrigger"):
            continue
        orders.append(
            TriggerOrder(
                cloid=row.get("cloid"),
                coin=str(row.get("coin")),
                oid=int(row.get("oid", 0)),
                trigger_px=_f(row.get("triggerPx")),
                is_stop="stop" in str(row.get("orderType", "")).lower(),
                size=_f(row.get("sz")),
                reduce_only=bool(row.get("reduceOnly")),
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
        """The account, including the spot USDC that is collateral for the perps.

        Two calls rather than one, because `clearinghouseState` alone reports zero equity
        for an account that is flat with its USDC in spot, and sizing would refuse every
        entry on it.
        """
        state, spot = await asyncio.gather(
            self.post({"type": "clearinghouseState", "user": address}),
            self.post({"type": "spotClearinghouseState", "user": address}),
        )
        return parse_clearinghouse_state(state, spot=spot)

    async def asset_meta(self) -> dict[str, AssetRules]:
        """The venue's own rounding rules for every listed perp."""
        return parse_asset_meta(await self.post({"type": "meta"}))

    async def open_orders(self, address: str) -> list[VenueOrder]:
        return parse_open_orders(await self.post({"type": "openOrders", "user": address}))

    async def trigger_orders(self, address: str) -> list[TriggerOrder]:
        """Resting trigger orders, which `openOrders` does not carry at all."""
        return parse_trigger_orders(
            await self.post({"type": "frontendOpenOrders", "user": address})
        )

    async def user_fills(self, address: str) -> list[VenueFill]:
        return parse_user_fills(await self.post({"type": "userFills", "user": address}))


class NonceGate:
    """Serialises signed actions so no two share a millisecond.

    docs/HYPERLIQUID.md: nonces are millisecond timestamps that must increase per signer.
    The SDK stamps one per call from the wall clock, so two calls inside the same
    millisecond produce the same nonce and the venue refuses the second with
    "Invalid nonce: duplicate nonce". Drilled on testnet during a kill, where three
    cancels went out back to back and one was silently lost: the registry recorded the
    cancel and the venue kept the order.

    A lock alone is not enough, because a lock does not make the clock advance. This waits
    until the millisecond after the last action before letting the next one sign.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last_ms = 0

    async def __aenter__(self) -> None:
        await self._lock.acquire()
        # The wait happens with the lock held, and the kill wraps every venue call in a
        # `wait_for`: a call abandoned at its deadline is cancelled inside this sleep and
        # never reaches `__aexit__`. Released here, or the lock leaks once and every later
        # signed action in the process waits on it forever.
        try:
            now = int(time.time() * 1000)
            if now <= self._last_ms:
                await asyncio.sleep((self._last_ms + 1 - now) / 1000.0)
                now = int(time.time() * 1000)
            self._last_ms = now
        except BaseException:
            self._lock.release()
            raise

    async def __aexit__(self, *exc: object) -> None:
        self._lock.release()


class HyperliquidTransport:
    """`OrderTransport` against the real venue. The SDK is sync, so every call is a thread.

    One `Exchange` for the process, per docs/HYPERLIQUID.md: nonces are millisecond
    timestamps that must increase per signer, so a second signing client in the same
    process is a nonce collision waiting to happen.
    """

    def __init__(self, signer: Signer) -> None:
        self.signer = signer
        self._exchange = signer.exchange()
        # One gate for the process: every signed action goes through it, in order.
        self._nonce = NonceGate()

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
            async with self._nonce:
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

    async def place_group(self, requests: list[dict[str, Any]]) -> list[str]:
        """Place a `positionTpsl` group: the whole bracket, or nothing.

        `grouping="positionTpsl"` is what ties the legs to the position rather than to
        each other, which is what makes the venue cancel both when the position closes
        (measured, docs/HYPERLIQUID.md probe 3). Without it the two are unrelated orders
        and a closed position leaves both resting.
        """
        from hyperliquid.utils.types import Cloid

        wire = [{**request, "cloid": Cloid.from_str(str(request["cloid"]))} for request in requests]
        try:
            async with self._nonce:
                response = await asyncio.to_thread(
                    self._exchange.bulk_orders, wire, grouping="positionTpsl"
                )
        except Exception as exc:
            raise as_transport_error(exc) from exc
        return classify_group_response(response)

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
            async with self._nonce:
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
            async with self._nonce:
                response = await asyncio.to_thread(
                    self._exchange.cancel_by_cloid, coin, Cloid.from_str(cloid)
                )
        except Exception as exc:
            raise as_transport_error(exc) from exc
        if isinstance(response, dict) and response.get("status") != "ok":
            # A cancel that failed because the order is already gone is not an error worth
            # raising on: reconciliation is what settles what is actually resting.
            log.warning("cancel_not_ok", coin=coin, cloid=cloid, response=response)
