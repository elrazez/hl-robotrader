"""Backoff bounds, gap accounting, and the stream's handling of a bad message."""

from __future__ import annotations

from typing import Any

import pytest

from liqfade.config import load_settings
from liqfade.config.models import Recorder
from liqfade.recorder.streams import bybit
from liqfade.recorder.streams.base import (
    CONTINUOUS_STREAMS,
    StreamStats,
    WSStream,
    backoff_delay,
)

CFG: Recorder = load_settings(
    "paper", environ={}
).recorder  # environ={}: these thresholds are the test's, not the box's .env or overrides.yaml
NOW_MS = 1788099600000
BYBIT_LIQ = (
    b'{"topic":"allLiquidation.BTCUSDT","type":"snapshot","ts":1788099227482,'
    b'"data":[{"T":1788099227003,"s":"BTCUSDT","S":"Sell","v":"0.003","p":"79149.90"}]}'
)


class FakeWS:
    """Just enough websocket to see what the stream sent."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)


def _stream(**kwargs: Any) -> tuple[WSStream, list[tuple[str, str, dict[str, Any]]]]:
    rows: list[tuple[str, str, dict[str, Any]]] = []
    stream = WSStream(
        kwargs.pop("venue", "bybit"),
        "wss://example.invalid/ws",
        parse=kwargs.pop("parse", lambda msg, rx: [("liquidations", {"rx_ms": rx})]),
        sink=lambda table, venue, row: rows.append((table, venue, row)),
        cfg=CFG,
        **kwargs,
    )
    return stream, rows


# --- backoff -------------------------------------------------------------------


def test_backoff_stays_inside_the_configured_band() -> None:
    ceiling = CFG.reconnect_backoff_max_s * (1.0 + CFG.reconnect_jitter_frac)
    for attempt in range(1000):
        delay = backoff_delay(attempt % 20, CFG)
        assert CFG.reconnect_backoff_min_s <= delay <= ceiling


def test_backoff_grows_and_then_caps() -> None:
    first = backoff_delay(0, CFG)
    assert first < backoff_delay(6, CFG)
    # Past the ladder every attempt sits at the cap plus jitter.
    assert backoff_delay(40, CFG) >= CFG.reconnect_backoff_max_s


def test_zero_jitter_is_exact() -> None:
    cfg = CFG.model_copy(update={"reconnect_jitter_frac": 0.0})
    assert backoff_delay(0, cfg) == cfg.reconnect_backoff_min_s
    assert backoff_delay(1, cfg) == cfg.reconnect_backoff_min_s * 2


# --- gap accounting -------------------------------------------------------------


def test_a_continuous_stream_counts_one_gap_per_silence() -> None:
    stat = StreamStats(name="hyperliquid.trades", continuous=True)
    stat.record_message(NOW_MS)
    assert stat.check_silence(5.0, NOW_MS + 4_000) is False
    assert stat.check_silence(5.0, NOW_MS + 6_000) is True
    # Still silent ten minutes later: the same outage, not sixty more gaps.
    assert stat.check_silence(5.0, NOW_MS + 600_000) is False
    assert stat.gaps_1h == 1
    # A message ends the silence, so the next one counts again.
    stat.record_message(NOW_MS + 600_001)
    assert stat.check_silence(5.0, NOW_MS + 610_000) is True
    assert stat.gaps_1h == 2


def test_a_sparse_stream_never_counts_a_gap() -> None:
    """Binance and OKX only publish when someone is liquidated. Quiet is not broken."""
    stat = StreamStats(name="binance.liquidations", continuous=False)
    stat.record_message(NOW_MS)
    assert stat.check_silence(5.0, NOW_MS + 3_600_000) is False
    assert stat.gaps_1h == 0


def test_a_stream_that_has_never_delivered_is_not_a_gap() -> None:
    stat = StreamStats(name="hyperliquid.trades", continuous=True)
    assert stat.check_silence(5.0, NOW_MS) is False
    assert stat.silent_s(NOW_MS) == 0.0


def test_which_streams_are_continuous() -> None:
    assert "hyperliquid.trades" in CONTINUOUS_STREAMS
    assert "bybit.liquidations" in CONTINUOUS_STREAMS  # complete since 2025-02-25
    assert "binance.liquidations" not in CONTINUOUS_STREAMS
    assert "okx.liquidations" not in CONTINUOUS_STREAMS


def test_feed_state_reports_lag_and_message_rate() -> None:
    stat = StreamStats(name="hyperliquid.bbo", continuous=True)
    for _ in range(3):
        stat.record_message(NOW_MS)
    state = stat.feed_state(NOW_MS + 250)
    assert state.lag_ms == 250
    assert state.msgs_1m == 3
    assert state.last_msg_ms == NOW_MS


def test_reconnects_are_counted_for_the_last_hour() -> None:
    stat = StreamStats(name="okx.liquidations")
    stat.record_reconnect()
    assert stat.feed_state(NOW_MS).reconnects_1h == 1


# --- message handling ----------------------------------------------------------


def test_declared_tables_show_up_before_any_message_arrives() -> None:
    stream, _ = _stream(tables=("liquidations",))
    (state,) = stream.feed_states(NOW_MS)
    assert (state.name, state.connected, state.lag_ms) == ("bybit.liquidations", False, 0)


def test_a_bad_message_is_dropped_without_killing_the_stream() -> None:
    stream, rows = _stream(parse=bybit.make_parser(True), tables=("liquidations",))
    stream.handle(b"not json at all", NOW_MS)  # must not raise
    assert rows == []
    stream.handle(BYBIT_LIQ, NOW_MS)
    assert len(rows) == 1


def test_a_parser_that_raises_does_not_reach_the_sink() -> None:
    def explode(_msg: bytes, _rx: int) -> list[tuple[str, dict[str, Any]]]:
        raise ValueError("venue changed a field")

    stream, rows = _stream(parse=explode, tables=("liquidations",))
    stream.handle(b"{}", NOW_MS)
    assert rows == []


async def test_a_universe_change_sends_only_the_difference() -> None:
    stream, _ = _stream(
        symbols=["BTCUSDT"],
        on_universe_change=lambda added, removed: [{"add": list(added), "drop": list(removed)}],
    )
    fake = FakeWS()
    stream._ws = fake
    await stream.set_symbols(["BTCUSDT", "ETHUSDT"])
    assert fake.sent == ['{"add":["ETHUSDT"],"drop":[]}']
    # The same set again is not worth a frame.
    await stream.set_symbols(["BTCUSDT", "ETHUSDT"])
    assert len(fake.sent) == 1


async def test_symbols_are_remembered_while_disconnected() -> None:
    stream, _ = _stream(on_universe_change=lambda added, removed: [{"x": 1}])
    await stream.set_symbols(["BTCUSDT"])
    assert stream.symbols == ["BTCUSDT"]


def test_ws_ping_round_trip_is_measured_only_while_a_ping_is_outstanding() -> None:
    samples: list[float] = []
    stream, _ = _stream(pong_marker=b'"pong"', on_rtt_ms=samples.append)
    stream.handle(b'{"channel":"pong"}', NOW_MS)
    assert samples == []  # no ping was sent, so there is nothing to measure
    stream._ping_sent = 0.0
    stream.handle(b'{"channel":"pong"}', NOW_MS)
    assert len(samples) == 1


def test_frames_are_sent_as_text() -> None:
    """HL and OKX drop binary frames silently, which reads as a mystery disconnect."""
    assert WSStream._encode({"method": "ping"}) == '{"method":"ping"}'
    assert WSStream._encode("ping") == "ping"


@pytest.mark.parametrize("payload", [b"", b"[]", b"null"])
def test_parsers_do_not_have_to_cope_with_junk_because_handle_does(payload: bytes) -> None:
    stream, rows = _stream(parse=lambda msg, rx: [("liquidations", {"n": len(msg)})])
    stream.handle(payload, NOW_MS)
    assert len(rows) == 1
