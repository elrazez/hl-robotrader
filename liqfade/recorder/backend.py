"""The recorder's ControlBackend: what `liqfade status`, Telegram and the panel see.

The recorder answers `status` and `get_metrics` with real feed health and refuses the
trading commands outright rather than pretending. `pause`, `resume` and `flat` are
meaningless for a process that only writes files, and a backend that quietly returned
"ok" to `flat` would be the kind of thing that reads as safe at 3am and is not.

`kill` is honoured: it sets the shutdown event, which flushes the buffers and stops the
process. Nothing is at risk, but the kill switch having one meaning everywhere matters
more than the recorder being a special case.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from liqfade.config.models import Settings
from liqfade.control.backend import kill_not_persisted, write_kill_file
from liqfade.control.protocol import (
    FeedState,
    FlatResult,
    InjectResult,
    KillResult,
    LatencyState,
    MetricsResult,
    PauseResult,
    ReloadResult,
    ResumeResult,
    RiskState,
    StateSnapshot,
    now_ms,
)
from liqfade.log import get_logger
from liqfade.recorder.health import Alerter

log = get_logger(__name__)

LATENCY_WINDOW = 300  # samples kept per series, per the Phase 1 prompt
NOT_SUPPORTED = "not supported by recorder"


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
    return round(ordered[index], 3)


class LatencyTracker:
    """Rolling WS ping and REST round trip times, in milliseconds, with their timestamps.

    The timestamps are kept because the Phase 1 DoD asks for these as time series and a
    p50 and a p95 cannot show a step change, a drift or a bad five minutes. `samples` is
    still the total count, so nothing that reads the scalars changes.
    """

    def __init__(self, window: int = LATENCY_WINDOW) -> None:
        self.window = window
        self.ws_ping: list[tuple[int, float]] = []
        self.rest: list[tuple[int, float]] = []
        # Submit to ack, which only a loop with a venue produces. Empty in the
        # recorder and in paper, where nothing is submitted to an exchange.
        self.submit_ack: list[tuple[int, float]] = []

    def _add(self, series: list[tuple[int, float]], value: float, at_ms: int | None) -> None:
        series.append((now_ms() if at_ms is None else at_ms, round(value, 3)))
        if len(series) > self.window:
            del series[: len(series) - self.window]

    def add_ws_ping(self, ms: float, at_ms: int | None = None) -> None:
        self._add(self.ws_ping, ms, at_ms)

    def add_rest(self, ms: float, at_ms: int | None = None) -> None:
        self._add(self.rest, ms, at_ms)

    def add_submit_ack(self, ms: float, at_ms: int | None = None) -> None:
        """One order's submit to ack, in ms. Fed by the order manager, not by a probe."""
        self._add(self.submit_ack, ms, at_ms)

    def state(self) -> LatencyState:
        ws_ping = [ms for _, ms in self.ws_ping]
        rest = [ms for _, ms in self.rest]
        submit_ack = [ms for _, ms in self.submit_ack]
        return LatencyState(
            ws_ping_p50=_percentile(ws_ping, 0.5),
            ws_ping_p95=_percentile(ws_ping, 0.95),
            rest_p50=_percentile(rest, 0.5),
            rest_p95=_percentile(rest, 0.95),
            submit_ack_p50=_percentile(submit_ack, 0.5),
            submit_ack_p95=_percentile(submit_ack, 0.95),
            samples=len(ws_ping) + len(rest) + len(submit_ack),
            ws_ping_samples=list(self.ws_ping),
            rest_samples=list(self.rest),
            submit_ack_samples=list(self.submit_ack),
        )


class RecorderBackend:
    """Reads the recorder's live state. Holds no data of its own beyond the counters."""

    def __init__(
        self,
        settings: Settings,
        *,
        feeds: Callable[[int], list[FeedState]],
        universe: Callable[[], Sequence[str]],
        latency: LatencyTracker,
        counters: dict[str, int],
        stop: asyncio.Event,
        reload: Callable[[], Awaitable[ReloadResult]],
        alert: Alerter | None = None,
        started_ms: int | None = None,
    ) -> None:
        self.settings = settings
        self._feeds = feeds
        self._universe = universe
        self._latency = latency
        self._counters = counters
        self._stop = stop
        self._reload = reload
        self._alert = alert
        self._started_ms = started_ms or now_ms()

    async def status(self) -> StateSnapshot:
        at = now_ms()
        return StateSnapshot(
            proc="recorder",
            env=self.settings.env,
            ts_ms=at,
            live_trading=False,
            uptime_s=(at - self._started_ms) / 1000.0,
            feeds=self._feeds(at),
            positions=[],
            orders=[],
            risk=RiskState(paused=False, killed=self._stop.is_set()),
            latency=self._latency.state(),
            universe=list(self._universe()),
        )

    async def pause(self, reason: str | None = None) -> PauseResult:
        raise RuntimeError(NOT_SUPPORTED)

    async def resume(self) -> ResumeResult:
        raise RuntimeError(NOT_SUPPORTED)

    async def flat(self, coin: str | None = None) -> FlatResult:
        raise RuntimeError(NOT_SUPPORTED)

    async def kill(self, reason: str | None = None) -> KillResult:
        log.warning("recorder_kill", reason=reason)
        # The flag first: it is the half that outlives this process and that the live
        # unit gates on. Stopping the recorder alone would leave nothing behind.
        written = write_kill_file(self.settings.kill_file, reason)
        self._stop.set()
        given = reason or "kill via control plane"
        if written:
            return KillResult(killed=True, reason=given)
        # The process still stops, but a kill without its flag is not the kill that was
        # asked for, so it is reported as a failure and pushed to Telegram rather than
        # confirmed. Nobody should have to read the journal to find out.
        failed = kill_not_persisted(self.settings.kill_file, given)
        if self._alert is not None:
            await self._alert(f"recorder: {failed}")
        return KillResult(killed=False, reason=failed)

    async def inject_intent(self, **args: Any) -> InjectResult:
        raise RuntimeError(NOT_SUPPORTED)

    async def reload_config(self) -> ReloadResult:
        return await self._reload()

    async def get_metrics(self) -> MetricsResult:
        return MetricsResult(
            feeds=self._feeds(now_ms()),
            latency=self._latency.state(),
            counters=dict(self._counters),
        )

    def snapshot_source(self) -> Callable[[], Any]:
        """The StateWriter wants a zero argument coroutine factory."""
        return self.status
