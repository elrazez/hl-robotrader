"""Paper mode: the backtest engine, driven by a live clock instead of a Parquet replay.

The Phase 5 DoD asks whether paper results land in a plausible band of the backtest's for
the same days. The only way that question means anything is if paper and backtest are the
same code, so this module reuses `backtest/engine.py`'s tick functions verbatim rather
than reimplementing entries, exits and the fill model against a live feed. What changes is
where the events come from and what drives the clock; the decisions are identical, and a
divergence in paper is therefore a divergence in the engine and worth chasing.

Those tick functions are private to `backtest/engine.py` and are imported here anyway. The
alternative is a second implementation of the entry, exit and repricing rules that would
drift from the first within a week, and a drifted twin is a worse thing to own than a
private import. If they ever need to change shape, they change in one place.

**Nothing here can place a real order.** `PaperTransport` is the only transport: it hands
back an order id and touches nothing. The positions live in a `Portfolio` and the fills
come from `backtest/fills.py`.

The orders are still real in every way that costs nothing. They go through the same
`OrderManager` a testnet order goes through, so each one is rounded onto the venue's grid,
walks SUBMITTED to ACKED to PARTIAL to FILLED or CANCELED, is refused if the kill flag is
there, and writes both the orders table and the fills table of `data/orders.sqlite`. The
panel, `/status` and reconciliation therefore see the rows they will see on testnet rather
than a subset of them written by hand.

`inject_intent` exists for drills, because a cross-venue cascade may not happen on the
afternoon you want to test the plumbing. It builds an intent from the current book and
pushes it through sizing, the risk gate and the order path exactly as the strategy's own
intents go, and it is refused outright when the environment is mainnet **or** when
`LIQFADE_LIVE_TRADING` is set. The env is only one of the three gates in CLAUDE.md rule 3,
and a box that has been armed is not a box to open a position on with no signal behind it,
whatever its env file says.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from liqfade.backtest.data import L2, AssetCtx, Bbo, Liq, Trade
from liqfade.backtest.engine import (
    BacktestResult,
    _CloidCounter,
    _close_out,
    _CoinState,
    _on_event,
    _on_tick,
    _resolve_meta,
    _take_exit,
)
from liqfade.backtest.fills import OrderKind, SimOrder
from liqfade.backtest.portfolio import Portfolio
from liqfade.config.models import Settings
from liqfade.control.protocol import OrderState, PositionState
from liqfade.control.protocol import now_ms as wall_ms
from liqfade.execution.orders import (
    OrderIntent,
    OrderManager,
    OrderStatus,
    OrderStore,
    new_cloid,
)
from liqfade.live.features import LiveFeatureEngine
from liqfade.log import get_logger
from liqfade.risk.engine import RiskEngine
from liqfade.strategy.engine import StrategyEngine
from liqfade.strategy.types import AssetMeta, FeatureRow, Intent, Side

log = get_logger(__name__)

__all__ = ["PaperSession"]


class _HexCloids(_CloidCounter):
    """Real 16 byte cloids for paper orders, not the replay's counter.

    A paper order is written to the same registry a testnet order is and is read by the
    same panel and the same reconciliation. Giving it a cloid the venue would reject makes
    those rows a different kind of thing, and the point of paper mode is that they are not.
    """

    def next(self) -> str:
        return new_cloid()


class PaperTransport:
    """The `OrderTransport` paper runs against. Answers, and does nothing else.

    The order it is asked to place already exists as a `SimOrder` in the engine, which is
    what fills it. This exists so the manager's state machine, its persistence and its
    idempotence are the ones paper exercises, rather than a second implementation of them
    that would drift from the first.
    """

    def __init__(self) -> None:
        self.oid = 0
        self.placed: list[OrderIntent] = []
        self.cancelled: list[tuple[str, str]] = []

    async def place(self, intent: OrderIntent) -> int:
        self.placed.append(intent)
        self.oid += 1
        return self.oid

    async def cancel(self, coin: str, cloid: str) -> None:
        self.cancelled.append((coin, cloid))


class _RecordingStrategy(StrategyEngine):
    """The real engine, remembering the intents of the last tick so they can be posted.

    `_on_tick` places intents itself and returns nothing, and Telegram has to see every
    one of them including those the risk gate then refuses. Recording them here is six
    lines against copying the tick function to add a hook.

    It is also where a pause is applied. `allow_intents` False still runs the detectors
    and then throws their intents away, so the pause costs nothing but the new entries:
    skipping the tick instead would leave every cascade detector blind for the length of
    the pause and make a resume a cold start.
    """

    def __init__(self, settings: Settings, meta: Mapping[str, AssetMeta]) -> None:
        super().__init__(settings, meta)
        self.last_intents: list[Intent] = []
        self.allow_intents = True
        self.dropped_intents = 0

    def on_tick(self, now_ms: int, features_by_coin: Mapping[str, FeatureRow]) -> list[Intent]:
        intents = super().on_tick(now_ms, features_by_coin)
        if not self.allow_intents and intents:
            self.dropped_intents += len(intents)
            log.warning(
                "intent_dropped_while_paused",
                coins=sorted(intent.coin for intent in intents),
                count=len(intents),
                dropped_total=self.dropped_intents,
            )
            intents = []
        self.last_intents = intents
        return intents


class PaperSession:
    """One paper trading session. Owns the book, the portfolio and the order registry."""

    def __init__(
        self,
        settings: Settings,
        *,
        store: OrderStore | None,
        meta: Mapping[str, AssetMeta],
        starting_equity: float,
        risk_state_path: Any = None,
    ) -> None:
        """
        Args:
            settings: resolved config. `cascade.trigger` decides what fires an intent.
            store: the sqlite order registry, or None for a session that persists nothing.
            meta: per-coin venue metadata. Coins absent from it get the conservative
                defaults `_resolve_meta` uses, which are the ones that constrain sizing most.
            starting_equity: paper's opening balance, USD. From the run config, never
                hardcoded: CLAUDE.md forbids a dollar amount in the package.
            risk_state_path: where risk state persists. None keeps it in memory.
        """
        self.settings = settings
        self.store = store
        self.features = LiveFeatureEngine(settings)
        self.meta = _resolve_meta(dict(meta), sorted(meta), settings)
        self.transport = PaperTransport()
        # The real manager, with the real kill check. A paper order that skipped it would
        # be drilling something other than the thing that runs on the box.
        self.orders = (
            OrderManager(
                store,
                self.transport,
                sz_decimals=self._sz_decimals(),
                killed=lambda: settings.kill_switch_present,
                retries=settings.execution.transient_retries,
            )
            if store is not None
            else None
        )
        self.strategy = _RecordingStrategy(settings, self.meta)
        self.portfolio = Portfolio(settings, starting_equity=starting_equity)
        # The real gate, with the real kill switch: a paper loop that ignored the kill file
        # would be drilling something other than the thing that runs on the box.
        self.risk = RiskEngine(settings, path=risk_state_path)
        self.state: dict[str, _CoinState] = {}
        self.cloid = _HexCloids()
        self.result = BacktestResult(
            trades=self.portfolio.trades,
            equity_curve=[],
            portfolio=self.portfolio,
            strategy=self.strategy,
            risk_blocks={},
            rejections=[],
        )
        self.intents: list[Intent] = []
        self.fills: list[tuple[int, str, str, float, float]] = []
        self._known_orders: dict[str, OrderStatus] = {}
        self._seen_orders: dict[str, SimOrder] = {}
        self._recorded_fill: dict[str, float] = {}
        self._trades_seen = 0
        self._positions_seen: set[str] = set()

    async def _record_taker_exit(self, trade: Any, now_ms: int) -> None:
        """One crossing exit into the registry: submitted, acked, filled, all at once."""
        if self.orders is None:
            return
        cloid = new_cloid()
        intent = OrderIntent(
            cloid=cloid,
            coin=trade.coin,
            # A long position exits by selling. `trade.side` is the position's side.
            is_buy=trade.side != "long",
            px=trade.exit_px,
            size=trade.size,
            tif="Ioc",
            reduce_only=True,
            tag=trade.exit_reason,
        )
        placed = await self.orders.submit(intent)
        if placed.status is OrderStatus.REJECTED:
            return
        self._known_orders[cloid] = placed.status
        updated = await self.orders.record_fill(
            cloid,
            size=placed.intent.size,
            px=placed.intent.px,
            ts_ms=trade.exit_ms or now_ms,
            fee=placed.intent.px * placed.intent.size * self.settings.backtest.taker_fee_frac,
            maker=False,
        )
        self._known_orders[cloid] = updated.status

    def _sz_decimals(self) -> dict[str, int]:
        return {coin: asset.sz_decimals for coin, asset in self.meta.items()}

    # --- feeds ---------------------------------------------------------------

    def ingest(self, table: str, venue: str, row: Mapping[str, Any]) -> None:
        """One parsed row from any stream. Feeds the features and the book alike.

        Called from the WebSocket receive loop, so it must not raise: an exception here
        takes the connection down and the loop stops seeing the market.
        """
        try:
            self.features.ingest(table, venue, row)
            event = _as_event(table, venue, row)
            if event is not None:
                _on_event(event, self.state, self.portfolio, self.result, self.settings)
        except Exception as exc:
            log.warning(
                "paper_ingest_failed",
                table=table,
                venue=venue,
                error=f"{type(exc).__name__}: {exc}",
            )

    # --- the clock -----------------------------------------------------------

    async def tick(self, now_ms: int, *, allow_entries: bool = True) -> list[Intent]:
        """One decision tick. Returns the intents the strategy emitted, for Telegram.

        Args:
            now_ms: the decision timestamp.
            allow_entries: False while the loop is paused. The tick still runs in full,
                because `_on_tick` is the only path to the stop, the time stop and the
                entry expiry; only the new intents are dropped. A pause stops new risk,
                it does not abandon the risk already on the book.
        """
        self.features.advance(now_ms)
        rows = {coin: self.features.row(coin, now_ms) for coin in self.features.coins}
        self.strategy.allow_intents = allow_entries
        self.strategy.last_intents = []
        _on_tick(
            now_ms,
            self.state,
            rows,
            self.portfolio,
            self.strategy,
            self.risk,
            self.result,
            self.meta,
            self.settings,
            self.cloid,
        )
        intents = list(self.strategy.last_intents)
        self.intents.extend(intents)
        await self.sync_orders(now_ms)
        await self._note_trades(now_ms)
        return intents

    # --- drills --------------------------------------------------------------

    def inject_intent(
        self,
        *,
        coin: str,
        side: Side,
        now_ms: int,
        target_bps: float = 30.0,
        stop_bps: float = 30.0,
        time_stop_s: float | None = None,
    ) -> Intent | None:
        """Build an intent from the current book and push it through the real path.

        For drills: a cross-venue cascade may not happen on the afternoon the plumbing
        needs testing, and an intent that skipped sizing and the risk gate would drill
        neither. Returns None when the coin has no price to build one from, because an
        invented entry price is an invented drill.

        Raises:
            PermissionError: the environment is mainnet, or LIQFADE_LIVE_TRADING is set.
                A drill command that can open a real position is not a drill command, and
                the env is only one of the three gates that decide whether it can.
        """
        if self.settings.env == "mainnet":
            raise PermissionError(
                "inject_intent is refused on mainnet: it opens a position with no signal "
                "behind it. Run it in paper or on testnet."
            )
        if os.environ.get("LIQFADE_LIVE_TRADING"):
            raise PermissionError(
                "inject_intent is refused while LIQFADE_LIVE_TRADING is set: this process "
                "has been armed for live trading, whatever its env says. Unset it first."
            )
        coin_state = self.state.get(coin)
        mid = coin_state.book.mid() if coin_state else None
        if mid is None:
            log.warning("inject_intent_unpriceable", coin=coin)
            return None

        direction = 1.0 if side == "long" else -1.0
        intent = Intent(
            coin=coin,
            side=side,
            entry_px=mid,
            target_px=mid * (1.0 + direction * target_bps / 1e4),
            stop_px=mid * (1.0 - direction * stop_bps / 1e4),
            time_stop_s=time_stop_s or float(self.settings.intent.time_stop_s),
            urgency="normal",
            ts_ms=now_ms,
            explain={"injected": 1.0, "mid": mid},
        )
        log.warning("intent_injected", coin=coin, side=side, mid=mid, entry_px=intent.entry_px)
        self._place(intent, now_ms)
        return intent

    def _place(self, intent: Intent, now_ms: int) -> None:
        """Push one intent through sizing, the risk gate and the order path."""
        from liqfade.backtest.engine import _place_entry

        mids = {coin: coin_state.book.mid() for coin, coin_state in self.state.items()}
        rows = {intent.coin: self.features.row(intent.coin, now_ms)}
        if intent.coin not in self.meta:
            self.meta = _resolve_meta(self.meta, [intent.coin], self.settings)
            if self.orders is not None:
                # `_resolve_meta` returns a new dict, so the manager's copy has to be told.
                self.orders.sz_decimals = self._sz_decimals()
        _place_entry(
            intent,
            now_ms,
            self.state,
            rows,
            mids,
            self.portfolio,
            self.risk,
            self.result,
            self.meta,
            self.settings,
            self.cloid,
        )

    # --- what the kill switch and /flat call ---------------------------------

    async def open_order_cloids(self) -> list[str]:
        """Every simulated order that could still fill."""
        cloids = []
        for coin_state in self.state.values():
            for order in (coin_state.entry, coin_state.exit_order):
                if order is not None and order.remaining > 0:
                    cloids.append(order.cloid)
        return sorted(cloids)

    async def cancel(self, cloid: str) -> None:
        """Cancel one simulated order by cloid, and mark the registry row cancelled."""
        for coin_state in self.state.values():
            for name in ("entry", "exit_order"):
                order = getattr(coin_state, name)
                if order is not None and order.cloid == cloid:
                    setattr(coin_state, name, None)
                    if name == "entry":
                        coin_state.intent = None
                    self._seen_orders.pop(cloid, None)
                    if self.orders is not None and cloid in self._known_orders:
                        cancelled = await self.orders.cancel(cloid)
                        self._known_orders[cloid] = cancelled.status
                    return

    async def cancel_all(self) -> int:
        cloids = await self.open_order_cloids()
        for cloid in cloids:
            await self.cancel(cloid)
        return len(cloids)

    async def open_positions(self) -> dict[str, float]:
        """Coin to signed size, positive long. The shape reconciliation compares against."""
        return {
            coin: position.size if position.is_long else -position.size
            for coin, position in self.portfolio.positions.items()
        }

    async def close_position(self, coin: str, size: float) -> None:
        """Close one coin with a taker order, which is what an IOC reduce-only does."""
        await self.flatten(coin=coin, now_ms=wall_ms())

    async def flatten(self, *, now_ms: int, coin: str | None = None) -> list[str]:
        """Cancel the resting orders and cross out of every position, or just one coin.

        Returns the coins it closed. Uses the engine's own exit path, so a paper flatten
        is priced by the same book walk a stop is.
        """
        await self.cancel_all()
        coins = [coin] if coin else sorted(self.portfolio.positions)
        closed = []
        for name in coins:
            coin_state = self.state.get(name)
            position = self.portfolio.positions.get(name)
            if coin_state is None or position is None:
                continue
            mid = coin_state.book.mid()
            if mid is None:
                log.error("flatten_unpriceable", coin=name)
                continue
            _take_exit(
                name,
                coin_state,
                asked_px=mid,
                mid=mid,
                reason="flat",
                now_ms=now_ms,
                portfolio=self.portfolio,
                settings=self.settings,
                cloid=self.cloid,
            )
            if name not in self.portfolio.positions:
                closed.append(name)
        await self._note_trades(now_ms)
        return closed

    async def aclose(self) -> None:
        """Release the registry connection. aiosqlite runs a worker thread per connection."""
        if self.store is not None:
            await self.store.close()

    def close_out(self, end_ms: int) -> None:
        """Flatten everything at shutdown, the way a backtest closes its final window."""
        _close_out(end_ms, self.state, self.portfolio, self.result, self.settings)

    # --- reporting -----------------------------------------------------------

    def equity(self) -> float:
        mids = {coin: coin_state.book.mid() for coin, coin_state in self.state.items()}
        return self.portfolio.equity(mids)

    def snapshot(self) -> tuple[list[PositionState], list[OrderState]]:
        """Positions and working orders in the control plane's shapes, for state.json."""
        positions = []
        for coin, position in sorted(self.portfolio.positions.items()):
            coin_state = self.state.get(coin)
            mid = coin_state.book.mid() if coin_state else None
            mark = mid if mid is not None else position.entry_px
            positions.append(
                PositionState(
                    coin=coin,
                    side=position.side,
                    size=position.size if position.is_long else -position.size,
                    entry_px=position.entry_px,
                    mark_px=mark,
                    notional_usd=position.notional(mark),
                    unrealized_pnl=position.unrealized(mark) - position.fees_usd,
                    opened_ms=position.opened_ms,
                )
            )
        orders = []
        for coin, coin_state in sorted(self.state.items()):
            for order in (coin_state.entry, coin_state.exit_order):
                if order is None or order.remaining <= 0:
                    continue
                orders.append(
                    OrderState(
                        cloid=order.cloid,
                        oid=None,
                        coin=coin,
                        side="buy" if order.is_buy else "sell",
                        px=order.px,
                        size=order.size,
                        filled=order.filled,
                        tif=order.kind.value,
                        reduce_only=order.reduce_only,
                        status=str(OrderStatus.PARTIAL if order.filled else OrderStatus.ACKED),
                        submitted_ms=order.visible_ms,
                    )
                )
        return positions, orders

    # --- the registry mirror -------------------------------------------------

    async def sync_orders(self, now_ms: int) -> None:
        """Push the engine's working orders through the order manager, transitions and all.

        The engine owns the simulated orders and fills them; the manager owns the registry
        and the state machine. Each tick is a diff: an order the manager has never seen is
        submitted, an order whose filled size grew is a fill, and an order that left the
        coin state without being cancelled filled the rest of the way, because the engine
        drops an order the moment nothing is left of it. The `SimOrder` is kept for exactly
        that last case, so the closing fill carries its real size.
        """
        if self.orders is None:
            return
        live: dict[str, SimOrder] = {}
        for coin_state in self.state.values():
            for order in (coin_state.entry, coin_state.exit_order):
                if order is not None:
                    live[order.cloid] = order

        for cloid, order in live.items():
            if cloid not in self._known_orders:
                await self._submit(order)
            await self._book_fill(order, now_ms)

        self._seen_orders.update(live)
        for cloid in list(self._known_orders):
            if cloid in live or self._known_orders[cloid] in {
                OrderStatus.FILLED,
                OrderStatus.CANCELED,
                OrderStatus.REJECTED,
            }:
                continue
            gone = self._seen_orders.pop(cloid, None)
            if gone is not None:
                gone.filled = gone.size
                await self._book_fill(gone, now_ms)

    async def _submit(self, order: SimOrder) -> None:
        """One simulated order into the registry, through the real submit path."""
        assert self.orders is not None
        placed = await self.orders.submit(
            OrderIntent(
                cloid=order.cloid,
                coin=order.coin,
                is_buy=order.is_buy,
                px=order.px,
                size=order.size,
                tif=order.kind.value,
                reduce_only=order.reduce_only,
                tag=order.tag,
            )
        )
        self._known_orders[order.cloid] = placed.status
        self._recorded_fill.setdefault(order.cloid, 0.0)
        log.info(
            "paper_order",
            cloid=order.cloid,
            coin=order.coin,
            status=str(placed.status),
            px=order.px,
            size=order.size,
            tag=order.tag,
            reduce_only=order.reduce_only,
        )

    async def _book_fill(self, order: SimOrder, now_ms: int) -> None:
        """Record whatever filled since the last look, if anything did.

        The fee is the same one the portfolio charged, computed from the same config
        fractions, so the registry's fills add up to the trade log's costs. Maker or taker
        is the order kind: an `Alo` rested and an `Ioc` crossed.
        """
        assert self.orders is not None
        if self._known_orders.get(order.cloid) in {OrderStatus.FILLED, OrderStatus.CANCELED}:
            return
        delta = order.filled - self._recorded_fill.get(order.cloid, 0.0)
        if delta <= 0:
            return
        self._recorded_fill[order.cloid] = order.filled
        maker = order.kind is not OrderKind.IOC
        costs = self.settings.backtest
        fee_frac = costs.maker_fee_frac if maker else costs.taker_fee_frac
        updated = await self.orders.record_fill(
            order.cloid,
            size=delta,
            px=order.px,
            ts_ms=now_ms,
            fee=order.px * delta * fee_frac,
            maker=maker,
        )
        self._known_orders[order.cloid] = updated.status

    async def _note_trades(self, now_ms: int) -> None:
        """Record fills and closed trades since the last call, for Telegram and counters.

        A crossing exit (a stop, a time stop, a flat) is built, filled and discarded inside
        the engine and never rests in the coin state, so `sync_orders` cannot see it. It is
        still an order the venue would have received and a fill the panel has to show, so
        it is written here from the closed trade. The resting target exit is not: that one
        rests, and `sync_orders` already has it.
        """
        for coin, position in self.portfolio.positions.items():
            if coin not in self._positions_seen:
                self._positions_seen.add(coin)
                self.fills.append(
                    (position.opened_ms, coin, "entry", position.entry_px, position.size)
                )
                log.info(
                    "paper_fill",
                    coin=coin,
                    leg="entry",
                    px=position.entry_px,
                    size=position.size,
                    side=position.side,
                )
        self._positions_seen &= set(self.portfolio.positions)

        while self._trades_seen < len(self.portfolio.trades):
            trade = self.portfolio.trades[self._trades_seen]
            self._trades_seen += 1
            self.fills.append(
                (trade.exit_ms, trade.coin, trade.exit_reason, trade.exit_px, trade.size)
            )
            if trade.exit_reason != "target":
                await self._record_taker_exit(trade, now_ms)
            log.info(
                "paper_trade",
                coin=trade.coin,
                reason=trade.exit_reason,
                entry_px=trade.entry_px,
                exit_px=trade.exit_px,
                size=trade.size,
                net_pnl=trade.net_pnl,
                fees=trade.fees_usd,
                hold_s=trade.hold_s,
            )


