"""The ControlBackend protocol and a no-op implementation.

The live loop will implement ControlBackend for real in Phase 5. Until then NoopBackend
returns canned but schema valid state so the panel, the CLI and the Telegram bot can be
built and tested against the same surface the real loop will present.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

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

log = get_logger(__name__)


def write_kill_file(path: Path, reason: str | None = None) -> bool:
    """Create the kill flag, returning whether it is now on disk.

    This is the durable half of a kill and the reason it is written first, before
    anything is cancelled or closed: the live systemd unit gates on this file but carries
    Restart=always, so a kill that only stopped the loop was undone three seconds later,
    and a crash midway through flattening left nothing behind. Removing the file stays a
    deliberate manual act, per docs/RUNBOOK.md.

    A write failure is logged and returned rather than raised: the in-process half of the
    kill still has to run, but the caller has to report the failure instead of confirming
    a kill it did not get.

    **An existing flag is left exactly as it is.** The reason in it is the first thing
    anybody reads at 3am, and it belongs to whoever killed first. Drilled on 2026-09-08:
    the watchdog wrote "paper loop is stale", the trading loop saw the flag and rewrote it
    with "kill file at data/KILL", and the diagnosis was gone by the time anyone looked.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            # A file, specifically: a directory sitting where the flag must go is a write
            # that will fail, not a kill that already happened.
            return True
        stamp = f"killed_at: {datetime.now(UTC).isoformat()}\n"
        if reason:
            stamp += f"reason: {reason}\n"
        path.write_text(stamp)
    except OSError as exc:
        # What matters is that the flag is on disk, not that we are the ones who put it
        # there. A flag the owner dropped by hand as root cannot be overwritten by the
        # service user, and the 2026-09-06 drill on the VPS reported "NOT written, a
        # restart will not stay killed" about a file sitting right there. That is a false
        # alarm in the one moment where the message has to be trusted.
        if _flag_present(path):
            log.warning(
                "kill_file_not_updated",
                path=str(path),
                error=f"{type(exc).__name__}: {exc}",
                detail="the flag is already on disk, so the kill still persists",
            )
            return True
        log.error("kill_file_write_failed", path=str(path), error=f"{type(exc).__name__}: {exc}")
        return False
    log.warning("kill_file_written", path=str(path), reason=reason)
    return True


def _flag_present(path: Path) -> bool:
    """Whether the flag is definitely on disk. Anything we cannot see counts as absent.

    The opposite bias to `Settings.kill_switch_present`, deliberately, because the two
    answer different questions. That gate asks "is the kill switch on", where not being
    able to tell has to mean yes. This asks "did the flag survive", where not being able
    to tell has to mean no: reporting a kill as persisted on the strength of an error is
    how a restart comes back trading.
    """
    try:
        # A regular file specifically. A directory sitting where the flag belongs is a
        # broken install, not a flag we wrote, and it must not talk the kill into
        # reporting that it persisted.
        return path.is_file()
    except OSError:
        return False


def kill_not_persisted(path: Path, reason: str | None) -> str:
    """The reason a kill reports when the flag never reached disk.

    The flag is the half of a kill that outlives the process: the live unit carries
    Restart=always and gates on it, so without the file the box comes back trading three
    seconds later. Every reader of a KillResult renders `reason`, so putting the failure
    there is what makes the Telegram confirmation, the panel and `liqfade status` all say
    the kill did not fully take.
    """
    failed = f"kill flag {path} was NOT written, a restart will not stay killed"
    return f"{reason}; {failed}" if reason else failed


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

    async def inject_intent(self, **args: Any) -> InjectResult: ...


class NoopBackend:
    """Canned responses with no side effects beyond its own in-memory flags.

    Used by `liqfade panel` before the live loop exists and by the control tests. It never
    places, cancels or closes anything, so it is safe to run anywhere.
    """

    def __init__(
        self,
        *,
        env: str = "paper",
        proc: str = "noop",
        kill_file: Path | None = None,
    ) -> None:
        self.env = env
        self.proc = proc
        self.kill_file = kill_file
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
        if self.kill_file is None:
            killed, reported = True, reason
        else:
            killed = write_kill_file(self.kill_file, reason)
            reported = reason if killed else kill_not_persisted(self.kill_file, reason)
        # The in-process half runs either way, but `killed` reports what was actually
        # achieved: the kill switch never claims a success it did not get.
        self._killed = True
        self._paused = True
        return KillResult(killed=killed, canceled_orders=0, closed_positions=0, reason=reported)

    async def reload_config(self) -> ReloadResult:
        self._record("reload_config")
        return ReloadResult(changed=[])

    async def get_metrics(self) -> MetricsResult:
        self._record("get_metrics")
        return MetricsResult(counters={"intents": 0, "fills": 0, "rejects": 0})

    async def inject_intent(self, **args: Any) -> InjectResult:
        """A no-op backend has no book to price a drill against, so it refuses."""
        self._record("inject_intent", **args)
        return InjectResult(injected=False, reason="no trading loop behind this backend")
