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
from liqfade.recorder.writer import compact_day, quarantine_rows, write_rows

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
        self.counters: dict[str, int] = {
            "rows_written": 0,
            "files_written": 0,
            "flushes": 0,
            "write_failures": 0,
            "quarantined_rows": 0,
            # Rows the quarantine file itself refused. The shutdown quarantine's usual
            # cause is a full disk, which is also what stops a JSONL append, so this is
            # the counter that says the lake is short and by how much.
            "lost_rows": 0,
        }
        self.streams: list[WSStream] = self._build_streams()
        self.poller = CoinalyzePoller(settings.venues.coinalyze, self.sink)
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
        # Alerts raised from the receive loop, which is sync and cannot await. Drained on
        # every flush tick and at shutdown.
        self._alerts: list[str] = []

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
                    parse=hl.make_parser(),
                    sink=self.sink,
                    cfg=cfg,
                    tables=hl.TABLES,
                    on_connect=hl.on_connect,
                    on_universe_change=hl.on_universe_change,
                    ping_s=venues.hyperliquid.ws_ping_s,
                    ping_frame=hl.PING_FRAME,
                    pong_marker=hl.PONG_MARKER,
                    on_rtt_ms=self.latency.add_ws_ping,
                    # Only Hyperliquid expires a socket on a clock. The other three close
                    # on an error or not at all, and reconnect from the ladder in `run`.
                    rotate_after_s=venues.hyperliquid.ws_rotate_after_s,
                )
            )
        if venues.binance.enabled:
            streams.append(
                WSStream(
                    binance.VENUE,
                    venues.binance.ws_url,
                    parse=binance.make_parser(venues.binance.complete),
                    sink=self.sink,
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
                    sink=self.sink,
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
                    sink=self.sink,
                    cfg=cfg,
                    tables=okx.TABLES,
                    on_connect=okx.on_connect,
                    ping_s=okx.PING_S,
                    ping_frame=okx.PING_FRAME,
                )
            )
        return streams

    # --- the row sink every stream writes through ----------------------------

    def sink(self, table: str, venue: str, row: dict[str, Any]) -> None:
        """Buffer one parsed row, or quarantine it when it cannot be filed.

        Called from the WebSocket receive loop, so it must not raise: an exception here
        takes the connection down. `Buffers.add` refuses a row carrying neither `rx_ms`
        nor `ts_ms`, which used to land in `date=1970-01-01` and look like data forever;
        those go to `data/quarantine/` instead, where a human can see them.
        """
        try:
            self.buffers.add(table, venue, row)
        except ValueError as exc:
            self.quarantine("no_timestamp", table, venue, [row], f"{exc}")

    def quarantine(
        self, reason: str, table: str, venue: str, rows: list[dict[str, Any]], detail: str
    ) -> None:
        """Park rows outside the lake and queue one alert. Never raises into its caller.

        The rows are counted where they actually ended up. They used to be counted as
        quarantined before the file was written, so the one case that loses data (the
        quarantine append fails too, which a full disk causes along with the write that
        sent the rows here) reported itself as a clean save. Rows that get no file are
        counted as lost and alerted as lost, because a number nobody can trust is worse
        than no number when the question is whether the lake is complete.
        """
        try:
            quarantine_rows(self.settings.paths.data_dir, reason, table, venue, rows)
        except Exception as exc:
            self.counters["lost_rows"] += len(rows)
            log.exception(
                "quarantine_failed", reason=reason, table=table, venue=venue, rows=len(rows)
            )
            self._alerts.append(
                f"recorder: {len(rows)} {table}/{venue} row(s) are LOST, quarantine "
                f"({reason}) could not be written either: {type(exc).__name__}"
            )
            return
        self.counters["quarantined_rows"] += len(rows)
        log.warning("rows_quarantined", reason=reason, table=table, venue=venue, detail=detail)
        self._alerts.append(
            f"recorder: {len(rows)} {table}/{venue} row(s) went to quarantine ({reason})"
        )

    async def drain_alerts(self) -> None:
        """Send whatever the sink queued. The sink is sync and cannot await; this can.

        Deduping is the alerter's job (liqfade/alerts/telegram.py), so a storm of bad rows
        is one Telegram message per ten minutes rather than one per row.
        """
        while self._alerts:
            await self.health.alert(self._alerts.pop(0))

    # --- state exposed to the control plane ----------------------------------

    def feed_states(self, at_ms: int) -> list[FeedState]:
        states = [state for stream in self.streams for state in stream.feed_states(at_ms)]
        states.extend(self.poller.feed_states(at_ms))
        return sorted(states, key=lambda state: state.name)

    async def reload(self) -> ReloadResult:
        """Re-resolve config and apply what can change without a restart.

        Universe selection and the health thresholds take effect at the next tick.
        Venue URLs, enabled flags and paths need a restart, because they define the tasks
        that are already running; the returned `changed` list names them anyway so the
        operator can see the reload did not cover them.

        The YAML layers are read on a thread: they are three or four file reads plus
        pydantic validation, on an event loop that is also stamping `rx_ms` on every
        message that arrives. Applying the result stays on the loop, where it is a few
        attribute assignments.
        """
        try:
            settings, (merged, _) = await asyncio.to_thread(self._resolve_config)
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

    def _resolve_config(self) -> tuple[Settings, tuple[dict[str, Any], dict[str, str]]]:
        """The blocking half of `reload`: read and validate the YAML layers."""
        return load_settings(self.settings.env), resolve(self.settings.env)

    # --- universe ------------------------------------------------------------

    async def _apply_universe(self, coins: Sequence[str]) -> bool:
        """Point every stream at the new universe. True only when all of them took it.

        Each stream is applied under its own try/except, so a `ConnectionClosed` in the
        middle of one venue's subscribe frames no longer stops the venues after it. The
        caller keeps the old universe on a False, which is what makes the next refresh
        retry rather than see `coins == self.coins` and report no change; that is how a
        venue used to under-record a coin permanently behind one warning line.
        """
        applied = True
        for stream in self.streams:
            try:
                await stream.set_symbols([to_venue_symbol(coin, stream.venue) for coin in coins])
            except Exception as exc:
                applied = False
                log.warning(
                    "universe_apply_failed",
                    venue=stream.venue,
                    error=f"{type(exc).__name__}: {exc}",
                )
        self.poller.set_universe(list(coins))  # no socket, so nothing here can fail
        return applied

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
        if await self._apply_universe(fallback):
            self.universe.coins = fallback

    # --- flushing ------------------------------------------------------------

    async def _write(self, key: tuple[str, str, str, int]) -> None:
        """Write one buffer, and only then forget the rows it held.

        The rows used to be taken out of the buffer before the write was attempted, so
        any failure (a full disk, one row that fails schema validation) destroyed that
        partition's rows and the supervisor simply restarted the flush loop. Now a
        failure leaves them buffered for the next tick and raises an alert; the cost is
        that a persistent failure grows memory, which is what the alert is for.
        """
        rows = self.buffers.peek(key)
        if not rows:
            return
        table, venue, date, hour = key
        try:
            path = await asyncio.to_thread(write_rows, self.root, key, rows)
        except Exception as exc:
            self.counters["write_failures"] += 1
            log.error(
                "flush_failed",
                table=table,
                venue=venue,
                date=date,
                hour=hour,
                rows=len(rows),
                error=f"{type(exc).__name__}: {exc}",
            )
            await self.health.alert(
                f"recorder: writing {table}/{venue} {date} hour {hour} failed, "
                f"{len(rows)} rows still buffered: {type(exc).__name__}"
            )
            return
        self.buffers.drop_written(key, len(rows))
        if path is not None:
            self.counters["rows_written"] += len(rows)
            self.counters["files_written"] += 1

    async def flush_ready(self) -> None:
        for key in self.buffers.ready(now_ms()):
            await self._write(key)
        await self.drain_alerts()
        self.counters["flushes"] += 1

    async def flush_all(self, *, quarantine_leftovers: bool = False) -> None:
        """Drain every buffer. With `quarantine_leftovers`, lose nothing whatever happens.

        `_write` leaves rows buffered when the write raises, so the next flush tick
        retries them rather than destroying the partition. At shutdown there is no next
        tick: this used to walk the partitions once and whatever was still buffered went
        out with the process, behind a `log.error` and an alert that might not survive
        the shutdown either. So the fix that stopped a failed write destroying rows
        mid-run still lost them at the end of one.

        Every partition is now tried twice, and anything that still will not write goes
        to `data/quarantine/` with the rows a bad timestamp lands in, so there is one
        place to look. Rows are never dropped. The retry is skipped for the flush that
        runs while the streams are still closing, because the second flush a few seconds
        later is the one that has to make the call.
        """
        for key in self.buffers.partitions():
            await self._write(key)
        if not quarantine_leftovers:
            await self.drain_alerts()
            return
        for key in self.buffers.partitions():
            await self._write(key)  # one retry: a full disk that just got space back
        for key in self.buffers.partitions():
            table, venue, date, hour = key
            rows = self.buffers.take(key)
            self.quarantine("write_failed", table, venue, rows, f"{date} hour {hour:02d}")
        await self.drain_alerts()

    async def _flush_shielded(self, *, quarantine_leftovers: bool = False) -> None:
        """flush_all, run to completion even if this coroutine is cancelled.

        Shutdown happens inside `runtime.run_main`'s grace window, which cancels the main
        coroutine when it expires. Without the shield that cancel lands on the flush and
        every partition it had not reached yet goes out with the process: at shutdown
        there is no next flush tick to retry on, so those rows are simply gone. The loop
        re-awaits because a shield only protects the inner task, not the await on it.
        """
        flush = asyncio.ensure_future(self.flush_all(quarantine_leftovers=quarantine_leftovers))
        while not flush.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(flush)

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
            alert=self.health.alert,
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
            # Drain before the grace window, not only after it. The streams are already
            # closing, and a flush that starts once the window has been spent is the one
            # a cancel catches. The second pass picks up whatever arrived meanwhile;
            # writing two files for the same hour is what compaction is for.
            await self._flush_shielded()
            await asyncio.wait(tasks, timeout=SHUTDOWN_GRACE_S)
            for task in [*tasks, state_task]:
                task.cancel()
            await asyncio.gather(*tasks, state_task, return_exceptions=True)
            # The last chance any of these rows will get: retry, then quarantine.
            await self._flush_shielded(quarantine_leftovers=True)
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
