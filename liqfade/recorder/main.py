"""The recorder process: one asyncio task per stream, all writing one Parquet lake.

Wiring, and nothing clever:

    WebSocket -> parse (pure, per venue) -> Buffers.add -> hourly Parquet flush
    metaAndAssetCtxs every universe.refresh_min -> resubscribe every venue
    Coinalyze REST every venues.coinalyze.poll_s -> xchg_ctx
    health checks -> Telegram
    ControlServer + StateWriter -> `liqfade status`, the panel, Telegram

Every long lived task is supervised: if one raises, it is restarted after the same
jittered backoff a reconnect uses, and the other streams keep recording. A recorder that
dies at 3am because one venue changed a field is the failure mode this exists to prevent.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from liqfade.config import ConfigError, flatten, load_settings, resolve
from liqfade.config.models import Settings
from liqfade.control import ControlServer, StateWriter
from liqfade.control.protocol import FeedState, ReloadResult, now_ms
from liqfade.data.symbols import to_venue_symbol
from liqfade.log import get_logger
from liqfade.recorder.backend import LatencyTracker, RecorderBackend
from liqfade.recorder.buffer import Buffers
from liqfade.recorder.health import Health
from liqfade.recorder.streams import binance, bybit, hl, okx
from liqfade.recorder.streams.base import WSStream, backoff_delay
from liqfade.recorder.streams.coinalyze import CoinalyzePoller
from liqfade.recorder.universe import UniverseManager
from liqfade.recorder.writer import compact_day, write_rows

log = get_logger(__name__)

FLUSH_TICK_S = 5.0
SHUTDOWN_GRACE_S = 5.0


def _seconds_until_utc_hour(hour: int, now: datetime | None = None) -> float:
    now = now or datetime.now(UTC)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


class Recorder:
    """Owns every task the recorder runs and the state the control plane reports."""

    def __init__(self, settings: Settings, *, minutes: float | None = None) -> None:
        self.settings = settings
        self.minutes = minutes
        self.root = Path(settings.paths.data_dir) / "parquet"
        self.buffers = Buffers(settings.recorder.ring_buffer_rows)
        self.latency = LatencyTracker()
        self.counters: dict[str, int] = {"rows_written": 0, "files_written": 0, "flushes": 0}
        self.streams: list[WSStream] = self._build_streams()
        self.poller = CoinalyzePoller(settings.venues.coinalyze, self.buffers.add)
        self.universe = UniverseManager(
            settings.universe,
            settings.venues.hyperliquid.api_url,
            on_change=self._apply_universe,
            on_rtt_ms=self.latency.add_rest,
        )
        self.health = Health(
            settings.recorder,
            settings.paths.data_dir,
            [*self.streams, self.poller],
        )
        self._flat_config = flatten(resolve(settings.env)[0])

    # --- construction --------------------------------------------------------

    def _build_streams(self) -> list[WSStream]:
        venues = self.settings.venues
        cfg = self.settings.recorder
        streams: list[WSStream] = []

        if venues.hyperliquid.enabled:
            streams.append(
                WSStream(
                    hl.VENUE,
                    venues.hyperliquid.ws_url,
                    parse=hl.parse,
                    sink=self.buffers.add,
                    cfg=cfg,
                    tables=hl.TABLES,
                    on_connect=hl.on_connect,
                    on_universe_change=hl.on_universe_change,
                    ping_s=venues.hyperliquid.ws_ping_s,
                    ping_frame=hl.PING_FRAME,
                    pong_marker=hl.PONG_MARKER,
                    on_rtt_ms=self.latency.add_ws_ping,
                )
            )
        if venues.binance.enabled:
            streams.append(
                WSStream(
                    binance.VENUE,
                    venues.binance.ws_url,
                    parse=binance.make_parser(venues.binance.complete),
                    sink=self.buffers.add,
                    cfg=cfg,
                    tables=binance.TABLES,
                    on_connect=binance.on_connect,
                )
            )
        if venues.bybit.enabled:
            on_connect, on_change = bybit.make_frames(venues.bybit.subscribe_batch)
            streams.append(
                WSStream(
                    bybit.VENUE,
                    venues.bybit.ws_url,
                    parse=bybit.make_parser(venues.bybit.complete),
                    sink=self.buffers.add,
                    cfg=cfg,
                    tables=bybit.TABLES,
                    on_connect=on_connect,
                    on_universe_change=on_change,
                    ping_s=bybit.PING_S,
                    ping_frame=bybit.PING_FRAME,
                )
            )
        if venues.okx.enabled:
            streams.append(
                WSStream(
                    okx.VENUE,
                    venues.okx.ws_url,
                    parse=okx.make_parser(venues.okx.complete),
                    sink=self.buffers.add,
                    cfg=cfg,
                    tables=okx.TABLES,
                    on_connect=okx.on_connect,
                    ping_s=okx.PING_S,
                    ping_frame=okx.PING_FRAME,
                )
            )
        return streams

    # --- state exposed to the control plane ----------------------------------

    def feed_states(self, at_ms: int) -> list[FeedState]:
        states = [state for stream in self.streams for state in stream.feed_states(at_ms)]
        states.extend(self.poller.feed_states(at_ms))
        return sorted(states, key=lambda state: state.name)

    def reload(self) -> ReloadResult:
        """Re-resolve config and apply what can change without a restart.

        Universe selection and the health thresholds take effect at the next tick.
        Venue URLs, enabled flags and paths need a restart, because they define the tasks
        that are already running; the returned `changed` list names them anyway so the
        operator can see the reload did not cover them.
        """
        try:
            settings = load_settings(self.settings.env)
            merged, _ = resolve(self.settings.env)
        except ConfigError as exc:
            log.warning("reload_failed", error=str(exc))
            return ReloadResult(error=str(exc))

        flat = flatten(merged)
        changed = sorted(
            key
            for key in set(flat) | set(self._flat_config)
            if flat.get(key) != self._flat_config.get(key)
        )
        self.settings = settings
        self._flat_config = flat
        self.buffers.max_rows = settings.recorder.ring_buffer_rows
        self.universe.apply_config(settings.universe)
        self.health.apply_config(settings.recorder)
        log.info("config_reloaded", changed=changed)
        return ReloadResult(changed=changed)

    # --- universe ------------------------------------------------------------

    async def _apply_universe(self, coins: Sequence[str]) -> None:
        for stream in self.streams:
            await stream.set_symbols([to_venue_symbol(coin, stream.venue) for coin in coins])
        self.poller.set_universe(list(coins))

    async def _bootstrap_universe(self) -> None:
        """One refresh before the streams connect, so the first subscribe is complete."""
        async with httpx.AsyncClient() as client:
            try:
                await self.universe.refresh(client)
                return
            except Exception as exc:
                log.warning("universe_bootstrap_failed", error=f"{type(exc).__name__}: {exc}")
        # Record the core coins rather than nothing while /info is unreachable.
        fallback = sorted(
            set(self.settings.universe.core_coins) - set(self.settings.universe.exclude)
        )
        self.universe.coins = fallback
        await self._apply_universe(fallback)

    # --- flushing ------------------------------------------------------------

    async def _write(self, key: tuple[str, str, str, int], rows: list[dict[str, Any]]) -> None:
        path = await asyncio.to_thread(write_rows, self.root, key, rows)
        if path is not None:
            self.counters["rows_written"] += len(rows)
            self.counters["files_written"] += 1

    async def flush_ready(self) -> None:
        for key in self.buffers.ready(now_ms()):
            await self._write(key, self.buffers.take(key))
        self.counters["flushes"] += 1

    async def flush_all(self) -> None:
        for key, rows in self.buffers.take_all():
            await self._write(key, rows)

    async def _flush_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=FLUSH_TICK_S)
            if stop.is_set():
                return
            await self.flush_ready()

    async def _compaction_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            delay = _seconds_until_utc_hour(self.settings.recorder.compaction_hour_utc)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
            if stop.is_set():
                return
            date = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
            try:
                written = await asyncio.to_thread(compact_day, self.root, date)
                log.info("compaction_done", date=date, tables=written)
            except Exception:
                log.exception("compaction_failed", date=date)

    # --- supervision ---------------------------------------------------------

    async def _supervise(
        self, name: str, factory: Callable[[], Awaitable[None]], stop: asyncio.Event
    ) -> None:
        attempt = 0
        while not stop.is_set():
            try:
                await factory()
                return  # a clean return means the task saw the stop event
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("task_crashed", task=name)
            delay = backoff_delay(attempt, self.settings.recorder)
            attempt += 1
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)

    # --- run -----------------------------------------------------------------

    async def run(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        settings = self.settings
        backend = RecorderBackend(
            settings,
            feeds=self.feed_states,
            universe=lambda: self.universe.coins,
            latency=self.latency,
            counters=self.counters,
            stop=stop,
            reload=self.reload,
        )
        server = ControlServer(backend, settings.socket_path)
        state_writer = StateWriter(
            backend.status, settings.state_path, settings.control.state_interval_s
        )

        self.root.mkdir(parents=True, exist_ok=True)
        await self._bootstrap_universe()
        await server.start()
        state_task = state_writer.start(stop)

        jobs: list[tuple[str, Callable[[], Awaitable[None]]]] = [
            *[(f"stream.{s.venue}", lambda s=s: s.run(stop)) for s in self.streams],
            ("universe", lambda: self.universe.run(stop)),
            ("coinalyze", lambda: self.poller.run(stop)),
            ("health", lambda: self.health.run(stop)),
            ("flush", lambda: self._flush_loop(stop)),
            ("compaction", lambda: self._compaction_loop(stop)),
        ]
        tasks = [
            asyncio.create_task(self._supervise(name, factory, stop), name=name)
            for name, factory in jobs
        ]
        if self.minutes:
            tasks.append(asyncio.create_task(self._deadline(stop), name="deadline"))

        log.info(
            "recorder_started",
            env=settings.env,
            data_dir=str(settings.paths.data_dir),
            streams=[s.venue for s in self.streams],
            universe=len(self.universe.coins),
            minutes=self.minutes,
        )
        try:
            await stop.wait()
        finally:
            log.info("recorder_stopping", buffered_rows=self.buffers.pending_rows())
            stop.set()
            await asyncio.wait(tasks, timeout=SHUTDOWN_GRACE_S)
            for task in [*tasks, state_task]:
                task.cancel()
            await asyncio.gather(*tasks, state_task, return_exceptions=True)
            await self.flush_all()
            await server.stop()
            log.info("recorder_stopped", **self.counters)

    async def _deadline(self, stop: asyncio.Event) -> None:
        """--minutes N: a bounded run, for fixture capture and smoke tests."""
        assert self.minutes is not None
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=self.minutes * 60.0)
        if not stop.is_set():
            log.info("recorder_deadline_reached", minutes=self.minutes)
            stop.set()
