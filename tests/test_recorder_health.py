"""Silence, disconnect and disk alerts, and the gap counting behind them."""

from __future__ import annotations

from pathlib import Path

from liqfade.config import load_settings
from liqfade.control.protocol import now_ms
from liqfade.recorder.health import Health, disk_stats
from liqfade.recorder.streams.base import StreamStats

CFG = load_settings(
    "paper", environ={}
).recorder  # environ={}: these thresholds are the test's, not the box's .env or overrides.yaml


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
    assert stat.feed_state(at).gaps_1h == 1
    assert alerts.sent == [
        f"recorder: hyperliquid.trades silent for over {CFG.silence_alert_s:.0f}s"
    ]


async def test_the_silence_alert_reads_the_same_all_through_one_outage(tmp_path: Path) -> None:
    """The text used to carry `silent for {n}s`, and telegram._deduped keys on the exact
    string, so a ten minute outage sent about 120 messages and got rate limited."""
    stat = StreamStats(name="hyperliquid.trades", continuous=True, connected=True)
    at = now_ms()
    stat.record_message(at - 120_000)
    health, alerts = _health(tmp_path, stat)
    await health.check_streams(at)
    await health.check_streams(at + 5_000)
    await health.check_streams(at + 600_000)
    assert len(set(alerts.sent)) == 1


async def test_the_silence_alert_names_the_coin_on_a_per_coin_stream(tmp_path: Path) -> None:
    """One dead subscription out of twenty: the alert has to say which."""
    stat = StreamStats(
        name="hyperliquid.bbo", continuous=True, connected=True, per_coin=True, per_coin_gaps=True
    )
    at = now_ms()
    stat.record_message(at - 120_000, ["BTC"])
    stat.record_message(at, ["ETH"])
    health, alerts = _health(tmp_path, stat)
    await health.check_streams(at)
    assert alerts.sent == [
        f"recorder: hyperliquid.bbo silent for over {CFG.silence_alert_s:.0f}s (worst coin BTC)"
    ]


async def test_a_sparse_stream_being_quiet_is_not_an_alert(tmp_path: Path) -> None:
    stat = StreamStats(name="binance.liquidations", continuous=False, connected=True)
    at = now_ms()
    stat.record_message(at - 3_600_000)
    health, alerts = _health(tmp_path, stat)
    await health.check_streams(at)
    assert alerts.sent == []
    assert stat.feed_state(at).gaps_1h == 0


async def test_a_sparse_stream_with_a_dead_socket_is_an_alert(tmp_path: Path) -> None:
    """For a feed that is quiet by nature, the connection is the only signal."""
    stat = StreamStats(name="binance.liquidations", continuous=False, connected=False)
    at = now_ms()
    stat.record_message(at - 1000)
    health, alerts = _health(tmp_path, stat)
    await health.check_streams(at)
    assert alerts.sent == ["recorder: binance.liquidations disconnected"]


async def test_a_declared_stream_whose_socket_never_came_up_is_an_alert(
    tmp_path: Path,
) -> None:
    """`and stat.last_msg_ms` used to gate this, so a stream that never connected at all
    was the one case nothing reported."""
    stat = StreamStats(name="okx.liquidations", continuous=False, connected=False)
    health, alerts = _health(tmp_path, stat)
    await health.check_streams(now_ms())
    assert alerts.sent == ["recorder: okx.liquidations disconnected"]


async def test_a_stream_that_connects_and_never_delivers_is_quiet_during_the_grace(
    tmp_path: Path,
) -> None:
    """Binance can go a genuinely quiet hour, so the grace has to be generous."""
    stat = StreamStats(name="binance.liquidations", continuous=False, connected=True)
    health, alerts = _health(tmp_path, stat)
    await health.check_streams(now_ms())
    assert alerts.sent == []


async def test_a_stream_that_connects_and_never_delivers_alerts_once_after_the_grace(
    tmp_path: Path,
) -> None:
    """The Binance regional block docs/DATA_SOURCES.md warns about: socket accepted,
    connection held open, and not one message, ever. Nothing reported it."""
    stat = StreamStats(name="binance.liquidations", continuous=False, connected=True)
    health, alerts = _health(tmp_path, stat)
    health.started -= health.startup_grace_s + 1.0
    await health.check_streams(now_ms())
    assert alerts.sent == [
        "recorder: binance.liquidations is connected but has never delivered a message, "
        f"{health.startup_grace_s / 3600:.0f}h after start"
    ]
    # Once. A feed that will never deliver must not page the owner every check.
    await health.check_streams(now_ms())
    assert len(alerts.sent) == 1


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


async def test_a_stream_whose_parse_errors_cross_the_rate_alerts(tmp_path: Path) -> None:
    """A venue renaming a field is silent otherwise: connected, and zero rows."""
    stat = StreamStats(name="bybit.liquidations", continuous=True, connected=True)
    at = now_ms()
    stat.record_message(at)
    for _ in range(CFG.parse_error_alert_1h):
        stat.record_parse_error()
    health, alerts = _health(tmp_path, stat)
    await health.check_streams(at)
    assert alerts.sent == [
        f"recorder: bybit.liquidations parse errors over {CFG.parse_error_alert_1h}/h, "
        "the wire has stopped matching the parser"
    ]


async def test_parse_errors_under_the_rate_are_not_worth_a_message(tmp_path: Path) -> None:
    stat = StreamStats(name="bybit.liquidations", continuous=True, connected=True)
    at = now_ms()
    stat.record_message(at)
    stat.record_parse_error()
    health, alerts = _health(tmp_path, stat)
    await health.check_streams(at)
    assert alerts.sent == []


async def test_the_disk_alert_reads_the_same_as_the_volume_keeps_filling(tmp_path: Path) -> None:
    """Free bytes move every check, and a text that moves defeats the dedupe."""
    health, alerts = _health(tmp_path)
    health.cfg = CFG.model_copy(update={"disk_alert_pct": 0.0})
    await health.check_disk()
    await health.check_disk()
    assert len(set(alerts.sent)) == 1
