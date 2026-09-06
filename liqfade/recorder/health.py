"""Gap, silence and disk checks, and the Telegram alerts they raise.

Two different questions get asked of a stream, and confusing them is how a recorder ends
up either silently losing data or paging its owner every ten minutes:

- A *continuous* stream (HL trades, bbo, l2Book) is expected to say something
  constantly. Silence past `recorder.gap_alert_s` is a gap and is counted; silence past
  `recorder.silence_alert_s` is an outage and is alerted. On the Hyperliquid streams that
  question is asked per coin, because HL subscribes each coin separately and one dead
  subscription is invisible in a stream-level number that nineteen other coins keep fresh.
- A *sparse* stream (every liquidation feed, Bybit's complete one included; all event
  driven) is quiet whenever the market is calm. Silence there means nothing, so only the connection
  being down is alerted, which the ping/pong on the socket already tells us. A stream
  that has not delivered yet is exempt until `startup_grace_s`: every stream is
  disconnected for the first moments of a process, and alerting on that put one message
  per declared stream into Telegram on every restart.

A stream that has never delivered *anything* is a fourth case, and the one the other
three miss: no gap is counted (there is no last message to measure from), no silence is
alerted (same reason), and the socket is up. After `startup_grace_s` that is a fact about
the feed, not a quiet market, and it is said once.

Parse errors are the third question, and the one neither of the others can answer. A
venue renaming a field leaves the socket connected and the message rate normal while
every row is dropped, so the only evidence is the count of rows the parser refused. Past
`recorder.parse_error_alert_1h` per stream per hour, that is an alert.

Disk is checked every five minutes with `shutil.disk_usage`, and `data/ALERT_DISK`, the
flag the VPS cron writes at 80%, is treated as an alert in its own right so the two
paths agree.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from liqfade.alerts.telegram import send_alert
from liqfade.config.models import Recorder
from liqfade.control.protocol import now_ms
from liqfade.log import get_logger
from liqfade.recorder.streams.base import StreamStats, gap_threshold_s, silence_threshold_s

log = get_logger(__name__)

CHECK_INTERVAL_S = 5.0
DISK_INTERVAL_S = 300.0
DISK_FLAG = "ALERT_DISK"
# How long a declared stream may deliver nothing before that is treated as a fact rather
# than a quiet market. An hour is generous even for Binance's throttled liquidation feed,
# which is the point: the failure this catches is permanent, so a slow answer is fine and
# a false alarm on a genuinely calm hour is not.
STARTUP_GRACE_S = 3600.0

Alerter = Callable[[str], Awaitable[bool]]


class HasStats(Protocol):
    """Anything the recorder runs that reports per stream health."""

    stats: dict[str, StreamStats]


def disk_stats(path: Path) -> dict[str, Any]:
    """Bytes used and free on the filesystem holding `path`, plus the percent used.

    The panel reads this too, so there is one definition of "how full is the disk".
    Falls back to the nearest existing parent, because the data dir may not exist yet on
    a first run.
    """
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    return {
        "path": str(path),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "used_pct": round(usage.used / usage.total * 100.0, 2) if usage.total else 0.0,
    }


class Health:
    """Runs the checks on a timer and dispatches alerts. Owns no state but timers."""

    def __init__(
        self,
        cfg: Recorder,
        data_dir: Path,
        sources: Sequence[HasStats],
        *,
        alert: Alerter = send_alert,
        check_s: float = CHECK_INTERVAL_S,
        disk_s: float = DISK_INTERVAL_S,
        startup_grace_s: float = STARTUP_GRACE_S,
    ) -> None:
        self.cfg = cfg
        self.data_dir = Path(data_dir)
        self.sources = sources
        self.alert = alert
        self.check_s = check_s
        self.disk_s = disk_s
        self.startup_grace_s = startup_grace_s
        self.started = time.monotonic()
        self._next_disk_check = 0.0
        # Streams already reported as never having delivered. A feed that will never
        # deliver is one discovery, not a condition worth paging about every five seconds.
        self._never_delivered: set[str] = set()

    def apply_config(self, cfg: Recorder) -> None:
        self.cfg = cfg

    def _past_grace(self, now: float | None = None) -> bool:
        return (time.monotonic() if now is None else now) - self.started > self.startup_grace_s

    def _all_stats(self) -> list[StreamStats]:
        return [stat for source in self.sources for stat in source.stats.values()]

    async def check_streams(self, at_ms: int | None = None) -> list[str]:
        """Count gaps and alert on outages. Returns the alert texts dispatched."""
        at_ms = now_ms() if at_ms is None else at_ms
        sent: list[str] = []
        for stat in self._all_stats():
            for label in stat.check_silence(gap_threshold_s(stat, self.cfg), at_ms):
                # `label` is a coin on a per-coin stream and "" for the stream as a whole.
                log.warning(
                    "stream_gap",
                    stream=stat.name,
                    coin=label or None,
                    silent_s=round(stat.silent_s(at_ms, coin=label or None), 1),
                )
            errors = stat.parse_errors_1h()
            if errors >= self.cfg.parse_error_alert_1h:
                sent.append(
                    f"recorder: {stat.name} parse errors over "
                    f"{self.cfg.parse_error_alert_1h}/h, the wire has stopped matching the parser"
                )
            silent_s = stat.silent_s(at_ms)
            outage_s = silence_threshold_s(stat, self.cfg)
            if stat.continuous and silent_s > outage_s:
                # The text carries the threshold, not the elapsed time. It used to say
                # "silent for {n}s", and `telegram._deduped` keys on the exact string, so
                # one ten minute outage sent about 120 messages and hit the rate limit.
                # Constant per stream per outage, so the dedupe collapses it to one every
                # DEDUPE_S, which is the "still down" reminder anyone would want.
                worst = stat.worst_coin(at_ms)[0] if stat.per_coin_gaps else None
                sent.append(
                    f"recorder: {stat.name} silent for over {outage_s:.0f}s"
                    + (f" (worst coin {worst})" if worst else "")
                )
            elif not stat.connected and (stat.last_msg_ms or self._past_grace()):
                # Sparse feeds are quiet by nature, so only a dead socket is news. A
                # stream that has delivered before is reported the moment it drops; one
                # that never has waits for the startup grace, because every stream is
                # disconnected until its first connect returns and an unconditional alert
                # here posted one Telegram message per declared stream on every restart.
                # Nine different texts, so the alerter's dedupe could not collapse them.
                # The grace is the same one the never-delivered branch below uses, and
                # a socket that never comes up is still said out loud, just not at t=0.
                sent.append(f"recorder: {stat.name} disconnected")
            elif (
                stat.last_msg_ms == 0
                and self._past_grace()
                and stat.name not in (self._never_delivered)
            ):
                # Connected, held open, and not one message since the process started.
                # That is the Binance regional block docs/DATA_SOURCES.md warns about,
                # and nothing else here can see it: no gap, no silence, no disconnect.
                self._never_delivered.add(stat.name)
                sent.append(
                    f"recorder: {stat.name} is connected but has never delivered a message, "
                    f"{self.startup_grace_s / 3600:.0f}h after start"
                )
        for text in sent:
            await self.alert(text)
        return sent

    async def check_disk(self) -> list[str]:
        """Disk usage plus the cron written flag file. Returns the alerts dispatched."""
        sent: list[str] = []
        stats = disk_stats(self.data_dir)
        log.info("disk_usage", **stats)
        if stats["used_pct"] >= self.cfg.disk_alert_pct:
            # The threshold, not the current numbers: free bytes move every check, and a
            # text that moves defeats the alerter's dedupe. The `disk_usage` log line and
            # the panel both carry how full it actually is.
            sent.append(
                f"recorder: disk over {self.cfg.disk_alert_pct:.0f}% used on {stats['path']}"
            )
        flag = self.data_dir / DISK_FLAG
        if flag.exists():
            with contextlib.suppress(OSError):
                sent.append(f"recorder: {flag.read_text().strip() or 'disk alert flag present'}")
        for text in sent:
            await self.alert(text)
        return sent

    async def run(self, stop: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            try:
                await self.check_streams()
                if loop.time() >= self._next_disk_check:
                    self._next_disk_check = loop.time() + self.disk_s
                    await self.check_disk()
            except Exception:
                log.exception("health_check_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.check_s)
