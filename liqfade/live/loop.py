"""`liqfade paper`: the process. Feeds in, decisions every 250ms, everything else watching.

What runs here, and why each piece is where it is:

- **The recorder's own stream builder**, with a different sink. Paper turns rows into
  features and a book instead of into Parquet, and reusing `build_streams` means one
  definition of which venues, which frames and which parser.
- **A decision tick every `features.decision_cadence_ms`**, driven by the wall clock. The
  session behind it is `backtest/engine.py`, so a paper decision and a replayed one are
  the same decision.
- **The control plane**, on its own socket and its own `state.json`, because the recorder
  runs on the same box and the server takes an exclusive lock per socket.
- **The kill switch and its file watcher**, wired to the session's own cancel and close.
  The Telegram `/kill` and the `data/KILL` file reach the same `KillSwitch.fire`.
- **The Telegram command bot**, polling in its own task, dispatching through the control
  backend so `/flat` means one thing everywhere.
- **A heartbeat file touched every second**, which is what the watchdog reads.
- **Registry reconciliation on startup, on every stream reconnect and every
  `execution.reconcile_s`**, run by `execution/reconcile.py`'s `Reconciler`, which is the
  one implementation of it. Paper has no venue account, so the position half has no second
  source and is switched off rather than compared against nothing. The order half does run:
  the sqlite registry is compared against the engine's working orders, which is what
  catches the mirror drifting from the book it mirrors.

**Nothing here can place a real order.** The session simulates every fill, and the mainnet
gates are untouched: `Settings.live_trading_enabled` still needs all three conditions, and
this loop never asks for a signer.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from liqfade.alerts.commands import CommandBot, allowed_chat_ids
from liqfade.alerts.telegram import send_alert
from liqfade.config.models import Settings
from liqfade.control import ControlServer, StateWriter
from liqfade.control.protocol import (
    FeedState,
    FlatResult,
    InjectResult,
    KillResult,
    MetricsResult,
    PauseResult,
    ReloadResult,
    ResumeResult,
    RiskState,
    StateSnapshot,
    now_ms,
)
from liqfade.execution.hl import AccountSnapshot, TriggerOrder, VenueOrder
from liqfade.execution.kill import KillSwitch, KillWatcher
from liqfade.execution.orders import OrderStore
from liqfade.execution.reconcile import Reconciler
from liqfade.execution.watchdog import write_heartbeat_atomic
from liqfade.live.paper import PaperSession
from liqfade.live.venue import VenueSession
from liqfade.log import get_logger
from liqfade.recorder.backend import LatencyTracker
from liqfade.recorder.main import apply_universe, build_streams
from liqfade.recorder.streams.base import WSStream, backoff_delay
from liqfade.recorder.universe import UniverseManager
from liqfade.strategy.rules import build_enabled_rules

log = get_logger(__name__)


def apply_settings(session: PaperSession | VenueSession, fresh: Settings) -> None:
    """Swap reloaded settings into everything under the session that holds a copy.

    The session, the risk engine and the strategy each keep their own reference, and the
    strategy's rules and per coin detectors were built from the object they were given.
    A reload that misses any of them announces a change on Telegram and applies none of
    it, which is worse than refusing: the page and the box disagree and only the box is
    trading. `strategy` and `filters` are two of the panel's writable sections, so this
    is most of what a settings write is for.

    The feature engine is deliberately not swapped: `features.` and `cascade.` are
    restart only, because the buffers were filled under them.
    """
    session.settings = fresh
    session.risk.settings = fresh
    strategy = session.strategy
    strategy.settings = fresh
    # Built once at construction, from the config sections of the settings object handed
    # in then. Reached here rather than through the engine because a live rebuild is only
    # ever a reload, and `StrategyEngine` is shared with the backtest.
    strategy._rules = build_enabled_rules(fresh)
    for detector in strategy._detectors.values():
        detector.settings = fresh


async def _maybe_await(result: Any) -> Any:
    """Await it when it is a coroutine. Paper's close_out is sync and the venue's is not.

    `RuntimeWarning: coroutine 'VenueSession.close_out' was never awaited` is what this
    fixes, and the cost of that warning was that shutdown cancelled nothing at all.
    """
    if inspect.isawaitable(result):
        return await result
    return result


__all__ = ["PaperBackend", "PaperLoop"]

# Seconds before the same risk reason is posted again. Every refusal is logged; a gate
# that refuses every 250ms tick is one thing to say, not two thousand.
RISK_BLOCK_ALERT_S = 300.0
# Config keys that cannot change under a running loop. The feature engine's ring buffers
# were filled under these, so swapping them leaves windows that no longer describe the data
# in them, and the universe decides which streams are even subscribed.
RESTART_ONLY = ("features.", "cascade.", "universe.", "paths.", "control.", "venues.")

DEFAULT_PAPER_EQUITY = 10_000.0  # USD, overridden by --equity. Never a hardcoded live number.


class PaperBooks:
    """The `BookReader` the reconciler compares the registry against, in paper.

    The engine's working orders are what the sqlite registry mirrors, so they are the
    second opinion here in exactly the way `openOrders` is on testnet. There is no account
    to read, which is why the reconciler runs with `compare_positions=False` and `account`
    is never called.
    """

    def __init__(self, session: PaperSession) -> None:
        self.session = session

    async def account(self, address: str) -> AccountSnapshot:  # pragma: no cover - never called
        raise NotImplementedError("paper has no venue account; compare_positions must be False")

    async def trigger_orders(self, address: str) -> list[TriggerOrder]:
        """None: the simulator has no separate trigger book.

        A paper exit, stop or target included, is a working order and comes back from
        `open_orders` with everything else. The venue keeps its triggers in a query of
        their own, which is what the reconciler reads this for.
        """
        return []

    async def open_orders(self, address: str) -> list[VenueOrder]:
        orders = []
        for coin, coin_state in self.session.state.items():
            for order in (coin_state.entry, coin_state.exit_order):
                if order is None or order.remaining <= 0:
                    continue
                orders.append(
                    VenueOrder(
                        oid=0,
                        coin=coin,
                        is_buy=order.is_buy,
                        px=order.px,
                        size=order.remaining,
                        ts_ms=order.visible_ms,
                        cloid=order.cloid,
                    )
                )
        return orders


class PaperBackend:
    """The paper loop's `ControlBackend`. Reads the session, never blocks the tick."""

    def __init__(self, loop: PaperLoop) -> None:
        self.loop = loop

    async def status(self) -> StateSnapshot:
        loop = self.loop
        at = now_ms()
        positions, orders = loop.session.snapshot()
        risk_state = loop.session.risk.state
        return StateSnapshot(
            proc="paper",
            env=loop.settings.env,
            ts_ms=at,
            live_trading=False,
            uptime_s=(at - loop.started_ms) / 1000.0,
            feeds=loop.feed_states(at),
            positions=positions,
            orders=orders,
            risk=RiskState(
                paused=loop.paused,
                halt_reason=risk_state.halt_reason,
                halt_until_ms=risk_state.halt_until_ms,
                killed=loop.kill.fired,
                equity=loop.session.equity(),
                day_start_equity=risk_state.day_start_equity,
                peak_equity=risk_state.peak_equity,
                daily_realized_pnl=risk_state.daily_realized_pnl,
                consecutive_losses=risk_state.consecutive_losses,
                open_positions=len(positions),
                coin_cooldowns=dict(risk_state.coin_cooldowns),
                actions_remaining=loop.session.actions_remaining(),
            ),
            latency=loop.latency.state(),
            universe=list(loop.universe.coins),
        )

    async def pause(self, reason: str | None = None) -> PauseResult:
        # Posted, not only logged. These commands arrive from Telegram, the panel and the
        # CLI alike, and an owner who paused from the panel should see the same message an
        # owner who paused from their phone sees. A state change nobody is told about is
        # how two people disagree about whether the bot is trading.
        self.loop.paused = True
        log.warning("paper_paused", reason=reason)
        await self.loop.post(
            f"[{self.loop.settings.env}] PAUSED" + (f": {reason}" if reason else ""),
            dedupe=False,
        )
        return PauseResult(paused=True, reason=reason)

    async def resume(self) -> ResumeResult:
        if self.loop.kill.fired:
            return ResumeResult(paused=True, halt_cleared=False, reason="killed, restart required")
        self.loop.paused = False
        log.warning("paper_resumed")
        await self.loop.post(f"[{self.loop.settings.env}] RESUMED, entries allowed", dedupe=False)
        return ResumeResult(paused=False, halt_cleared=True)

    async def flat(self, coin: str | None = None) -> FlatResult:
        cancelled = await self.loop.session.cancel_all()
        closed = await self.loop.session.flatten(now_ms=now_ms(), coin=coin)
        self.loop.paused = True
        log.warning("paper_flat", coin=coin, closed=closed, cancelled=cancelled)
        await self.loop.post(
            f"[{self.loop.settings.env}] FLAT {coin or 'all'}: closed {len(closed)} "
            f"position(s), cancelled {cancelled} order(s). Entries paused.",
            dedupe=False,
        )
        return FlatResult(closing=closed, canceled_orders=cancelled)

    async def kill(self, reason: str | None = None) -> KillResult:
        return await self.loop.kill.fire(reason=reason)

    async def reload_config(self) -> ReloadResult:
        """Re-resolve config and apply what is safe to change while running.

        The panel writes `overrides.yaml` and calls this. What it wants to change is
        sizing, risk limits and trigger thresholds, and those are all read fresh on the
        tick that uses them, so applying them between ticks is safe.

        **What is refused is anything the feature engine was built around.** The ring
        buffers were filled under `features.*`, `cascade.*` and the universe, and swapping
        those under a running engine gives a half-reloaded one whose windows no longer
        mean what its buffers hold. That is worse than saying no, which is why 05a said no
        to everything; the answer is to name the keys rather than refuse the command.

        Applied between ticks, never mid-order: this runs on the control task, and the
        session reads `self.settings` at the top of each tick.
        """
        return await self.loop.reload_config()

    async def get_metrics(self) -> MetricsResult:
        return MetricsResult(
            feeds=self.loop.feed_states(now_ms()),
            latency=self.loop.latency.state(),
            counters=self.loop.counters(),
        )

    async def inject_intent(self, **args: Any) -> InjectResult:
        """The drill command. Refused on mainnet by the session itself."""
        coin = str(args.get("coin", "")).upper()
        side = str(args.get("side", "long"))
        if not coin or side not in {"long", "short"}:
            return InjectResult(injected=False, reason="need coin and side (long|short)")
        try:
            intent = self.loop.session.inject_intent(
                coin=coin,
                side=side,  # type: ignore[arg-type]
                now_ms=now_ms(),
                target_bps=float(args.get("target_bps", 30.0)),
                stop_bps=float(args.get("stop_bps", 30.0)),
                urgency=str(args.get("urgency", "normal")),
            )
        except PermissionError as exc:
            return InjectResult(injected=False, reason=str(exc))
        if intent is None:
            return InjectResult(injected=False, coin=coin, reason="no price for that coin yet")
        # An injected order is placed outside a tick, and the fill model can take it before
        # the next one. Sync now, or a drill's entry never reaches the registry at all.
        await self.loop.session.sync_orders(now_ms())
        drain = getattr(self.loop.session, "drain_injections", None)
        if drain is not None:
            # The venue session queues an injected intent rather than placing it from
            # a sync call. Nothing to drain in paper, where the engine took it already.
            await drain()
        await self.loop.post(
            f"[{self.loop.settings.env}] INTENT INJECTED {coin} {side} "
            f"entry {intent.entry_px:g} target {intent.target_px:g} stop {intent.stop_px:g}",
            dedupe=False,
        )
        return InjectResult(injected=True, coin=coin, side=side, entry_px=intent.entry_px)


