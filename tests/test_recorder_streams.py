"""Backoff bounds, gap accounting, and the stream's handling of a bad message."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest import mock

import pytest
import websockets

from liqfade.config import load_settings
from liqfade.config.models import Recorder
from liqfade.recorder.streams import bybit, hl
from liqfade.recorder.streams.base import (
    CONTINUOUS_STREAMS,
    PER_COIN_GAP_STREAMS,
    PER_COIN_STREAMS,
    ParsedRow,
    StreamStats,
    WSStream,
    backoff_delay,
    gap_threshold_s,
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
        kwargs.pop("url", "wss://example.invalid/ws"),
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
    assert stat.check_silence(5.0, NOW_MS + 4_000) == []
    assert stat.check_silence(5.0, NOW_MS + 6_000) == [""]
    # Still silent ten minutes later: the same outage, not sixty more gaps.
    assert stat.check_silence(5.0, NOW_MS + 600_000) == []
    assert stat.feed_state(NOW_MS + 600_000).gaps_1h == 1
    # A message ends the silence, so the next one counts again.
    stat.record_message(NOW_MS + 600_001)
    assert stat.check_silence(5.0, NOW_MS + 610_000) == [""]
    assert stat.feed_state(NOW_MS + 610_000).gaps_1h == 2


def test_a_sparse_stream_never_counts_a_gap() -> None:
    """Binance and OKX only publish when someone is liquidated. Quiet is not broken."""
    stat = StreamStats(name="binance.liquidations", continuous=False)
    stat.record_message(NOW_MS)
    assert stat.check_silence(5.0, NOW_MS + 3_600_000) == []
    assert stat.feed_state(NOW_MS + 3_600_000).gaps_1h == 0


def test_a_stream_that_has_never_delivered_is_not_a_gap() -> None:
    stat = StreamStats(name="hyperliquid.trades", continuous=True)
    assert stat.check_silence(5.0, NOW_MS) == []
    assert stat.silent_s(NOW_MS) == 0.0


def test_which_streams_are_continuous() -> None:
    assert "hyperliquid.trades" in CONTINUOUS_STREAMS
    # Complete since 2025-02-25, but event driven: a calm hour is silent, not broken.
    assert "bybit.liquidations" not in CONTINUOUS_STREAMS
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


# --- dropped rows ---------------------------------------------------------------


def test_a_dropped_row_is_counted_and_never_reaches_the_sink() -> None:
    """A parser that refuses a row (unknown side, unusable unit) reports it as None."""
    stream, rows = _stream(
        parse=lambda msg, rx: [("liquidations", None), ("liquidations", {"rx_ms": rx})],
        tables=("liquidations",),
    )
    stream.handle(b"{}", NOW_MS)
    assert len(rows) == 1
    (state,) = stream.feed_states(NOW_MS)
    assert state.parse_errors_1h == 1


def test_an_unreadable_message_counts_against_every_table_of_that_venue() -> None:
    """A venue field rename used to take a feed to zero rows with `connected=True` and
    nothing anywhere saying so. A message that fails to parse cannot be attributed to one
    table, so it counts against all of them; the alert names the stream either way."""
    stream, rows = _stream(parse=hl.make_parser(), tables=("trades", "bbo"), venue="hyperliquid")
    stream.handle(b"not json at all", NOW_MS)
    assert rows == []
    assert {s.name: s.parse_errors_1h() for s in stream.stats.values()} == {
        "hyperliquid.trades": 1,
        "hyperliquid.bbo": 1,
    }


# --- per coin gaps on the HL continuous streams --------------------------------


def test_one_dead_coin_subscription_is_a_gap_even_while_the_others_keep_flowing() -> None:
    """Stats keyed `venue.table` alone: if BTC's bbo died while ETH and SOL kept the
    stream alive, `lag_ms` stayed near zero and the Phase 1 DoD could not be evidenced."""
    stat = StreamStats(name="hyperliquid.bbo", continuous=True, per_coin=True, per_coin_gaps=True)
    stat.record_message(NOW_MS, ["BTC", "ETH"])
    stat.record_message(NOW_MS + 10_000, ["ETH"])  # BTC's subscription is gone

    assert stat.check_silence(5.0, NOW_MS + 10_000) == ["BTC"]
    assert stat.silent_s(NOW_MS + 10_000) == 10.0  # the worst coin, not the stream
    state = stat.feed_state(NOW_MS + 10_000)
    assert (state.worst_coin, state.worst_lag_ms) == ("BTC", 10_000)
    assert state.gaps_1h == 1
    # The same coin still silent is the same gap, and ETH is not one yet.
    assert stat.check_silence(5.0, NOW_MS + 12_000) == []


def test_a_coin_dropped_from_the_universe_stops_being_a_gap() -> None:
    """Otherwise an unsubscribed coin is silent forever and alerts forever."""
    stat = StreamStats(name="hyperliquid.bbo", continuous=True, per_coin=True, per_coin_gaps=True)
    stat.record_message(NOW_MS, ["BTC", "WIF"])
    stat.forget_coin("WIF")
    stat.record_message(NOW_MS + 10_000, ["BTC"])
    assert stat.check_silence(5.0, NOW_MS + 10_000) == []
    assert stat.feed_state(NOW_MS + 10_000).worst_coin == "BTC"


def test_a_stream_that_is_not_per_coin_still_counts_one_gap_for_the_whole_stream() -> None:
    stat = StreamStats(name="bybit.liquidations", continuous=True)
    stat.record_message(NOW_MS)
    assert stat.check_silence(5.0, NOW_MS + 6_000) == [""]
    assert stat.feed_state(NOW_MS + 6_000).worst_coin is None


def test_one_message_counts_once_however_many_rows_it_carried() -> None:
    """`hyperliquid.l2` emits twenty rows per message, so msgs_1m read twenty times the
    real message rate."""
    stream, rows = _stream(
        venue="hyperliquid",
        parse=lambda msg, rx: [("l2", {"coin": "BTC", "level": n, "rx_ms": rx}) for n in range(20)],
        tables=("l2",),
    )
    stream.handle(b"{}", NOW_MS)
    assert len(rows) == 20
    (state,) = stream.feed_states(NOW_MS)
    assert state.msgs_1m == 1


async def test_dropping_a_coin_from_the_universe_reaches_its_stream_stats() -> None:
    stream, _ = _stream(venue="hyperliquid", tables=("trades",), symbols=["BTC", "WIF"])
    stat = stream.stats["hyperliquid.trades"]
    stat.record_message(NOW_MS, ["BTC", "WIF"])
    await stream.set_symbols(["BTC"])
    stat.record_message(NOW_MS + 10_000, ["BTC"])
    assert stat.check_silence(5.0, NOW_MS + 10_000) == []  # WIF is unsubscribed, not silent


def test_a_thin_coin_going_a_minute_without_a_trade_is_not_a_gap() -> None:
    """Measured against the live feed on 2026-09-05: counting per-coin gaps on `trades`
    and `l2` produced 20 warnings in one quiet minute, nearly all of them normal market.
    Trades are events (a thin coin genuinely goes minutes without one) and l2 pushes
    about every 4s per coin, so only `bbo` ticks per coin continuously."""
    stat = StreamStats(name="hyperliquid.trades", continuous=True, per_coin=True)
    stat.record_message(NOW_MS, ["BTC"])
    stat.record_message(NOW_MS + 60_000, ["ETH"])

    assert stat.check_silence(5.0, NOW_MS + 60_000) == []
    assert stat.silent_s(NOW_MS + 60_000) == 0.0  # the stream is alive, so it is not silent
    # But which coin has been quiet longest is still reported, which is the evidence a
    # dead per-coin subscription leaves.
    assert stat.feed_state(NOW_MS + 60_000).worst_coin == "BTC"


def test_the_whole_trades_stream_going_quiet_is_still_a_gap() -> None:
    stat = StreamStats(name="hyperliquid.trades", continuous=True, per_coin=True)
    stat.record_message(NOW_MS, ["BTC"])
    assert stat.check_silence(5.0, NOW_MS + 6_000) == [""]
    assert stat.silent_s(NOW_MS + 6_000) == 6.0


def test_which_streams_count_gaps_per_coin() -> None:
    assert set(PER_COIN_GAP_STREAMS) == {"hyperliquid.trades"}
    assert PER_COIN_GAP_STREAMS < PER_COIN_STREAMS  # a strict subset, by construction


def test_a_quiet_bbo_coin_is_not_a_gap_while_the_stream_still_ticks() -> None:
    """`bbo` sends on a best-quote change, so a thin coin is quiet and the venue is fine.

    Measured on the box over the 18h to 2026-09-06: the recorded bbo stream's p99 between
    messages is 185 ms and it went over 5s five times, every one a reconnect. The detector
    logged 3,829 gaps in the same window, because CASHCAT and DOGE sit still for 5 to 10s
    at a time. Any coin's message proves the connection is alive, so that is the test.
    """
    stat = StreamStats(name="hyperliquid.bbo", continuous=True, per_coin=True)
    stat.record_message(NOW_MS, ["BTC", "CASHCAT"])
    for step in range(1, 20):  # BTC keeps quoting, CASHCAT does not
        stat.record_message(NOW_MS + step * 1_000, ["BTC"])

    assert stat.check_silence(5.0, NOW_MS + 19_000) == []
    assert stat.feed_state(NOW_MS + 19_000).gaps_1h == 0
    # The quiet coin is still reported, as information rather than as a gap.
    assert stat.feed_state(NOW_MS + 19_000).worst_coin == "CASHCAT"


# --- thresholds that match how each stream actually arrives ---------------------


def test_the_l2_gap_threshold_is_a_multiple_of_the_measured_cadence() -> None:
    """`l2Book` arrives as one snapshot per coin every 5.4s, all coins within a few ms.

    Measured on the box on 2026-09-05: BTC's interval p50 is 5,390 ms and p90 is 5,511 ms,
    identical on every coin. `recorder.gap_alert_s` of 5s therefore sits just under the
    feed's normal cadence, which is how the detector logged 1,192 l2 gaps in 24h.
    """
    stat = StreamStats(name="hyperliquid.l2", continuous=True, per_coin=True, cadence=True)
    for step in range(40):  # forty snapshots of two coins, 5.4s apart
        stat.record_message(NOW_MS + step * 5_400, ["BTC", "ETH"])

    assert stat.cadence_ms() == 5_400
    # 3 x 5.4s, so the natural cadence is nowhere near it and a real stall still is.
    assert gap_threshold_s(stat, CFG) == pytest.approx(16.2)


def test_an_l2_stream_with_no_cadence_yet_falls_back_to_the_configured_multiple() -> None:
    """The first seconds of a process have nothing measured. 3 x 5s, not 5s."""
    stat = StreamStats(name="hyperliquid.l2", continuous=True, per_coin=True, cadence=True)
    assert stat.cadence_ms() == 0
    assert gap_threshold_s(stat, CFG) == CFG.gap_alert_s * CFG.l2_gap_multiple


def test_the_l2_cadence_is_measured_per_coin_not_per_message() -> None:
    """Every coin's snapshot lands in the same millisecond, so the stream-level interval
    is 1 ms seventeen times out of eighteen and 5.4s once. The median of that is 1 ms,
    which would set the threshold to nothing. The cadence a coin sees is the real one."""
    stat = StreamStats(name="hyperliquid.l2", continuous=True, per_coin=True, cadence=True)
    coins = [f"C{n}" for n in range(18)]
    for step in range(20):
        for offset, coin in enumerate(coins):  # a batch spread over 17 ms
            stat.record_message(NOW_MS + step * 5_400 + offset, [coin])

    assert stat.cadence_ms() == 5_400


def test_a_thin_coins_natural_silence_does_not_trip_the_trades_gap() -> None:
    """Per-coin trade intervals over the 18-coin universe of 2026-09-05: DOGE p99 54.5s,
    XRP p99 44.1s, and the worst interval on any coin all day was 92.3s. At 60s the day
    holds 32 such intervals, at 120s none. `recorder.trades_gap_s` of 180s is twice the
    worst measured silence, so a quiet coin is quiet and a dead subscription is a gap."""
    stat = StreamStats(
        name="hyperliquid.trades", continuous=True, per_coin=True, per_coin_gaps=True
    )
    threshold = gap_threshold_s(stat, CFG)
    stat.record_message(NOW_MS, ["DOGE", "BTC"])
    stat.record_message(NOW_MS + 92_300, ["BTC"])  # DOGE's worst measured quiet spell

    assert stat.check_silence(threshold, NOW_MS + 92_300) == []
    # Three minutes without a print on a coin the universe still subscribes is a gap.
    assert stat.check_silence(threshold, NOW_MS + 181_000) == ["DOGE"]


def test_every_other_stream_keeps_the_configured_gap_threshold() -> None:
    for name in ("hyperliquid.bbo", "bybit.liquidations"):
        assert gap_threshold_s(StreamStats(name=name), CFG) == CFG.gap_alert_s


# --- delivery lag ---------------------------------------------------------------


def test_delivery_lag_is_measured_per_coin() -> None:
    """`lag_ms` on a feed is how long since the stream last said anything. It cannot see
    a coin that is delivering steadily but minutes behind, which is what DOGE did."""
    stat = StreamStats(name="hyperliquid.trades", continuous=True, per_coin=True)
    for step in range(40):
        at = NOW_MS + step * 1_000
        stat.record_message(at, lags_ms={"BTC": 200, "DOGE": 90_000})

    assert stat.delivery_lag_ms("BTC") == 200
    assert stat.delivery_lag_ms("DOGE") == 90_000
    assert stat.worst_delivery() == ("DOGE", 90_000)
    state = stat.feed_state(NOW_MS + 40_000)
    assert (state.delivery_coin, state.delivery_lag_ms) == ("DOGE", 90_000)


def test_one_late_row_does_not_become_the_coins_lag() -> None:
    """A subscribe snapshot replays trades minutes old, so a single sample is noise. The
    number that alerts has to mean the coin is behind, not that one row was."""
    stat = StreamStats(name="hyperliquid.trades", continuous=True, per_coin=True)
    for _ in range(40):
        stat.record_message(NOW_MS, lags_ms={"DOGE": 200})
    stat.record_message(NOW_MS, lags_ms={"DOGE": 356_085})  # the 2026-09-05 burst's worst

    assert stat.delivery_lag_ms("DOGE") == 200


def test_a_coin_with_no_delivery_lag_measured_reports_nothing() -> None:
    stat = StreamStats(name="hyperliquid.trades", continuous=True, per_coin=True)
    stat.record_message(NOW_MS, ["BTC"])
    assert stat.delivery_lag_ms("BTC") == 0
    assert stat.worst_delivery() == (None, 0)
    assert stat.feed_state(NOW_MS).delivery_coin is None


def test_a_coin_dropped_from_the_universe_stops_carrying_a_delivery_lag() -> None:
    stat = StreamStats(name="hyperliquid.trades", continuous=True, per_coin=True)
    for _ in range(40):
        stat.record_message(NOW_MS, lags_ms={"BTC": 200, "WIF": 90_000})
    stat.forget_coin("WIF")
    assert stat.worst_delivery() == ("BTC", 200)


def test_the_stream_measures_the_lag_of_the_rows_it_parsed() -> None:
    """End to end from the wire: `handle` is what knows both timestamps."""
    stream, _ = _stream(
        venue="hyperliquid",
        parse=lambda msg, rx: [("trades", {"coin": "DOGE", "ts_ms": rx - 90_000, "rx_ms": rx})],
        tables=("trades",),
    )
    for step in range(40):
        stream.handle(b"{}", NOW_MS + step * 1_000)

    assert stream.stats["hyperliquid.trades"].delivery_lag_ms("DOGE") == 90_000


# --- rotating the connection before the venue expires it -------------------------


class ExpiringServer:
    """A venue that broadcasts a numbered stream and expires every socket, as HL does.

    One counter shared by every live connection, so a subscriber that misses a number
    missed a message the venue sent, and a subscriber that sees one twice wrote a row
    twice. That is the whole assertion of the tests below.
    """

    def __init__(self, expire_after_s: float, tick_s: float = 0.005) -> None:
        self.expire_after_s = expire_after_s
        self.tick_s = tick_s
        self.seq = 0
        self.connections = 0
        self._live: set[Any] = set()
        self._server: Any = None
        self._broadcast: asyncio.Task[None] | None = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"

    async def _handler(self, ws: Any) -> None:
        self.connections += 1
        self._live.add(ws)
        try:
            await asyncio.sleep(self.expire_after_s)
            await ws.close(1000, "Expired")
        finally:
            self._live.discard(ws)

    async def _pump(self) -> None:
        while True:
            await asyncio.sleep(self.tick_s)
            self.seq += 1
            payload = str(self.seq)
            for ws in list(self._live):
                with contextlib.suppress(Exception):
                    await ws.send(payload)

    async def __aenter__(self) -> ExpiringServer:
        self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
        self._broadcast = asyncio.create_task(self._pump())
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._broadcast is not None
        self._broadcast.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._broadcast
        self._server.close()
        await self._server.wait_closed()


def _seq_parse(raw: bytes, rx_ms: int) -> list[ParsedRow]:
    return [("trades", {"coin": "BTC", "ts_ms": rx_ms, "rx_ms": rx_ms, "seq": int(raw)})]


async def _run_for(stream: WSStream, seconds: float) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    await asyncio.sleep(seconds)
    stop.set()
    await asyncio.wait_for(task, timeout=5.0)


async def test_the_replacement_is_up_before_the_venue_expires_the_old_one() -> None:
    """Hyperliquid closes every socket with `1000 (OK) Expired` after 167 to 181 minutes,
    measured seven times on the box between 2026-09-05 and 2026-09-06, and each close cost
    an 18s hole in bbo and trades. The replacement connects, subscribes and starts
    delivering before the old one goes, so the changeover misses nothing."""
    async with ExpiringServer(expire_after_s=0.5) as venue:
        stream, rows = _stream(
            venue="hyperliquid",
            url=venue.url,
            parse=_seq_parse,
            tables=("trades",),
            rotate_after_s=0.15,
            rotate_overlap_s=0.05,
        )
        await _run_for(stream, 0.9)

    seqs = [row[2]["seq"] for row in rows]
    assert len(seqs) > 20, "the fake venue should have sent plenty"
    assert seqs == sorted(seqs)
    # Contiguous: no number missing (a hole) and none seen twice (a duplicate).
    assert seqs == list(range(seqs[0], seqs[-1] + 1))
    assert venue.connections > 1, "the stream should have rotated at least once"
    # A rotation is not a reconnect: nothing went through the backoff ladder.
    assert stream.stats["hyperliquid.trades"].feed_state(NOW_MS).reconnects_1h == 0


async def test_a_rotation_that_cannot_connect_leaves_the_old_socket_working() -> None:
    """The failure has to cost the hole the rotation was meant to avoid, never the feed."""
    async with ExpiringServer(expire_after_s=5.0) as venue:
        stream, rows = _stream(
            venue="hyperliquid",
            url=venue.url,
            parse=_seq_parse,
            tables=("trades",),
            rotate_after_s=0.15,
            rotate_overlap_s=0.05,
        )
        real_url, real_connect = venue.url, websockets.connect

        def _connect_once_then_fail(url: str, **kwargs: Any) -> Any:
            # Not `async def`: `run` uses the result as an async context manager and
            # `_rotate` awaits it, which is what websockets.connect itself returns.
            if venue.connections >= 1:
                raise OSError("refused")
            return real_connect(real_url, **kwargs)

        with mock.patch.object(websockets, "connect", _connect_once_then_fail):
            await _run_for(stream, 0.5)

    seqs = [row[2]["seq"] for row in rows]
    assert seqs == list(range(seqs[0], seqs[-1] + 1))
    assert seqs[-1] - seqs[0] > 20, "the original socket kept delivering throughout"


async def test_a_stream_with_no_rotation_configured_is_unchanged() -> None:
    """Only Hyperliquid expires sockets on a clock. Everything else reconnects on close."""
    async with ExpiringServer(expire_after_s=0.2) as venue:
        stream, rows = _stream(venue="bybit", url=venue.url, parse=_seq_parse, tables=("trades",))
        await _run_for(stream, 0.6)

    assert rows, "it should still record"
    assert stream.stats["bybit.trades"].feed_state(NOW_MS).reconnects_1h >= 1


async def test_closing_a_retired_socket_does_not_take_the_stream_down() -> None:
    """Measured on the box: Hyperliquid does not answer our close frame, so closing the
    socket we retired raises `ConnectionClosedError: sent 1000 (OK); no close frame
    received` out of its receive loop. That used to leave the task group, which took the
    replacement's tasks with it and made `run` reconnect from scratch, so every rotation
    still cost the hole and the resubscribe snapshot it existed to avoid."""
    stream, _ = _stream(venue="hyperliquid", tables=("trades",))
    stream._ws = object()  # the replacement is the live socket now

    retired = _ClosingWS(websockets.exceptions.ConnectionClosedError(None, None))
    await stream._recv_loop(retired)  # returns rather than raising


async def test_the_live_socket_dropping_is_still_an_error() -> None:
    """The same exception on the connection actually in use has to reach `run`, which is
    what reconnects. Distinguishing them is the whole point."""
    stream, _ = _stream(venue="hyperliquid", tables=("trades",))
    live = _ClosingWS(websockets.exceptions.ConnectionClosedError(None, None))
    stream._ws = live

    with pytest.raises(websockets.exceptions.ConnectionClosedError):
        await stream._recv_loop(live)


class _ClosingWS:
    """A socket that hands over one message and then reports the connection closed."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def __aiter__(self) -> _ClosingWS:
        return self

    async def __anext__(self) -> str:
        raise self.error
