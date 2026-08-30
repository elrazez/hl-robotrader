"""The ControlBackend protocol and a no-op implementation.

The live loop will implement ControlBackend for real in Phase 5. Until then NoopBackend
returns canned but schema valid state so the panel, the CLI and the Telegram bot can be
built and tested against the same surface the real loop will present.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

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


@runtime_checkable
class ControlBackend(Protocol):
    """What a process must implement to accept control commands.

    Every method must return quickly. A backend that needs to do slow work (flattening,
    killing) starts it and returns what it started, it does not wait for completion.
    """

    async def status(self) -> StateSnapshot: ...

    async def pause(self, reason: str | None = None) -> PauseResult: ...

    async def resume(self) -> ResumeResult: ...

    async def flat(self, coin: str | None = None) -> FlatResult: ...

    async def kill(self, reason: str | None = None) -> KillResult: ...

    async def reload_config(self) -> ReloadResult: ...

    async def get_metrics(self) -> MetricsResult: ...


class NoopBackend:
    """Canned responses with no side effects beyond its own in-memory flags.

    Used by `liqfade panel` before the live loop exists and by the control tests. It never
    places, cancels or closes anything, so it is safe to run anywhere.
    """

    def __init__(self, *, env: str = "paper", proc: str = "noop") -> None:
        self.env = env
        self.proc = proc
        self._paused = False
        self._killed = False
        self._started_ms = now_ms()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))

    async def status(self) -> StateSnapshot:
        self._record("status")
        return StateSnapshot(
            proc=self.proc,
            env=self.env,
            live_trading=False,
            uptime_s=(now_ms() - self._started_ms) / 1000.0,
            feeds=[
                FeedState(
                    name="hyperliquid.trades",
                    connected=False,
                    last_msg_ms=0,
                    lag_ms=0,
                )
            ],
            positions=[],
            orders=[],
            risk=RiskState(paused=self._paused, killed=self._killed),
            latency=LatencyState(),
            universe=[],
        )

    async def pause(self, reason: str | None = None) -> PauseResult:
        self._record("pause", reason=reason)
        self._paused = True
        return PauseResult(paused=True, reason=reason)

    async def resume(self) -> ResumeResult:
        self._record("resume")
        if self._killed:
            return ResumeResult(paused=True, halt_cleared=False, reason="killed, restart required")
        self._paused = False
        return ResumeResult(paused=False, halt_cleared=True)

    async def flat(self, coin: str | None = None) -> FlatResult:
        self._record("flat", coin=coin)
        return FlatResult(closing=[coin] if coin else [], canceled_orders=0)

    async def kill(self, reason: str | None = None) -> KillResult:
        self._record("kill", reason=reason)
        self._killed = True
        self._paused = True
        return KillResult(killed=True, canceled_orders=0, closed_positions=0, reason=reason)

    async def reload_config(self) -> ReloadResult:
        self._record("reload_config")
        return ReloadResult(changed=[])

    async def get_metrics(self) -> MetricsResult:
        self._record("get_metrics")
        return MetricsResult(counters={"intents": 0, "fills": 0, "rejects": 0})
