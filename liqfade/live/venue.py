"""The live order path: real orders at a real venue, and the venue as the truth.

`live/paper.py` runs the strategy against a fill simulator. This runs the same strategy
against Hyperliquid. The decision layers are shared and unchanged, which is CLAUDE.md's
central constraint: `features/`, `strategy/`, `sizing/` and `risk/` do not know which of
the two is calling them.

What is deliberately different from paper, and why:

- **There is no fill model.** A fill is something the venue tells us about, through the
  user stream or through reconciliation. Nothing here decides that an order filled.
- **The position book is a mirror, not a ledger.** `clearinghouseState` is the truth and
  `sync_account` overwrites what we think we hold. A local book that argued with the venue
  would be the bug reconciliation exists to find.
- **Exits are a `positionTpsl` bracket at the venue**, placed the moment an entry fills,
  rather than an order the tick rests and a stop the tick checks. A bracket survives this
  process dying; a stop checked every 250ms does not, and Phase 6 runs unattended.

**This module can place real orders.** It is reached only through `liqfade testnet` or
`liqfade live`, and `execution/signer.py` still holds all three of rule 3's gates: nothing
here weakens them, and nothing here asks for a signer.
"""

from __future__ import annotations

import os
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from liqfade.backtest.engine import _resolve_meta
from liqfade.backtest.fills import BookState
from liqfade.config.models import Settings
from liqfade.control.protocol import OrderState, PositionState
from liqfade.execution.hl import AccountSnapshot, InfoReader, VenuePosition
from liqfade.execution.orders import (
    LIVE,
    OrderIntent,
    OrderManager,
    OrderStatus,
    new_cloid,
)
from liqfade.execution.policy import EntryAction, entry_action, touch_price
from liqfade.execution.tpsl import TpslLegs
from liqfade.live.features import LiveFeatureEngine
from liqfade.log import get_logger
from liqfade.risk.engine import AccountView, EntryRequest, RiskEngine
from liqfade.sizing import LiquidityStats, size_intent
from liqfade.strategy.types import Intent

log = get_logger(__name__)

__all__ = ["VenueSession"]

RECENT_BLOCKS = 256


@dataclass(frozen=True, slots=True)
class VenueTrade:
    """One closed round trip, as the venue accounted for it.

    `net_pnl` is Hyperliquid's own `closedPnl` off the fill that closed the position, not
    a number computed here. The venue charged the fees and knows the entry it matched
    against; recomputing it locally would produce a second answer to a question that has
    one.
    """

    coin: str
    net_pnl: float
    ts_ms: int


class VenueLedger:
    """The counters the loop and the daily report read. Deliberately thin.

    The venue is the accountant. This holds what the loop needs to report and nothing it
    would have to keep correct against `clearinghouseState`.
    """

    def __init__(self) -> None:
        self.trades: list[VenueTrade] = []


class VenueCounters:
    """The refusal counters `report_risk_blocks` drains, in the shape it expects.

    Named the same as the backtest's because the loop reads both through one attribute:
    a gate that refuses is one event to an owner whichever session produced it.
    """

    def __init__(self) -> None:
        self.recent_blocks: deque[tuple[int, str, str, str]] = deque(maxlen=RECENT_BLOCKS)
        self.sizing_blocks: dict[str, int] = {}
        self.risk_blocks: dict[str, int] = {}

    def blocked(self, *, now_ms: int, coin: str, gate: str, reason: str) -> None:
        counts = self.sizing_blocks if gate == "sizing" else self.risk_blocks
        counts[reason] = counts.get(reason, 0) + 1
        self.recent_blocks.append((now_ms, coin, gate, reason))