def _as_event(table: str, venue: str, row: Mapping[str, Any]) -> Any:
    """Turn one canonical row into the engine's event, or None where there is none.

    L2 arrives as one row per level and the engine wants an assembled snapshot, so levels
    are folded into a one-level book here; the full ladder is what `l2Book` gives and the
    recorder's `bbo` stream already carries the touch. A taker walk that runs out of levels
    degrades on its own, which `backtest/fills.py` documents.
    """
    known = int(row.get("rx_ms") or row.get("ts_ms") or 0)
    coin = str(row.get("coin") or "")
    if not coin or known <= 0:
        return None
    match table:
        case "trades":
            return Trade(
                ts_ms=known,
                coin=coin,
                px=float(row["px"]),
                sz=float(row["sz"]),
                side=str(row["side"]),
            )
        case "bbo":
            return Bbo(
                ts_ms=known,
                coin=coin,
                bid_px=float(row["bid_px"]),
                bid_sz=float(row["bid_sz"]),
                ask_px=float(row["ask_px"]),
                ask_sz=float(row["ask_sz"]),
            )
        case "l2":
            if int(row.get("level", 0)) != 0:
                return None
            return L2(
                ts_ms=known,
                coin=coin,
                bids=((float(row["bid_px"]), float(row["bid_sz"])),),
                asks=((float(row["ask_px"]), float(row["ask_sz"])),),
            )
        case "liquidations":
            notional = _maybe(row.get("notional_usd"))
            if notional is None:
                # Same rule as the feature engine: a liquidation with no notional is not a
                # liquidation of zero. OKX sends these for contracts it has no price for,
                # a few an hour, and they only prove the feed is alive, which the stream
                # stats already recorded.
                return None
            return Liq(
                ts_ms=known,
                coin=coin,
                venue=venue,
                side=str(row["side"]),
                px=_maybe(row.get("px")) or 0.0,
                notional_usd=notional,
            )
        case "asset_ctx":
            return AssetCtx(
                ts_ms=known,
                coin=coin,
                mark_px=_maybe(row.get("mark_px")),
                oracle_px=_maybe(row.get("oracle_px")),
                mid_px=_maybe(row.get("mid_px")),
                funding=_maybe(row.get("funding")),
            )
        case _:
            return None


def _maybe(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
