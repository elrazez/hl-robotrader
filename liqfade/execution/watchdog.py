"""The watchdog: a separate process that notices when the trading loop stops breathing.

`scripts/systemd/liqfade-watchdog.service` runs `python -m liqfade.execution.watchdog`
and is deliberately independent of `liqfade-live` and `liqfade-paper`. A watchdog that
dies with the process it watches is not a watchdog, which is why it shares no code path
with the loop and reads only two things off the filesystem:

1. **`data/heartbeat`**, which the loop rewrites every second. Its age is the whole
   signal. A missing or unparseable file is *not* a young heartbeat: absence reads as
   stale, because "I cannot tell" and "everything is fine" being the same answer is the
   exact failure this process exists to catch.
2. **`data/KILL`**, which silences it. A killed box has a dead loop on purpose and paging
   about it at 3am is noise that teaches the owner to ignore the channel.

**What it does not do, in 05a: it does not cancel.** `docs/STRATEGY_AND_RISK.md` wants a
watchdog that cancels every resting order when the heartbeat goes stale, and doing that
needs the agent key, which means a second process able to sign. Phase 0 item 4 is still
outstanding, so there is nothing to cancel and no key to cancel it with; this ships as the
alerting half and `scripts/probe/hl_testnet.py schedule_cancel` decides whether the
cancelling half is `scheduleCancel` at the venue or a signing watchdog here. Both halves
are named in docs/DECISIONS.md so neither is mistaken for finished.

Restarting the loop is systemd's job, not this process's: `Restart=always` with
`RestartSec=3` already does it, and two things racing to restart one unit is worse than
one. What the watchdog adds is that somebody is told.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from liqfade.log import get_logger

log = get_logger(__name__)

__all__ = ["Watchdog", "heartbeat_age_s", "read_heartbeat"]

DEFAULT_STALE_S = 10.0  # docs/STRATEGY_AND_RISK.md: stale after ten seconds of silence
DEFAULT_POLL_S = 2.0


def read_heartbeat(path: Path) -> int | None:
    """The stamp in the heartbeat file, or None when it is missing or unreadable.

    None is the answer for every failure on purpose. A heartbeat that cannot be read has
    to be indistinguishable from one that is not being written, or a corrupted file reads
    as a healthy loop forever.
    """
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def heartbeat_age_s(path: Path, *, now_ms: int) -> float | None:
    """Seconds since the loop last breathed, or None when there is no heartbeat at all."""
    stamp = read_heartbeat(path)
    if stamp is None:
        return None
    return (now_ms - stamp) / 1000.0


class Watchdog:
    """Polls the heartbeat and alerts on the edges, not on every tick."""

    def __init__(
        self,
        *,
        heartbeat: Path,
        kill_file: Path,
        alert: Callable[[str], Awaitable[bool]] | None = None,
        stale_s: float = DEFAULT_STALE_S,
        poll_s: float = DEFAULT_POLL_S,
        label: str = "trading loop",
    ) -> None:
        self.heartbeat = Path(heartbeat)
        self.kill_file = Path(kill_file)
        self._alert = alert
        self.stale_s = stale_s
        self.poll_s = poll_s
        self.label = label
        self.healthy = True
        self.checks = 0
        self.alerts = 0

    async def check(self, now_ms: int | None = None) -> bool:
        """One look. Returns whether the loop is breathing, and posts only on a change."""
        import time

        self.checks += 1
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)

        if self.kill_file.exists():
            # Deliberately dead. Reset the edge so a later restart alerts properly.
            self.healthy = True
            return True

        age = heartbeat_age_s(self.heartbeat, now_ms=now_ms)
        if age is None:
            return await self._unhealthy(f"no heartbeat file at {self.heartbeat}")
        if age > self.stale_s:
            return await self._unhealthy(
                f"heartbeat is {age:.1f}s old, over the {self.stale_s:.0f}s limit"
            )
        return await self._healthy(age)

    async def _unhealthy(self, detail: str) -> bool:
        if self.healthy:
            self.healthy = False
            self.alerts += 1
            log.error(
                "watchdog_stale", label=self.label, detail=detail, heartbeat=str(self.heartbeat)
            )
            await self._post(f"WATCHDOG: {self.label} is not responding. {detail}")
        return False

    async def _healthy(self, age: float) -> bool:
        if not self.healthy:
            self.healthy = True
            log.warning("watchdog_recovered", label=self.label, age_s=round(age, 2))
            await self._post(f"WATCHDOG: {self.label} recovered, heartbeat {age:.1f}s old")
        return True

    async def _post(self, text: str) -> None:
        if self._alert is None:
            return
        with contextlib.suppress(Exception):
            # Telegram being down must never take the watchdog down with it.
            await self._alert(text)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.check()
            except Exception:
                log.exception("watchdog_check_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.poll_s)


def main() -> int:
    """`python -m liqfade.execution.watchdog`, which is what the systemd unit runs."""
    from liqfade.alerts.telegram import send_alert
    from liqfade.config import load_settings
    from liqfade.log import setup_logging
    from liqfade.runtime import run_main, shutdown_event

    settings = load_settings()
    setup_logging(proc="watchdog", env=settings.env, level=settings.logging.level)
    # The unit watching the paper loop points here with LIQFADE_WATCH_HEARTBEAT; without
    # it the watchdog reads the same path the live loop writes, which is the default the
    # live unit uses.
    heartbeat = Path(os.environ.get("LIQFADE_WATCH_HEARTBEAT") or settings.heartbeat_path)
    dog = Watchdog(
        heartbeat=heartbeat,
        kill_file=settings.kill_file,
        alert=send_alert,
        stale_s=float(os.environ.get("LIQFADE_WATCH_STALE_S") or DEFAULT_STALE_S),
        label=os.environ.get("LIQFADE_WATCH_LABEL") or "trading loop",
    )
    log.info(
        "watchdog_started",
        heartbeat=str(heartbeat),
        stale_s=dog.stale_s,
        kill_file=str(settings.kill_file),
    )
    run_main(lambda: dog.run(shutdown_event()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
