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
from collections.abc import Callable, Sequence
from typing import Any

from liqfade.config.models import Settings
from liqfade.control.protocol import (
    FeedState,
    FlatResult,
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
    """Rolling WS ping and REST round trip times, in milliseconds."""

    def __init__(self, window: int = LATENCY_WINDOW) -> None:
        self.window = window
        self.ws_ping: list[float] = []
        self.rest: list[float] = []

    def _add(self, series: list[float], value: float) -> None:
        series.append(value)
        if len(series) > self.window:
            del series[: len(series) - self.window]

    def add_ws_ping(self, ms: float) -> None:
        self._add(self.ws_ping, ms)

    def add_rest(self, ms: float) -> None:
        self._add(self.rest, ms)

    def state(self) -> LatencyState:
        return LatencyState(
            ws_ping_p50=_percentile(self.ws_ping, 0.5),
            ws_ping_p95=_percentile(self.ws_ping, 0.95),
            rest_p50=_percentile(self.rest, 0.5),
            rest_p95=_percentile(self.rest, 0.95),
            samples=len(self.ws_ping) + len(self.rest),
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
        reload: Callable[[], ReloadResult],
        started_ms: int | None = None,
    ) -> None:
        self.settings = settings
        self._feeds = feeds
        self._universe = universe
        self._latency = latency
        self._counters = counters
        self._stop = stop
        self._reload = reload
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
        self._stop.set()
        return KillResult(killed=True, reason=reason or "kill via control plane")

    async def reload_config(self) -> ReloadResult:
        return self._reload()

    async def get_metrics(self) -> MetricsResult:
        return MetricsResult(
            feeds=self._feeds(now_ms()),
            latency=self._latency.state(),
            counters=dict(self._counters),
        )

    def snapshot_source(self) -> Callable[[], Any]:
        """The StateWriter wants a zero argument coroutine factory."""
        return self.status
