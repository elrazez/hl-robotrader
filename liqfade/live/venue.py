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

import contextlib
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
from liqfade.risk.engine import RiskEngine
from liqfade.risk.entry import gate_entry
from liqfade.sizing import LiquidityStats
from liqfade.strategy.types import Intent

log = get_logger(__name__)

__all__ = ["VenueSession"]

RECENT_BLOCKS = 256
# The tags an order carries when it closes a position rather than opening one. A fill on
# one of these is a round trip the risk engine has to hear about.
EXIT_TAGS = frozenset({"target", "stop", "flat"})
# The two legs of a bracket. They are resting orders like any other to the venue, and the
# only thing that tells them apart from an entry the sweep should cancel is this tag.
BRACKET_TAGS = frozenset({"target", "stop"})


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
        # cloid -> how much of that entry has filled, so a bracket covers the whole
        # position rather than whichever partial arrived first.
        self.filled: dict[str, float] = {}
        self.asset_rules: dict[str, Any] = {}
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

    async def load_asset_rules(self) -> int:
        """Read `szDecimals` and max leverage from the venue. Called before anything trades.

        The rounding rules are the venue's, not ours and not the backtest's.
        `_resolve_meta` answers 5 decimals and 3x for every coin, which is a replay's
        fallback and is what the testnet loop inherited: it sent ETH with five decimals
        against the four allowed and the venue answered "Order has invalid size".
        """
        rules = await self.reader.asset_meta()
        self.orders.sz_decimals = {coin: rule.sz_decimals for coin, rule in rules.items()}
        self.asset_rules = rules
        log.info("asset_rules_loaded", assets=len(rules))
        return len(rules)

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

    async def venue_state(self) -> tuple[dict[str, float], list[str]]:
        """What Hyperliquid holds right now: positions, and the cloids of every open order.

        The kill switch's pass criterion and its work list. Read fresh from the venue
        rather than from the local mirror, because every failure this exists for was the
        loop trusting its own bookkeeping over the account: a kill that closed what it
        remembered, and one that cancelled only the orders in its registry.

        Trigger orders are included. They live in `frontendOpenOrders` alone, so a sweep
        reading `openOrders` leaves a bracket resting behind a closed position.
        """
        account = await self.reader.account(self.address)
        orders = await self.reader.open_orders(self.address)
        triggers = await self.reader.trigger_orders(self.address)
        cloids = [o.cloid for o in orders if o.cloid] + [t.cloid for t in triggers if t.cloid]
        return {coin: p.size for coin, p in account.positions.items()}, cloids

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
        # Logged, not silent. An intent that produces no order and says nothing is the
        # 05b lesson: to an owner watching, a refused entry and a strategy finding nothing
        # look identical.
        if intent.coin in self.positions:
            log.info("entry_skipped", coin=intent.coin, reason="position already open")
            return None
        if any(
            o.coin == intent.coin and o.intent.tag == "entry"
            for o in await self.orders.open_orders()
        ):
            log.info("entry_skipped", coin=intent.coin, reason="entry already working")
            return None

        if intent.coin not in self.orders.sz_decimals:
            # No rules for this coin means no order. `_resolve_meta` would answer 5 for
            # anything asked of it, and a guessed grid is refused by the venue with
            # "Order has invalid size" at best and filled at the wrong size at worst.
            log.error("no_asset_rules", coin=intent.coin)
            return None
        if intent.coin not in self.meta:
            self.meta = _resolve_meta(self.meta, [intent.coin], self.settings)

        is_buy = intent.side == "long"
        # `Alo` at the touch, `Ioc` through it. strategy/types.py: only high urgency may
        # take liquidity, and an Ioc priced at its own side of the book cannot match
        # anything. Drilled on testnet: "Order could not immediately match against any
        # resting orders", which would have refused every urgent entry, meaning the loop
        # took the trades the strategy considers least urgent and refused the rest.
        crossing = intent.urgency == "high"
        # Priced before the gate rather than after it, so an intent with no book to rest
        # against never reaches `gate_entry`: the gate records the entry it allows, and
        # recording one for an order that was then never sent would tighten the cluster
        # cap on a trade that did not happen.
        px = self.touch(intent.coin, is_buy=not is_buy if crossing else is_buy)
        if px is None:
            log.warning("entry_unpriceable", coin=intent.coin)
            return None

        gated = gate_entry(
            intent,
            now_ms=now_ms,
            equity=self.equity_usd,
            stats=LiquidityStats(
                traded_usd_window=getattr(row, "traded_usd_vol_window", 0.0) if row else 0.0,
                depth_usd=row.depth(intent.side) if row else 0.0,
            ),
            meta=self.meta[intent.coin],
            settings=self.settings,
            risk=self.risk,
            coin_exposure_usd=abs(self._notional(intent.coin)),
            total_exposure_usd=sum(abs(self._notional(c)) for c in self.positions),
            open_sides={c: ("long" if p.size > 0 else "short") for c, p in self.positions.items()},
            coin_last_data_ms=self.coin_last_ms,
            global_last_data_ms=self.global_last_ms,
        )
        if gated.reason is not None:
            self.result.blocked(
                now_ms=now_ms, coin=intent.coin, gate=gated.gate or "risk", reason=gated.reason
            )
            return None

        if crossing:
            bound = self.settings.execution.taker_slippage_bps / 1e4
            px = px * (1 + bound) if is_buy else px * (1 - bound)

        cloid = new_cloid()
        # Recorded before the submit, not after. An Ioc fills at the venue in a few hundred
        # milliseconds and the user stream pushes the fill back while `submit` is still
        # waiting on its own response, so an intent registered afterwards arrives too late
        # and the fill finds nothing to bracket. Drilled on testnet: two positions with no
        # stop behind them. The faster the venue, the more likely.
        self.entry_intents[cloid] = intent
        order = await self.orders.submit(
            OrderIntent(
                cloid=cloid,
                coin=intent.coin,
                is_buy=is_buy,
                px=px,
                size=gated.size,
                # Alo at the touch, per docs/STRATEGY_AND_RISK.md section 3. A high
                # urgency intent crosses instead: strategy/types.py says only high urgency
                # may take liquidity, and the cascade it is fading does not wait.
                tif="Ioc" if crossing else "Alo",
                tag="entry",
            )
        )
        if order.status is OrderStatus.REJECTED:
            self.entry_intents.pop(cloid, None)
            # Counted, because enough refusals in ten minutes is a reason to stop trying:
            # "Insufficient margin" or "Order has invalid size" repeating is a sizing bug
            # and every retry of it spends address budget the kill path may need.
            self.risk.on_reject(now_ms)
            return None
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
        """An entry filled, in whole or in part: bracket everything it has opened so far.

        The bracket goes on immediately and at the venue, so the position is protected by
        something that outlives this process. A stop that only exists as a check inside a
        250ms tick is a stop that a crash removes.

        **Every fill re-brackets.** An entry that fills in four parts used to get a bracket
        for the first part and nothing for the rest, because the coin already had one:
        drilled on testnet 2026-09-08, a 0.0076 position wearing a 0.0019 stop. A partial
        bracket is the failure `execution/tpsl.py` warns about, since the position reads as
        protected. The old legs are cancelled before the new ones go on, or the venue holds
        two brackets for one position and the second to trigger closes a flat account.
        """
        intent = self.entry_intents.get(cloid)
        if intent is None:
            await self._on_exit_fill(cloid, ts_ms)
            return
        self.fills.append((ts_ms, intent.coin, "entry", px, size))
        self.filled[cloid] = self.filled.get(cloid, 0.0) + size
        total = self.filled[cloid]

        previous = self.brackets.pop(intent.coin, None)
        if previous is not None:
            await self._cancel_legs(previous)

        legs = await self._bracket(intent, total)
        if legs is None:
            await self._abandon_unbracketed(cloid, intent, total, ts_ms)
            return
        self.brackets[intent.coin] = legs
        log.info(
            "bracket_placed",
            coin=intent.coin,
            size=total,
            replaced=previous is not None,
            tp=intent.target_px,
            sl=intent.stop_px,
            tp_cloid=legs.tp_cloid,
            sl_cloid=legs.sl_cloid,
        )

    async def _bracket(self, intent: Intent, size: float) -> TpslLegs | None:
        """Ask the venue for the bracket, once and then once more. None if it will not.

        One retry, because the two ways this fails are worth one more attempt each: a
        refusal that came from a size or a price the venue disliked for a moment, and a
        transport failure where the group may equally well have landed. A duplicate bracket
        from the second case is reduce-only and dies with the position, which is the
        cheaper of the two ways to be wrong.
        """
        for attempt in (0, 1):
            try:
                legs = await self.orders.submit_bracket(
                    coin=intent.coin,
                    is_long=intent.side == "long",
                    size=size,
                    tp_px=intent.target_px,
                    sl_px=intent.stop_px,
                )
            except Exception as exc:
                # Including `TransientTransportError`, which used to propagate into the
                # user stream's fill hook and be logged there as a failed hook, leaving a
                # position open with nothing on it and nothing said about it.
                log.error(
                    "bracket_attempt_failed",
                    coin=intent.coin,
                    attempt=attempt,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            if legs is not None:
                return legs
            log.error("bracket_refused", coin=intent.coin, attempt=attempt, size=size)
        return None

    async def _abandon_unbracketed(
        self, cloid: str, intent: Intent, size: float, ts_ms: int
    ) -> None:
        """Close a position the venue would not let us protect, and say why.

        A position without a confirmed bracket is not held. The alternative is carrying an
        unattended position with no stop through whatever the cascade does next, which is
        the one thing Phase 6 running unattended cannot survive.

        The rest of the entry is cancelled first: it would otherwise keep filling into a
        coin we have just decided not to be in. The reason goes to the counters rather than
        straight to Telegram because the loop is what drains them, and `report_risk_blocks`
        posts `bracket_failed` like any other refusal.
        """
        log.error("position_unbracketed", coin=intent.coin, size=size)
        self.result.blocked(now_ms=ts_ms, coin=intent.coin, gate="risk", reason="bracket_failed")
        with contextlib.suppress(KeyError):
            await self.orders.cancel(cloid)
        self.entry_intents.pop(cloid, None)
        self.filled.pop(cloid, None)
        await self.close_position(intent.coin, size if intent.side == "long" else -size)

    async def _on_exit_fill(self, cloid: str, ts_ms: int) -> None:
        """A bracket leg or a flattening order filled: one round trip is closed.

        This is the only thing on the venue path that moves the day's realised PnL, the
        losing streak and the per-coin cooldown, and until it existed none of those three
        limits could fire on testnet or on mainnet however the day went.

        A partial exit is not a round trip and is left alone, exactly as the backtest's
        `_booked` leaves one: the position is still open and the venue has not finished
        telling us what it earned.
        """
        try:
            order = self.orders.get(cloid)
        except KeyError:
            return  # an order from before this process, or somebody else's
        if order.intent.tag not in EXIT_TAGS or order.status is not OrderStatus.FILLED:
            return
        self.fills.append((ts_ms, order.coin, order.intent.tag, order.avg_px, order.filled))
        self.portfolio.trades.append(
            VenueTrade(coin=order.coin, net_pnl=order.closed_pnl, ts_ms=ts_ms)
        )
        self.risk.on_exit(order.coin, order.closed_pnl, ts_ms)
        log.info(
            "round_trip_closed",
            coin=order.coin,
            tag=order.intent.tag,
            net_pnl=order.closed_pnl,
            size=order.filled,
        )

    def on_reconnect(self, now_ms: int) -> None:
        """A critical feed came back. Enough of them in ten minutes halts entries.

        Sync, because the stream's own loop is what calls it through the loop's reconnect
        handler, and counting a reconnect must not wait on anything.
        """
        self.risk.on_reconnect(now_ms)

    # --- what the kill switch and /flat call ---------------------------------

    async def cancel(self, cloid: str) -> None:
        await self.orders.cancel(cloid)

    async def cancel_all(self) -> int:
        """Cancel every resting order except the brackets holding open positions.

        A bracket leg rests at the venue like any other order, so a sweep over "everything
        open" takes the stop off every position we hold: `close_out` on a deploy stripped
        the protection it had just promised to leave in place, and `/flat BTC` cancelled
        ETH's stop on its way past. A leg comes off with its own position and not before,
        which is `_cancel_legs`.
        """
        cloids = [
            order.cloid
            for order in await self.orders.open_orders()
            if order.intent.tag not in BRACKET_TAGS
        ]
        for cloid in cloids:
            await self.orders.cancel(cloid)
        return len(cloids)

    async def _cancel_legs(self, legs: TpslLegs) -> None:
        """Take one bracket off, after the position it protected has been closed."""
        for leg in (legs.tp_cloid, legs.sl_cloid):
            with contextlib.suppress(KeyError):
                await self.orders.cancel(leg)

    async def close_position(self, coin: str, size: float = 0.0) -> None:
        """Flatten one coin with a reduce-only `Ioc`. What `/flat` and the kill call.

        `size` is the caller's signed size and wins over the local book. The kill passes
        the venue's own number, and the mirror can be a whole reconcile behind: reading it
        here made this a no-op for a position opened since the last reconcile, so the kill
        reported "closed 1" and submitted nothing at all.

        Reduce-only because the size we think we hold can be stale by a fill, and an order
        that could open the other way is not a close. The venue's refusal for a position
        that has already gone is "Reduce only order would increase position", measured.
        """
        known = self.positions.get(coin)
        signed = size if size else (known.size if known else 0.0)
        if signed == 0.0:
            log.info("close_skipped", coin=coin, reason="no position")
            return
        # Cross into the far side: closing a long sells into the bid, closing a short
        # buys from the ask. Taking our own side and shaving the slippage off leaves
        # the order inside the spread on anything but the tightest book, and an Ioc
        # that cannot match is cancelled rather than filled.
        px = self.touch(coin, is_buy=signed > 0)
        if px is None:
            book = self.books.get(coin)
            px = book.last_px if book else None
        if px is None:
            log.error("close_unpriceable", coin=coin)
            return
        slippage = self.settings.execution.taker_slippage_bps / 1e4
        # Cross by the configured bound so an Ioc actually takes rather than expiring.
        limit = px * (1 + slippage) if signed < 0 else px * (1 - slippage)
        await self.orders.submit(
            OrderIntent(
                cloid=new_cloid(),
                coin=coin,
                is_buy=signed < 0,
                px=limit,
                size=abs(signed),
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
                # After the close and never before it: a bracket cancelled first leaves
                # the position unprotected for as long as the closing Ioc takes, and an
                # Ioc that cannot match is cancelled rather than filled.
                legs = self.brackets.pop(name, None)
                if legs is not None:
                    await self._cancel_legs(legs)
        # Re-read rather than assume. The venue stays the truth, and without this the
        # mirror keeps a position we have just closed until the next reconcile, up to
        # execution.reconcile_s later, refusing every new entry in that coin meanwhile.
        with contextlib.suppress(Exception):
            self.sync_account(await self.reader.account(self.address))
        return closed

    async def close_out(self, now_ms: int) -> None:
        """Shutdown: cancel resting entries, leave positions to the bracket at the venue.

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
        urgency: str = "normal",
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
            # A drill that has to open a position now asks for `high`, which crosses.
            # `normal` rests at the touch and may wait all afternoon for a fill, which
            # drills patience rather than the kill path.
            urgency=urgency,  # type: ignore[arg-type]
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
