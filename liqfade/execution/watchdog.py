"""The watchdog: a separate process that notices when the trading loop stops breathing.

`scripts/systemd/liqfade-watchdog.service` runs `python -m liqfade.execution.watchdog`
and is deliberately independent of `liqfade-live` and `liqfade-paper`. A watchdog that
dies with the process it watches is not a watchdog, which is why it shares no code path
with the loop and reads only two things off the filesystem:

1. **`data/heartbeat`**, which the loop rewrites every `risk.heartbeat_s`. Its age is
   the whole signal. A missing or unparseable file is *not* a young heartbeat: absence
   reads as stale, because "I cannot tell" and "everything is fine" being the same answer
   is the exact failure this process exists to catch. That reading only holds because the
   loop writes through `write_heartbeat_atomic`: while the write was a plain truncate, a
   read landing inside it was unreadable through no fault of the loop, and this process
   killed a healthy one on 2026-09-07. Absence must mean absence for the rule to be safe.
2. **`data/KILL`**, which silences it, unless the watchdog is the one that wrote it. A
   killed box has a dead loop on purpose and paging about it at 3am is noise that teaches
   the owner to ignore the channel. Its own flag is a different thing: reading that as
   "deliberately dead, all is well" would have it answer that the loop is breathing one
   poll after it declared that the loop is not.

**What it does when the heartbeat goes stale: it writes the flag.** It cannot cancel at
the venue, which needs the agent key and a second process able to sign, and Phase 0 item 4
is still outstanding. It does not need to. `data/KILL` is where the loop's own watcher and
the unit's `ExecStartPre` both look, so writing it cancels, flattens and stops the restart,
and it needs nothing but a filesystem. A loop wedged badly enough not to see the flag is
one systemd will not restart either, once the flag is there.

What remains owed is only the venue side: an order resting at Hyperliquid when the whole
box is gone is cancelled by `scheduleCancel` or by a signing watchdog, and
`scripts/probe/hl_testnet.py schedule_cancel` decides which. That half is named in
docs/DECISIONS.md so it is not mistaken for finished.

The flag is written on the edge only. Rewriting it every poll would move its timestamp and
lose the moment the loop actually stopped, which is the first thing anybody reads.

Restarting the loop is systemd's job, not this process's: `Restart=always` with
`RestartSec=3` already does it, and two things racing to restart one unit is worse than
one. What the watchdog adds is that somebody is told.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from liqfade.config.models import Settings
from liqfade.control.backend import write_kill_file
from liqfade.log import get_logger

log = get_logger(__name__)

__all__ = [
    "Watchdog",
    "build_watchdog",
    "heartbeat_age_s",
    "read_heartbeat",
    "write_heartbeat_atomic",
]

DEFAULT_STALE_S = 10.0  # docs/STRATEGY_AND_RISK.md: stale after ten seconds of silence
DEFAULT_POLL_S = 2.0


def write_heartbeat_atomic(path: Path, stamp_ms: int) -> None:
    """Write the stamp so a concurrent reader sees the old value or the new one, never neither.

    `Path.write_text` truncates in place, which leaves the file empty for as long as it
    takes the writing thread to be scheduled again. There is no lock between the loop and
    the watchdog and there should not be one, so the file itself has to be the atom: write
    a sibling temp file and `os.replace` it, which is a single rename on the same
    filesystem. `read_heartbeat` cannot tell a torn read from a corrupt one, so the only
    place this can be fixed is here.
    """
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(f"{stamp_ms}\n")
    os.replace(tmp, path)


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
        self.kills = 0
        self._killed_by_us = False
        # Wall clock after which a stale heartbeat is judged again. Set when the flag
        # is cleared and at startup, because a loop that has not started yet has not
        # failed yet.
        self._grace_until = 0.0
        # Whether the flag was there last poll, so its removal is an edge we can see.
        self._flag_was_there = self.kill_file.exists()

    async def check(self, now_ms: int | None = None) -> bool:
        """One look. Returns whether the loop is breathing, and posts only on a change."""

        self.checks += 1
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)

        flagged = self.kill_file.exists()
        if self._flag_was_there and not flagged:
            # A kill stops the loop, so its heartbeat has been ageing for as long as the
            # flag was up. The very next poll after the flag is cleared reads that old
            # stamp and writes the flag straight back, before the loop it is judging has
            # had a chance to start: on 2026-09-08 the unit then failed to start 131
            # times. Give it one stale limit to come back. The watchdog exists to notice
            # a loop that stopped, not to judge one that has not started.
            self._grace_until = time.monotonic() + self.stale_s
            log.info("watchdog_grace_started", seconds=self.stale_s)
        self._flag_was_there = flagged

        if flagged:
            if not self._killed_by_us:
                # Somebody else's kill. Deliberately dead; reset the edge so a later
                # restart alerts properly.
                self.healthy = True
                return True
        else:
            # The flag went away, so whoever cleared it owns what happens next.
            self._killed_by_us = False

        age = heartbeat_age_s(self.heartbeat, now_ms=now_ms)
        if (age is None or age > self.stale_s) and time.monotonic() < self._grace_until:
            # Starting, not dead. Reported as healthy so a recovery message is posted when
            # it does come back rather than a second stale alert.
            return True
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
            written = write_kill_file(self.kill_file, f"watchdog: {self.label} is stale. {detail}")
            self._killed_by_us = True
            self.kills += 1
            log.error("watchdog_killed", kill_file=str(self.kill_file), flag_written=written)
            await self._post(
                f"WATCHDOG: {self.label} is not responding. {detail}. "
                + (
                    f"KILL flag written to {self.kill_file}; remove it deliberately."
                    if written
                    else f"KILL FLAG NOT WRITTEN to {self.kill_file}. Act by hand."
                )
            )
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


def build_watchdog(settings: Settings) -> Watchdog:
    """The watchdog the unit runs, from the same config the loop it watches reads.

    The threshold is `risk.watchdog_stale_s` and nothing else. It used to come from
    `LIQFADE_WATCH_STALE_S`, which meant the number the loop is judged by lived in a
    systemd unit while a validated field of the same name sat in the config being ignored.
    The path and the label stay environment overrides: which loop this watchdog watches is
    a property of the unit, not of the config.
    """
    from liqfade.alerts.telegram import send_alert

    heartbeat = Path(os.environ.get("LIQFADE_WATCH_HEARTBEAT") or settings.heartbeat_path)
    return Watchdog(
        heartbeat=heartbeat,
        kill_file=settings.kill_file,
        alert=send_alert,
        stale_s=settings.risk.watchdog_stale_s,
        label=os.environ.get("LIQFADE_WATCH_LABEL") or "trading loop",
    )


def main() -> int:
    """`python -m liqfade.execution.watchdog`, which is what the systemd unit runs."""
    from liqfade.config import load_settings
    from liqfade.log import setup_logging
    from liqfade.runtime import run_main, shutdown_event

    settings = load_settings()
    setup_logging(proc="watchdog", env=settings.env, level=settings.logging.level)
    dog = build_watchdog(settings)
    log.info(
        "watchdog_started",
        heartbeat=str(dog.heartbeat),
        stale_s=dog.stale_s,
        kill_file=str(dog.kill_file),
    )
    run_main(lambda: dog.run(shutdown_event()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