class PaperLoop:
    """Owns every task the paper process runs."""

    def __init__(
        self,
        settings: Settings,
        *,
        starting_equity: float = DEFAULT_PAPER_EQUITY,
        minutes: float | None = None,
    ) -> None:
        self.settings = settings
        self.starting_equity = starting_equity
        self.minutes = minutes
        self.started_ms = now_ms()
        self.paused = False
        self.latency = LatencyTracker()
        self.store: OrderStore | None = None
        self.session: PaperSession | VenueSession
        self.streams: list[WSStream] = []
        self.universe = UniverseManager(
            settings.universe,
            settings.venues.hyperliquid.info_base,
            on_change=self._apply_universe,
            on_rtt_ms=self.latency.add_rest,
        )
        self.kill: KillSwitch
        self.reconciler: Reconciler
        # Set by every stream when its socket comes back, and by `_supervise` when a task
        # it owns crashed and is being restarted. The reconciler waits on it.
        self.reconnected = asyncio.Event()
        self.ticks = 0
        self.reconnects = 0
        self._risk_block_posted: dict[str, float] = {}
        self._halt_posted: str | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    # --- wiring ---------------------------------------------------------------

    async def setup(self, stop: asyncio.Event | None = None) -> None:
        """Open the registry, build the session, the streams and the kill switch.

        `stop` is the process shutdown event. The kill switch holds it because CLAUDE.md
        rule 5 says a kill stops the loop as well as flattening it: without it the process
        cancels, closes and then carries on ticking against a killed account.
        """
        settings = self.settings
        # One mkdir at startup, off the event loop's hot path by virtue of being once.
        await asyncio.to_thread(Path(settings.paths.data_dir).mkdir, parents=True, exist_ok=True)
        self.store = await OrderStore.open(settings.order_db_path)
        self.session = PaperSession(
            settings,
            store=self.store,
            meta={},
            starting_equity=self.starting_equity,
            risk_state_path=settings.risk_state_path,
        )
        self.streams = build_streams(
            settings,
            self.sink,
            on_rtt_ms=self.latency.add_ws_ping,
            on_reconnect=self._on_reconnect,
        )
        self.reconciler = Reconciler(
            reader=PaperBooks(self.session),
            address="",  # paper reads no account; PaperBooks ignores it
            orders=self.store.open_orders,
            interval_s=settings.execution.reconcile_s,
            alert=self.post,
            compare_positions=False,
        )
        self.kill = KillSwitch(
            kill_file=settings.kill_file,
            open_orders=self.session.open_order_cloids,
            cancel_all=self.session.cancel,
            open_positions=self.session.open_positions,
            close_position=self.session.close_position,
            alert=self.post,
            stop=stop,
            budget_s=settings.risk.kill_budget_s,
        )

    def _on_reconnect(self, venue: str) -> None:
        """A stream came back, or a supervised task was restarted. Reconcile, and say so.

        CLAUDE.md rule 4: a socket that was away missed whatever happened while it was,
        so the two books are compared without waiting out the interval. CLAUDE.md section
        5 lists the reconnect itself among the things Telegram hears about. Sync, because
        the stream's own loop calls it, so the post goes on a task and the dedupe keeps a
        flapping venue to one line every ten minutes.
        """
        self.reconnects += 1
        self.reconnected.set()
        # The gate that halts entries after `risk.max_reconnects_10min` counts them here
        # and nowhere else: a feed that keeps dropping is a feed the strategy is deciding
        # against blind, and until this line the counter was never incremented at all.
        self.session.risk.on_reconnect(now_ms())
        log.warning("stream_reconnected", venue=venue, reconnects=self.reconnects)
        self._background(self.post(f"[{self.settings.env}] RECONNECT {venue}"))

    def _background(self, coro: Any) -> None:
        """Run a post from a sync caller. Kept referenced so it is not collected early."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def sink(self, table: str, venue: str, row: dict[str, Any]) -> None:
        """Every stream's rows land here. Called from the receive loop, so it never raises."""
        self.session.ingest(table, venue, row)

    async def _apply_universe(self, coins: list[str]) -> bool:
        """Point every stream at the new coin set. The recorder's own function, shared."""
        return await apply_universe(self.streams, coins)

    def feed_states(self, at_ms: int) -> list[FeedState]:
        return [state for stream in self.streams for state in stream.feed_states(at_ms)]

    def counters(self) -> dict[str, int]:
        return {
            "ticks": self.ticks,
            "intents": len(self.session.intents),
            "fills": len(self.session.fills),
            "trades": len(self.session.portfolio.trades),
            "reconcile_differences": self.reconciler.differences,
            "reconcile_runs": self.reconciler.runs,
            "reconnects": self.reconnects,
        }

    async def post(self, text: str, *, dedupe: bool = True) -> bool:
        """One Telegram line. Never raises, never blocks a decision.

        `dedupe=False` for an event. An intent and a fill are things that happened, and
        two identical ones inside the dedupe window are two of them.
        """
        if not self.settings.telegram.enabled:
            return False
        return await send_alert(
            text, timeout_s=self.settings.telegram.send_timeout_s, dedupe=dedupe
        )

    # --- the loops ------------------------------------------------------------

    async def tick_loop(self, stop: asyncio.Event) -> None:
        """The decision cadence. Everything else in the process exists to serve this."""
        cadence_s = self.settings.features.decision_cadence_ms / 1000.0
        posted_fills = 0
        while not stop.is_set():
            started = time.monotonic()
            at = now_ms()
            try:
                # Always tick, even paused. The tick is the only path to the exit checks,
                # so a paused loop that skipped it would stop honouring the stop and the
                # time stop of a position already open. Pause drops new entries, nothing
                # else; `PaperSession.tick` is where that is applied.
                intents = await self.session.tick(at, allow_entries=not self.paused)
            except Exception as exc:
                log.exception("tick_failed")
                intents = []
                await self.post(f"[{self.settings.env}] ERROR tick failed: {exc}")
            self.ticks += 1
            for intent in intents:
                await self._post_intent(intent)
            posted_fills = await self._post_fills(posted_fills)
            await self.report_risk_blocks()
            await self.report_halt()
            elapsed = time.monotonic() - started
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=max(0.0, cadence_s - elapsed))

    async def _post_intent(self, intent: Any) -> None:
        log.info(
            "intent",
            coin=intent.coin,
            side=intent.side,
            entry_px=intent.entry_px,
            target_px=intent.target_px,
            stop_px=intent.stop_px,
            urgency=intent.urgency,
            **{f"explain_{k}": v for k, v in intent.explain.items()},
        )
        if not self.settings.telegram.alert_on_intent:
            return
        explain = ", ".join(f"{key}={value:.4g}" for key, value in sorted(intent.explain.items()))
        await self.post(
            f"[{self.settings.env}] INTENT {intent.coin} {intent.side} "
            f"entry {intent.entry_px:g} target {intent.target_px:g} stop {intent.stop_px:g} "
            f"({intent.urgency})\n{explain}",
            dedupe=False,
        )

    async def _post_fills(self, posted: int) -> int:
        while posted < len(self.session.fills):
            _ts_ms, coin, leg, px, size = self.session.fills[posted]
            posted += 1
            await self.post(
                f"[{self.settings.env}] SIM FILL {coin} {leg} {size:g} @ {px:g}"
                + (
                    f" pnl {self.session.portfolio.trades[-1].net_pnl:+.2f}"
                    if leg != "entry" and self.session.portfolio.trades
                    else ""
                ),
                dedupe=False,
            )
        return posted

    async def report_risk_blocks(self) -> None:
        """Log every refusal since the last tick and post one per reason per window.

        The engine counts refusals into `result.sizing_blocks` and `result.risk_blocks`,
        which is what the backtest report reads. In the live process a counter nobody
        looks at is a silent refusal, and a strategy that has quietly stopped trading
        looks exactly like one that has found nothing to trade. Logging is unconditional;
        the Telegram post is rate limited per reason, because a gate that refuses on every
        tick has one thing to say.

        Sizing and risk are both reported. They are two gates to us and one event to an
        owner watching an intent produce no order, and sizing returns first, so covering
        only the risk gate left half the refusals exactly as silent as before.
        """
        blocks = self.session.result.recent_blocks
        now = time.monotonic()
        newest: dict[str, tuple[str, str]] = {}
        while blocks:
            ts_ms, coin, gate, reason = blocks.popleft()
            log.warning("risk_block", gate=gate, reason=reason, coin=coin, ts_ms=ts_ms)
            newest[reason] = (coin, gate)
        counters = self.session.result
        for reason, (coin, gate) in newest.items():
            if now - self._risk_block_posted.get(reason, -RISK_BLOCK_ALERT_S * 2) < (
                RISK_BLOCK_ALERT_S
            ):
                continue
            self._risk_block_posted[reason] = now
            seen = (counters.sizing_blocks if gate == "sizing" else counters.risk_blocks).get(
                reason, 0
            )
            await self.post(
                f"[{self.settings.env}] RISK BLOCK {gate}/{reason} on {coin} (x{seen} so far)"
            )

    async def report_halt(self) -> None:
        """Post when the risk engine halts, and again when it clears. Once each.

        The halt reason repeats every tick for as long as it holds, so the edge is what
        is worth saying. CLAUDE.md section 5 lists halt among the things Telegram hears.
        """
        reason = self.session.risk.state.halt_reason
        if reason == self._halt_posted:
            return
        self._halt_posted = reason
        if reason:
            log.error("risk_halted", reason=reason)
            await self.post(f"[{self.settings.env}] HALT {reason}. No new entries.")
        else:
            log.warning("risk_halt_cleared")
            await self.post(f"[{self.settings.env}] halt cleared, entries allowed again")

    async def reload_config(self) -> ReloadResult:
        """Re-resolve settings and swap in the keys that are safe to change live."""
        from liqfade.config import ConfigError, flatten, load_settings

        try:
            fresh = load_settings(self.settings.env)
        except ConfigError as exc:
            log.error("reload_failed", error=str(exc))
            return ReloadResult(changed=[], error=str(exc))

        before, after = flatten(self.settings.model_dump()), flatten(fresh.model_dump())
        changed = sorted(k for k in after if before.get(k) != after.get(k))
        if not changed:
            return ReloadResult(changed=[])

        blocked = sorted(k for k in changed if k.startswith(RESTART_ONLY))
        if blocked:
            reason = (
                "these need a restart, because the feature engine's buffers were filled "
                f"under them: {', '.join(blocked)}"
            )
            log.warning("reload_refused", blocked=blocked)
            return ReloadResult(changed=[], error=reason)

        for key in changed:
            log.warning("config_changed", key=key, before=before.get(key), after=after.get(key))
        self.settings = fresh
        apply_settings(self.session, fresh)
        await self.post(
            f"[{fresh.env}] CONFIG RELOADED, {len(changed)} change(s):\n"
            + "\n".join(f"{k}: {before.get(k)} -> {after.get(k)}" for k in changed[:10]),
            dedupe=False,
        )
        return ReloadResult(changed=changed)

    def extra_jobs(self, stop: asyncio.Event) -> list[tuple[str, Any]]:
        """Tasks a subclass runs beside the shared ones. Paper has none."""
        return []

    async def heartbeat_loop(self, stop: asyncio.Event) -> None:
        """Beat on a thread of its own, every `risk.heartbeat_s`. The watchdog reads nothing else.

        The thread is the point. A heartbeat that is one more coroutine says "this event
        loop is still turning", and the event loop turns for as long as no tick blocks it,
        so the file went silent exactly when a tick had the most work to do: a fill, a
        Telegram post, a reconcile. The watchdog cannot tell that apart from a dead process
        and kills a loop that is merely busy. What the owner wants to know is that the
        process is alive, and a thread answers that question even while the loop is
        wedged. A truly dead process cannot beat from any thread.

        The interval is `risk.heartbeat_s`, which is the field `risk.watchdog_stale_s` is
        validated against; it was a module constant, so the two numbers that have to agree
        lived in different files and only one of them was read.
        """
        path = self.settings.heartbeat_path
        interval = float(self.settings.risk.heartbeat_s)
        done = threading.Event()

        def beat() -> None:
            while not done.is_set():
                try:
                    write_heartbeat_atomic(path, now_ms())
                except OSError as exc:
                    log.error("heartbeat_write_failed", path=str(path), error=str(exc))
                done.wait(interval)

        worker = threading.Thread(target=beat, name="heartbeat", daemon=True)
        worker.start()
        try:
            await stop.wait()
        finally:
            done.set()
            worker.join(timeout=interval + 1.0)

    async def _supervise(
        self, name: str, factory: Callable[[], Awaitable[None]], stop: asyncio.Event
    ) -> None:
        attempt = 0
        while not stop.is_set():
            try:
                await factory()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("task_crashed", task=name)
                await self.post(f"[{self.settings.env}] ERROR task {name} crashed: {exc}")
                # A restarted task is a reconnect like any other, and the books have to be
                # compared before it is trusted again.
                self._on_reconnect(name)
            delay = backoff_delay(attempt, self.settings.recorder)
            attempt += 1
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)

    async def _deadline(self, stop: asyncio.Event) -> None:
        assert self.minutes is not None
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=self.minutes * 60.0)
        stop.set()

    async def run(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        await self.setup(stop)
        settings = self.settings
        backend = PaperBackend(self)
        server = ControlServer(backend, settings.socket_path)
        state_writer = StateWriter(
            backend.status, settings.state_path, settings.control.state_interval_s
        )
        bot = CommandBot(
            backend,
            allowed_chat_ids=allowed_chat_ids(),
            env=settings.env,
            # Persisted, so a /kill is consumed once rather than redelivered to the
            # process that starts after it.
            offset_path=settings.paths.data_dir / f"telegram_offset-{settings.env}",
        )

        if settings.kill_switch_present:
            # The unit's ExecStartPre catches this too. Checked again here because a manual
            # start bypasses the unit and a kill that a restart walks past is not a kill.
            log.error("paper_refuses_to_start", reason=f"kill flag at {settings.kill_file}")
            await self.post(
                f"[{settings.env}] paper loop refused to start: kill flag at "
                f"{settings.kill_file}. Remove it deliberately."
            )
            await self.aclose()
            return

        await server.start()
        state_task = state_writer.start(stop)

        jobs: list[tuple[str, Callable[[], Awaitable[None]]]] = [
            *[(f"stream.{s.venue}", lambda s=s: s.run(stop)) for s in self.streams],
            ("universe", lambda: self.universe.run(stop)),
            ("tick", lambda: self.tick_loop(stop)),
            ("heartbeat", lambda: self.heartbeat_loop(stop)),
            ("reconcile", lambda: self.reconciler.run(stop, reconnected=self.reconnected)),
            ("kill_watch", lambda: KillWatcher(self.kill).run(stop)),
            *([("telegram", lambda: bot.run(stop))] if settings.telegram.commands else []),
            *self.extra_jobs(stop),
        ]
        tasks = [
            asyncio.create_task(self._supervise(name, factory, stop), name=name)
            for name, factory in jobs
        ]
        if self.minutes:
            tasks.append(asyncio.create_task(self._deadline(stop), name="deadline"))

        log.info(
            "paper_started",
            env=settings.env,
            trigger=settings.cascade.trigger,
            equity=self.starting_equity,
            streams=[s.venue for s in self.streams],
            data_dir=str(settings.paths.data_dir),
        )
        await self.post(
            f"[{settings.env}] paper loop started on {settings.cascade.trigger}, "
            f"equity {self.starting_equity:,.0f}, {len(self.streams)} feeds"
        )
        try:
            await stop.wait()
        finally:
            log.info("paper_stopping", **self.counters())
            stop.set()
            # Flatten before the tasks go: an open paper position left across a restart
            # would be marked against a book the new process has not seen yet.
            with contextlib.suppress(Exception):
                await _maybe_await(self.session.close_out(now_ms()))
            await asyncio.wait(tasks, timeout=5.0)
            for task in [*tasks, state_task]:
                task.cancel()
            await asyncio.gather(*tasks, state_task, return_exceptions=True)
            await server.stop()
            await self.aclose()
            log.info("paper_stopped", **self.counters())
            await self.post(f"[{settings.env}] paper loop stopped. {self.counters()}")

    async def aclose(self) -> None:
        if self.store is not None:
            await self.store.close()
            self.store = None


class VenueLoop(PaperLoop):
    """`liqfade testnet` and `liqfade live`: the same loop, against a real venue.

    Everything the paper loop wires stays wired and is not re-implemented here: the feeds,
    the feature engine, the control socket, Telegram, the kill switch, the heartbeat and
    the supervisor. Four things change, and each is a consequence of there being a real
    account on the other side:

    - the session is `VenueSession`, so orders go to Hyperliquid and fills come back from
      it rather than from a fill model;
    - reconciliation reads `clearinghouseState` and `openOrders` and **compares positions**,
      which paper switches off because it has no second source for one;
    - the user stream is a task, so a fill is known when it happens;
    - the account snapshot feeds the session on every reconcile, because the venue is the
      truth about what is held.

    Phase 6 runs this with `env=mainnet`, so this is the real thing rather than a drill
    harness. The three gates in `execution/signer.py` are untouched and are what decide
    whether a mainnet order may be signed at all.
    """

    def __init__(
        self, settings: Settings, *, starting_equity: float = 0.0, minutes: float | None = None
    ) -> None:
        super().__init__(settings, starting_equity=starting_equity, minutes=minutes)
        self.reader: Any = None
        self.user_stream: Any = None

    async def setup(self, stop: asyncio.Event | None = None) -> None:
        from liqfade.execution.hl import HyperliquidTransport, InfoReader
        from liqfade.execution.orders import OrderManager
        from liqfade.execution.ratelimit import ActionBudget
        from liqfade.execution.signer import load_signer
        from liqfade.execution.userstream import UserStream
        from liqfade.live.venue import VenueSession

        settings = self.settings
        await asyncio.to_thread(Path(settings.paths.data_dir).mkdir, parents=True, exist_ok=True)
        store = await OrderStore.open(settings.order_db_path)
        self.store = store

        # The one place a key is read. `load_signer` refuses on a missing key, a
        # placeholder address, and while the kill flag is present.
        signer = load_signer(settings)
        self.reader = InfoReader(settings.venues.hyperliquid.info_base)
        session = VenueSession(
            settings,
            orders=OrderManager(
                store,
                HyperliquidTransport(signer),
                sz_decimals={},
                killed=lambda: settings.kill_switch_present,
                retries=settings.execution.transient_retries,
                budget=ActionBudget(settings.execution.action_budget_per_hour),
                on_ack_ms=self.latency.add_submit_ack,
            ),
            reader=self.reader,
            address=signer.account_address,
            meta={},
            starting_equity=self.starting_equity,
            risk_state_path=settings.risk_state_path,
        )
        self.session = session
        await session.orders.load()
        # Before anything can be sized: the venue's own szDecimals, not a default.
        await session.load_asset_rules()

        self.streams = build_streams(
            settings,
            self.sink,
            on_rtt_ms=self.latency.add_ws_ping,
            on_reconnect=self._on_reconnect,
        )
        self.user_stream = UserStream(
            url=settings.venues.hyperliquid.ws_url,
            address=signer.account_address,
            manager=session.orders,
            cfg=settings.recorder,
            on_reconnect=self._on_reconnect,
            on_fill=lambda event: session.on_fill(
                event.cloid, px=event.px, size=event.size, ts_ms=event.ts_ms
            ),
        )
        self.reconciler = Reconciler(
            reader=self.reader,
            address=signer.account_address,
            orders=store.open_orders,
            positions=lambda: {c: p.size for c, p in session.positions.items()},
            interval_s=settings.execution.reconcile_s,
            alert=self.post,
            compare_positions=True,
            on_account=session.sync_account,
        )
        self.kill = KillSwitch(
            kill_file=settings.kill_file,
            open_orders=session.open_order_cloids,
            cancel_all=session.cancel,
            open_positions=session.open_positions,
            close_position=session.close_position,
            venue_state=session.venue_state,
            alert=self.post,
            stop=stop,
            budget_s=settings.risk.kill_budget_s,
        )

    def extra_jobs(self, stop: asyncio.Event) -> list[tuple[str, Any]]:
        """The user stream, on top of everything the paper loop runs."""
        return [("user_stream", lambda: self.user_stream.run(stop))]

    async def aclose(self) -> None:
        await super().aclose()
        if self.reader is not None:
            await self.reader.close()
