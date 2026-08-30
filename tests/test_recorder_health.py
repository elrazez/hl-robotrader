"""Silence, disconnect and disk alerts, and the gap counting behind them."""

from __future__ import annotations

from pathlib import Path

from liqfade.config import load_settings
from liqfade.control.protocol import now_ms
from liqfade.recorder.health import Health, disk_stats
from liqfade.recorder.streams.base import StreamStats

CFG = load_settings("paper").recorder


class Source:
    def __init__(self, *stats: StreamStats) -> None:
        self.stats = {stat.name: stat for stat in stats}


class Collector:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, text: str) -> bool:
        self.sent.append(text)
        return True


def _health(tmp_path: Path, *stats: StreamStats) -> tuple[Health, Collector]:
    alerts = Collector()
    return Health(CFG, tmp_path, [Source(*stats)], alert=alerts), alerts


async def test_a_continuous_stream_gone_quiet_alerts_and_counts_a_gap(tmp_path: Path) -> None:
    stat = StreamStats(name="hyperliquid.trades", continuous=True, connected=True)
    at = now_ms()
    stat.record_message(at - 120_000)
    health, alerts = _health(tmp_path, stat)
    await health.check_streams(at)
    assert stat.gaps_1h == 1
    assert alerts.sent == ["recorder: hyperliquid.trades silent for 120s"]


async def test_a_sparse_stream_being_quiet_is_not_an_alert(tmp_path: Path) -> None:
    stat = StreamStats(name="binance.liquidations", continuous=False, connected=True)
    at = now_ms()
    stat.record_message(at - 3_600_000)
    health, alerts = _health(tmp_path, stat)
    await health.check_streams(at)
    assert alerts.sent == []
    assert stat.gaps_1h == 0


async def test_a_sparse_stream_with_a_dead_socket_is_an_alert(tmp_path: Path) -> None:
    """For a feed that is quiet by nature, the connection is the only signal."""
    stat = StreamStats(name="binance.liquidations", continuous=False, connected=False)
    at = now_ms()
    stat.record_message(at - 1000)
    health, alerts = _health(tmp_path, stat)
    await health.check_streams(at)
    assert alerts.sent == ["recorder: binance.liquidations disconnected"]


async def test_a_stream_that_has_never_delivered_does_not_alert_on_startup(
    tmp_path: Path,
) -> None:
    stat = StreamStats(name="okx.liquidations", continuous=False, connected=False)
    health, alerts = _health(tmp_path, stat)
    await health.check_streams(now_ms())
    assert alerts.sent == []


async def test_disk_over_the_threshold_alerts(tmp_path: Path) -> None:
    health, alerts = _health(tmp_path)
    health.cfg = CFG.model_copy(update={"disk_alert_pct": 0.0})
    await health.check_disk()
    assert len(alerts.sent) == 1
    assert "disk" in alerts.sent[0]


async def test_the_cron_written_flag_file_is_an_alert_of_its_own(tmp_path: Path) -> None:
    """scripts/vps_bootstrap.sh writes this every 15 minutes when the volume fills up."""
    (tmp_path / "ALERT_DISK").write_text(
        "2026-08-30T03:00:00+00:00 disk 91% used on /srv/liqfade\n"
    )
    health, alerts = _health(tmp_path)
    health.cfg = CFG.model_copy(update={"disk_alert_pct": 100.0})
    await health.check_disk()
    assert alerts.sent == ["recorder: 2026-08-30T03:00:00+00:00 disk 91% used on /srv/liqfade"]


def test_disk_stats_works_before_the_data_dir_exists(tmp_path: Path) -> None:
    stats = disk_stats(tmp_path / "not" / "created" / "yet")
    assert stats["total_bytes"] > 0
    assert 0.0 <= stats["used_pct"] <= 100.0
