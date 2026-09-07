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
- **Registry reconciliation every `execution.reconcile_s`.** Paper has no venue account,
  so the position half of reconciliation has no second source and only runs on testnet.
  The order half does run: the sqlite registry is compared against the engine's working
  orders, which is what catches the mirror drifting from the book it mirrors.

**Nothing here can place a real order.** The session simulates every fill, and the mainnet
gates are untouched: `Settings.live_trading_enabled` still needs all three conditions, and
this loop never asks for a signer.
"""

from __future__ import annotations

import asyncio
import contextlib
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
from liqfade.execution.hl import VenueOrder
from liqfade.execution.kill import KillSwitch, KillWatcher
from liqfade.execution.orders import OrderStore
from liqfade.execution.reconcile import reconcile
from liqfade.live.paper import PaperSession
from liqfade.log import get_logger
from liqfade.recorder.backend import LatencyTracker
from liqfade.recorder.main import apply_universe, build_streams
from liqfade.recorder.streams.base import WSStream, backoff_delay
from liqfade.recorder.universe import UniverseManager

log = get_logger(__name__)

__all__ = ["PaperBackend", "PaperLoop"]

HEARTBEAT_S = 1.0
DEFAULT_PAPER_EQUITY = 10_000.0  # USD, overridden by --equity. Never a hardcoded live number.


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
            ),
            latency=loop.latency.state(),
            universe=list(loop.universe.coins),
        )

    async def pause(self, reason: str | None = None) -> PauseResult:
        self.loop.paused = True
        log.warning("paper_paused", reason=reason)
        return PauseResult(paused=True, reason=reason)

    async def resume(self) -> ResumeResult:
        if self.loop.kill.fired:
            return ResumeResult(paused=True, halt_cleared=False, reason="killed, restart required")
        self.loop.paused = False
        log.warning("paper_resumed")
        return ResumeResult(paused=False, halt_cleared=True)

    async def flat(self, coin: str | None = None) -> FlatResult:
        cancelled = await self.loop.session.cancel_all()
        closed = await self.loop.session.flatten(now_ms=now_ms(), coin=coin)
        self.loop.paused = True
        log.warning("paper_flat", coin=coin, closed=closed, cancelled=cancelled)
        return FlatResult(closing=closed, canceled_orders=cancelled)

    async def kill(self, reason: str | None = None) -> KillResult:
        return await self.loop.kill.fire(reason=reason)

    async def reload_config(self) -> ReloadResult:
        # Deliberately not implemented in 05a. Reloading mid-session would swap the
        # windows the feature engine's buffers were filled under, and a half-reloaded
        # engine is worse than one that says no.
        return ReloadResult(changed=[], error="reload_config is not supported by the paper loop")

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
            )
        except PermissionError as exc:
            return InjectResult(injected=False, reason=str(exc))
        if intent is None:
            return InjectResult(injected=False, coin=coin, reason="no price for that coin yet")
        await self.loop.post(
            f"[{self.loop.settings.env}] INTENT INJECTED {coin} {side} "
            f"entry {intent.entry_px:g} target {intent.target_px:g} stop {intent.stop_px:g}"
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
        self.session: PaperSession
        self.streams: list[WSStream] = []
        self.universe = UniverseManager(
            settings.universe,
            settings.venues.hyperliquid.api_url,
            on_change=self._apply_universe,
            on_rtt_ms=self.latency.add_rest,
        )
        self.kill: KillSwitch
        self.ticks = 0
        self.reconcile_differences = 0
        self.reconnects = 0

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
        self.streams = build_streams(settings, self.sink, on_rtt_ms=self.latency.add_ws_ping)
        self.kill = KillSwitch(
            kill_file=settings.kill_file,
            open_orders=self.session.open_order_cloids,
            cancel_all=self.session.cancel,
            open_positions=self.session.open_positions,
            close_position=self.session.close_position,
            alert=self.post,
            stop=stop,
        )

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
            "reconcile_differences": self.reconcile_differences,
            "reconnects": self.reconnects,
        }

    async def post(self, text: str) -> bool:
        """One Telegram line. Never raises, never blocks a decision."""
        if not self.settings.telegram.enabled:
            return False
        return await send_alert(text, timeout_s=self.settings.telegram.send_timeout_s)

    # --- the loops ------------------------------------------------------------

    async def tick_loop(self, stop: asyncio.Event) -> None:
        """The decision cadence. Everything else in the process exists to serve this."""
        cadence_s = self.settings.features.decision_cadence_ms / 1000.0
        posted_fills = 0
        while not stop.is_set():
            started = time.monotonic()
            at = now_ms()
            try:
                intents = [] if self.paused else await self.session.tick(at)
            except Exception:
                log.exception("tick_failed")
                intents = []
            self.ticks += 1
            for intent in intents:
                await self._post_intent(intent)
            posted_fills = await self._post_fills(posted_fills)
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
            f"({intent.urgency})\n{explain}"
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
                )
            )
        return posted

    async def heartbeat_loop(self, stop: asyncio.Event) -> None:
        """Touch the heartbeat file every second. The watchdog reads nothing else."""
        path = self.settings.heartbeat_path
        while not stop.is_set():
            try:
                await asyncio.to_thread(path.write_text, f"{now_ms()}\n")
            except OSError as exc:
                log.error("heartbeat_write_failed", path=str(path), error=str(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_S)

    async def reconcile_loop(self, stop: asyncio.Event) -> None:
        """Compare the sqlite registry against the engine's working orders.

        Paper has no venue account, so the position half of reconciliation has no second
        source and is exercised on testnet instead. The order half is real: it is what
        catches the registry mirror drifting away from the book it mirrors.
        """
        interval = self.settings.execution.reconcile_s
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
            if stop.is_set():
                return
            try:
                await self.reconcile_once("interval")
            except Exception:
                log.exception("reconcile_loop_failed")

    async def reconcile_once(self, trigger: str) -> int:
        """One registry comparison. Returns the number of differences found."""
        if self.store is None:
            return 0
        engine_orders = []
        for coin, coin_state in self.session.state.items():
            for order in (coin_state.entry, coin_state.exit_order):
                if order is None or order.remaining <= 0:
                    continue
                engine_orders.append(
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
        positions = await self.session.open_positions()
        result = reconcile(
            our_orders=await self.store.open_orders(),
            venue_orders=engine_orders,
            our_positions=positions,
            venue_positions={},
        )
        # The position half is not comparable in paper: `venue_positions` above is empty
        # because there is no venue, so every open position would read as missing. Only
        # the order findings are real here.
        order_differences = [d for d in result.differences if "order" in d.kind]
        if order_differences:
            self.reconcile_differences += len(order_differences)
            for difference in order_differences:
                log.warning(
                    "reconcile_difference",
                    trigger=trigger,
                    kind=difference.kind,
                    detail=difference.detail,
                )
            await self.post(
                f"[{self.settings.env}] RECONCILE {trigger}: "
                + ", ".join(d.kind for d in order_differences)
            )
        else:
            log.info(
                "reconcile_clean",
                trigger=trigger,
                orders=len(engine_orders),
                positions=len(positions),
            )
        return len(order_differences)

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
            except Exception:
                log.exception("task_crashed", task=name)
                self.reconnects += 1
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
        bot = CommandBot(backend, allowed_chat_ids=allowed_chat_ids(), env=settings.env)

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
        await self.reconcile_once("startup")

        jobs: list[tuple[str, Callable[[], Awaitable[None]]]] = [
            *[(f"stream.{s.venue}", lambda s=s: s.run(stop)) for s in self.streams],
            ("universe", lambda: self.universe.run(stop)),
            ("tick", lambda: self.tick_loop(stop)),
            ("heartbeat", lambda: self.heartbeat_loop(stop)),
            ("reconcile", lambda: self.reconcile_loop(stop)),
            ("kill_watch", lambda: KillWatcher(self.kill).run(stop)),
            ("telegram", lambda: bot.run(stop)),
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
                self.session.close_out(now_ms())
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