class VenueSession:
    """Strategy in, venue orders out, venue fills back. One per process."""

    def __init__(
        self,
        settings: Settings,
        *,
        orders: OrderManager,
        reader: InfoReader,
        address: str,
        meta: Mapping[str, Any] | None = None,
        starting_equity: float = 0.0,
        risk_state_path: Any = None,
    ) -> None:
        from liqfade.live.paper import _RecordingStrategy

        self.settings = settings
        self.orders = orders
        self.reader = reader
        self.address = address
        self.features = LiveFeatureEngine(settings)
        self.meta = _resolve_meta(dict(meta or {}), sorted(meta or {}), settings)
        self.strategy = _RecordingStrategy(settings, self.meta)
        self.risk = RiskEngine(settings, path=risk_state_path)

        self.books: dict[str, BookState] = {}
        self.positions: dict[str, VenuePosition] = {}
        self.brackets: dict[str, TpslLegs] = {}
        self.equity_usd = starting_equity
        # cloid -> the intent that asked for it, so a fill can be bracketed with the
        # target and stop the strategy actually chose rather than a recomputed pair.
        self.entry_intents: dict[str, Intent] = {}
        self.coin_last_ms: dict[str, int] = {}
        self.global_last_ms: int | None = None

        self.portfolio = VenueLedger()
        self.result = VenueCounters()
        self.intents: list[Intent] = []
        self.pending_injections: list[tuple[Intent, int]] = []
        self.fills: list[tuple[int, str, str, float, float]] = []
        self.ticks = 0

    # --- feeds ----------------------------------------------------------------

    def ingest(self, table: str, venue: str, row: Mapping[str, Any]) -> None:
        """One parsed row from any stream. Never raises: this runs in a receive loop."""
        try:
            self.features.ingest(table, venue, row)
            coin = row.get("coin")
            rx = int(row.get("rx_ms") or row.get("ts_ms") or 0)
            if venue == "hyperliquid" and coin and rx:
                self.coin_last_ms[str(coin)] = rx
                self.global_last_ms = max(self.global_last_ms or 0, rx)
                self._book(table, str(coin), row)
        except Exception as exc:
            log.warning("venue_ingest_failed", table=table, error=f"{type(exc).__name__}: {exc}")

    def _book(self, table: str, coin: str, row: Mapping[str, Any]) -> None:
        """Keep the touch, which is the only thing the entry policy prices against."""
        book = self.books.get(coin, BookState())
        if table == "bbo":
            self.books[coin] = BookState(
                bids=((float(row["bid_px"]), float(row.get("bid_sz") or 0.0)),),
                asks=((float(row["ask_px"]), float(row.get("ask_sz") or 0.0)),),
                snapshot_ms=int(row.get("rx_ms") or 0),
                last_px=book.last_px,
                last_trade_ms=book.last_trade_ms,
            )
        elif table == "trades":
            self.books[coin] = BookState(
                bids=book.bids,
                asks=book.asks,
                snapshot_ms=book.snapshot_ms,
                last_px=float(row["px"]),
                last_trade_ms=int(row.get("rx_ms") or 0),
            )

    def touch(self, coin: str, *, is_buy: bool) -> float | None:
        """The price a post-only entry rests at, or None with no book to rest against."""
        book = self.books.get(coin)
        if book is None:
            return None
        return touch_price(
            is_buy=is_buy,
            bid=book.bids[0][0] if book.bids else None,
            ask=book.asks[0][0] if book.asks else None,
        )

    # --- the account, from the venue -----------------------------------------

    def sync_account(self, snapshot: AccountSnapshot) -> None:
        """Overwrite the local view with the venue's. The venue is the truth.

        Called by reconciliation. Positions that closed while we were not looking simply
        disappear, and their brackets go with them: the venue cancels both legs when the
        position closes, so a bracket for a coin we no longer hold is already gone there.
        """
        self.equity_usd = snapshot.equity
        self.positions = dict(snapshot.positions)
        for coin in list(self.brackets):
            if coin not in self.positions:
                self.brackets.pop(coin, None)
                log.info("bracket_released", coin=coin, reason="position closed")

    def equity(self) -> float:
        return self.equity_usd

    def actions_remaining(self) -> int | None:
        return self.orders.actions_remaining()

    async def open_positions(self) -> dict[str, float]:
        """Coin to signed size, for the kill switch and `/flat`."""
        return {coin: position.size for coin, position in self.positions.items()}

    async def open_order_cloids(self) -> list[str]:
        return sorted(order.cloid for order in await self.orders.open_orders())

    # --- the clock ------------------------------------------------------------

    async def tick(self, now_ms: int, *, allow_entries: bool = True) -> list[Intent]:
        """One decision tick: age the resting entries, then look for new ones."""
        self.ticks += 1
        self.features.advance(now_ms)
        rows = {coin: self.features.row(coin, now_ms) for coin in self.features.coins}

        await self.age_entries(now_ms)

        self.strategy.allow_intents = allow_entries
        self.strategy.last_intents = []
        intents = self.strategy.on_tick(now_ms, rows)
        self.intents.extend(intents)
        if not allow_entries:
            return []
        for intent in intents:
            await self.place(intent, now_ms, rows.get(intent.coin))
        return intents

    async def age_entries(self, now_ms: int) -> None:
        """Run the entry policy over every resting entry. The same table the backtest runs."""
        for order in await self.orders.open_orders():
            if order.intent.tag != "entry":
                continue
            action = entry_action(
                now_ms=now_ms,
                visible_ms=order.priced_ms,
                reprices=order.reprices,
                settings=self.settings,
            )
            if action is EntryAction.HOLD:
                continue
            if action is EntryAction.ABANDON:
                log.info(
                    "entry_abandoned", cloid=order.cloid, coin=order.coin, reprices=order.reprices
                )
                await self.orders.cancel(order.cloid)
                self.entry_intents.pop(order.cloid, None)
                continue
            touch = self.touch(order.coin, is_buy=order.intent.is_buy)
            if touch is None:
                continue  # no book to reprice onto; hold rather than invent a price
            await self.orders.reprice(order.cloid, touch)

    async def place(self, intent: Intent, now_ms: int, row: Any = None) -> str | None:
        """Size an intent, gate it, and rest an `Alo` at the touch if it survives both.

        Returns the cloid of the order placed, or None when a gate refused it or there is
        no book to rest against. Every refusal is logged with its reason, because a gate
        that quietly stops trading looks exactly like a strategy finding nothing.
        """
        if intent.coin in self.positions:
            return None  # already on; the policy reprices rather than stacking
        if any(
            o.coin == intent.coin and o.intent.tag == "entry"
            for o in await self.orders.open_orders()
        ):
            return None

        if intent.coin not in self.meta:
            self.meta = _resolve_meta(self.meta, [intent.coin], self.settings)
            self.orders.sz_decimals = {c: m.sz_decimals for c, m in self.meta.items()}

        decision = size_intent(
            intent,
            equity=self.equity_usd,
            stats=LiquidityStats(
                traded_usd_window=getattr(row, "traded_usd_vol_window", 0.0) if row else 0.0,
                depth_usd=row.depth(intent.side) if row else 0.0,
            ),
            meta=self.meta[intent.coin],
            settings=self.settings,
            coin_exposure_usd=abs(self._notional(intent.coin)),
            total_exposure_usd=sum(abs(self._notional(c)) for c in self.positions),
        )
        if decision.reason is not None:
            self.result.blocked(
                now_ms=now_ms, coin=intent.coin, gate="sizing", reason=decision.reason
            )
            return None

        verdict = self.risk.check_entry(
            EntryRequest(
                coin=intent.coin,
                side=intent.side,
                notional_usd=decision.notional_usd,
                now_ms=now_ms,
            ),
            AccountView(
                equity=self.equity_usd,
                open_sides={
                    c: ("long" if p.size > 0 else "short") for c, p in self.positions.items()
                },
                coin_last_data_ms=self.coin_last_ms,
                global_last_data_ms=self.global_last_ms,
            ),
        )
        if not verdict.allowed:
            self.result.blocked(
                now_ms=now_ms, coin=intent.coin, gate="risk", reason=verdict.reason or "refused"
            )
            return None

        is_buy = intent.side == "long"
        px = self.touch(intent.coin, is_buy=is_buy)
        if px is None:
            log.warning("entry_unpriceable", coin=intent.coin)
            return None

        cloid = new_cloid()
        order = await self.orders.submit(
            OrderIntent(
                cloid=cloid,
                coin=intent.coin,
                is_buy=is_buy,
                px=px,
                size=float(decision.size),
                # Alo at the touch, per docs/STRATEGY_AND_RISK.md section 3. A high
                # urgency intent crosses instead: strategy/types.py says only high urgency
                # may take liquidity, and the cascade it is fading does not wait.
                tif="Ioc" if intent.urgency == "high" else "Alo",
                tag="entry",
            )
        )
        if order.status is OrderStatus.REJECTED:
            return None
        self.entry_intents[cloid] = intent
        return cloid

    def _notional(self, coin: str) -> float:
        """Signed USD exposure in one coin, marked to the book we have.

        Marked to mid rather than to entry: the caps are about what is at risk now, and a
        position that has moved against us is a larger one than the day it was opened.
        """
        position = self.positions.get(coin)
        if position is None:
            return 0.0
        book = self.books.get(coin)
        mark = (book.mid() if book else None) or position.entry_px
        return position.size * mark

    # --- what the venue tells us ---------------------------------------------

    async def on_fill(self, cloid: str, *, px: float, size: float, ts_ms: int) -> None:
        """An entry filled: bracket the position it opened.

        The bracket goes on immediately and at the venue, so the position is protected by
        something that outlives this process. A stop that only exists as a check inside a
        250ms tick is a stop that a crash removes.
        """
        intent = self.entry_intents.pop(cloid, None)
        if intent is None:
            return  # an exit leg, or an order from before this process
        self.fills.append((ts_ms, intent.coin, "entry", px, size))
        if intent.coin in self.brackets:
            return
        legs = await self.orders.submit_bracket(
            coin=intent.coin,
            is_long=intent.side == "long",
            size=size,
            tp_px=intent.target_px,
            sl_px=intent.stop_px,
        )
        self.brackets[intent.coin] = legs
        log.info(
            "bracket_placed",
            coin=intent.coin,
            tp=intent.target_px,
            sl=intent.stop_px,
            tp_cloid=legs.tp_cloid,
            sl_cloid=legs.sl_cloid,
        )

    # --- what the kill switch and /flat call ---------------------------------

    async def cancel(self, cloid: str) -> None:
        await self.orders.cancel(cloid)

    async def cancel_all(self) -> int:
        cloids = await self.open_order_cloids()
        for cloid in cloids:
            await self.orders.cancel(cloid)
        return len(cloids)

    async def close_position(self, coin: str, size: float = 0.0) -> None:
        """Flatten one coin with a reduce-only `Ioc`. What `/flat` and the kill call.

        `size` is what the caller believes is open and is deliberately ignored: the venue's
        own number is read here instead, because the kill switch's copy can be a fill old
        by the time this runs and an order for the wrong size is not a close.

        Reduce-only because the size we think we hold can be stale by a fill, and an order
        that could open the other way is not a close. The venue's refusal for a position
        that has already gone is "Reduce only order would increase position", measured.
        """
        position = self.positions.get(coin)
        if position is None or position.size == 0:
            return
        px = self.touch(coin, is_buy=position.size < 0)
        if px is None:
            book = self.books.get(coin)
            px = book.last_px if book else None
        if px is None:
            log.error("close_unpriceable", coin=coin)
            return
        slippage = self.settings.execution.taker_slippage_bps / 1e4
        # Cross by the configured bound so an Ioc actually takes rather than expiring.
        limit = px * (1 + slippage) if position.size < 0 else px * (1 - slippage)
        await self.orders.submit(
            OrderIntent(
                cloid=new_cloid(),
                coin=coin,
                is_buy=position.size < 0,
                px=limit,
                size=abs(position.size),
                tif="Ioc",
                reduce_only=True,
                tag="flat",
            )
        )

    async def flatten(self, *, now_ms: int = 0, coin: str | None = None) -> list[str]:
        """Cancel the resting orders and close every position, or just one coin.

        Returns the coins it closed, which is what `/flat` reports.
        """
        await self.cancel_all()
        coins = [coin] if coin else sorted(self.positions)
        closed = []
        for name in coins:
            if name in self.positions:
                await self.close_position(name)
                closed.append(name)
        return closed

    async def close_out(self, now_ms: int) -> None:
        """Shutdown: cancel resting orders, leave positions to the bracket at the venue.

        Paper flattens on the way out because a simulated position across a restart would
        be marked against a book the new process has not seen. A real one is different: it
        is protected by a bracket the venue holds, and closing it on every deploy would
        turn a restart into a realised loss.
        """
        await self.cancel_all()

    async def sync_orders(self, now_ms: int) -> None:
        """No-op: the venue tells us what happened to an order, we do not infer it.

        Paper needs this because the fill simulator owns the orders and the registry has
        to be diffed against it every tick. Here the user stream pushes every transition
        and reconciliation checks the result, so there is nothing to mirror. It exists
        because the loop calls it, and doing nothing is the honest implementation.
        """
        return None

    def inject_intent(
        self,
        *,
        coin: str,
        side: str,
        now_ms: int,
        target_bps: float = 30.0,
        stop_bps: float = 30.0,
        time_stop_s: float | None = None,
    ) -> Intent | None:
        """Build an intent from the current book and push it through the real path.

        This is how the Phase 5 drills put a resting order and an open position in place:
        a cascade may not happen on the afternoon the kill path needs testing, and an
        order placed by a probe script drills the script rather than the loop.

        Raises:
            PermissionError: the environment is mainnet, or `LIQFADE_LIVE_TRADING` is set.
                A drill command that can open a real money position is not a drill command,
                and the env is only one of the three gates that decide whether it can.
        """
        if self.settings.env == "mainnet":
            raise PermissionError(
                "inject_intent is refused on mainnet: it opens a position with no signal "
                "behind it. Run it on testnet."
            )
        if os.environ.get("LIQFADE_LIVE_TRADING"):
            raise PermissionError(
                "inject_intent is refused while LIQFADE_LIVE_TRADING is set: this process "
                "has been armed for live trading, whatever its env says. Unset it first."
            )
        book = self.books.get(coin)
        mid = book.mid() if book else None
        if mid is None:
            log.warning("inject_intent_unpriceable", coin=coin)
            return None

        direction = 1.0 if side == "long" else -1.0
        intent = Intent(
            coin=coin,
            side=side,  # type: ignore[arg-type]
            entry_px=mid,
            target_px=mid * (1.0 + direction * target_bps / 1e4),
            stop_px=mid * (1.0 - direction * stop_bps / 1e4),
            time_stop_s=time_stop_s or float(self.settings.intent.time_stop_s),
            urgency="normal",
            ts_ms=now_ms,
            explain={"injected": 1.0, "mid": mid},
        )
        log.warning("intent_injected", coin=coin, side=side, mid=mid)
        self.pending_injections.append((intent, now_ms))
        return intent

    async def drain_injections(self) -> None:
        """Place whatever `inject_intent` queued. Called by the loop after an inject.

        `inject_intent` is sync because the control backend calls it that way, and placing
        an order is not. Queuing keeps the drill on the real path rather than giving it a
        second, simpler one.
        """
        while self.pending_injections:
            intent, at_ms = self.pending_injections.pop(0)
            row = (
                self.features.row(intent.coin, at_ms)
                if intent.coin in self.features.coins
                else None
            )
            await self.place(intent, at_ms, row)

    def snapshot(self) -> tuple[list[PositionState], list[OrderState]]:
        """Positions and working orders in the control plane's shapes, for state.json."""
        orders = [
            order.to_state() for order in self.orders._orders.values() if order.status in LIVE
        ]
        positions = [
            PositionState(
                coin=coin,
                side="long" if position.size > 0 else "short",
                size=position.size,
                entry_px=position.entry_px,
                mark_px=(self.books.get(coin) or BookState()).mid() or position.entry_px,
                notional_usd=self._notional(coin),
                unrealized_pnl=position.unrealized_pnl,
                # The venue does not say when a position was opened, and inventing a
                # timestamp would make the panel's age column a lie. Zero reads as unknown.
                opened_ms=0,
            )
            for coin, position in sorted(self.positions.items())
        ]
        return positions, orders
