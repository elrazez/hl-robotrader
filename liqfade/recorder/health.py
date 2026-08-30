"""Gap, silence and disk checks, and the Telegram alerts they raise.

Two different questions get asked of a stream, and confusing them is how a recorder ends
up either silently losing data or paging its owner every ten minutes:

- A *continuous* stream (HL trades, bbo, l2Book, and Bybit liquidations, which are
  complete rather than sampled) is expected to say something constantly. Silence past
  `recorder.gap_alert_s` is a gap and is counted; silence past
  `recorder.silence_alert_s` is an outage and is alerted.
- A *sparse* stream (Binance and OKX liquidations, both throttled and event driven) is
  quiet whenever the market is calm. Silence there means nothing, so only the connection
  being down is alerted, which the ping/pong on the socket already tells us.

Disk is checked every five minutes with `shutil.disk_usage`, and `data/ALERT_DISK`, the
flag the VPS cron writes at 80%, is treated as an alert in its own right so the two
paths agree.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from liqfade.alerts.telegram import send_alert
from liqfade.config.models import Recorder
from liqfade.control.protocol import now_ms
from liqfade.log import get_logger
from liqfade.recorder.streams.base import StreamStats

log = get_logger(__name__)

CHECK_INTERVAL_S = 5.0
DISK_INTERVAL_S = 300.0
DISK_FLAG = "ALERT_DISK"

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
    ) -> None:
        self.cfg = cfg
        self.data_dir = Path(data_dir)
        self.sources = sources
        self.alert = alert
        self.check_s = check_s
        self.disk_s = disk_s
        self._next_disk_check = 0.0

    def apply_config(self, cfg: Recorder) -> None:
        self.cfg = cfg

    def _all_stats(self) -> list[StreamStats]:
        return [stat for source in self.sources for stat in source.stats.values()]

    async def check_streams(self, at_ms: int | None = None) -> list[str]:
        """Count gaps and alert on outages. Returns the alert texts dispatched."""
        at_ms = now_ms() if at_ms is None else at_ms
        sent: list[str] = []
        for stat in self._all_stats():
            if stat.check_silence(self.cfg.gap_alert_s, at_ms):
                log.warning("stream_gap", stream=stat.name, silent_s=round(stat.silent_s(at_ms), 1))
            silent_s = stat.silent_s(at_ms)
            if stat.continuous and silent_s > self.cfg.silence_alert_s:
                sent.append(f"recorder: {stat.name} silent for {silent_s:.0f}s")
            elif not stat.connected and stat.last_msg_ms:
                # Sparse feeds are quiet by nature, so only a dead socket is news.
                sent.append(f"recorder: {stat.name} disconnected")
        for text in sent:
            await self.alert(text)
        return sent

    async def check_disk(self) -> list[str]:
        """Disk usage plus the cron written flag file. Returns the alerts dispatched."""
        sent: list[str] = []
        stats = disk_stats(self.data_dir)
        log.info("disk_usage", **stats)
        if stats["used_pct"] >= self.cfg.disk_alert_pct:
            sent.append(
                f"recorder: disk {stats['used_pct']:.0f}% used on {stats['path']} "
                f"({stats['free_bytes'] / 1e9:.1f} GB free)"
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
