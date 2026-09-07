"""One reusable WebSocket stream: connect, backoff, ping, gap and lag accounting.

Every venue gets one instance of WSStream. The venue module supplies three pure pieces
(a parser and two subscribe-frame builders) and nothing else, so the reconnect and health
logic exists once and the per venue code stays unit testable without a socket.

Naming: a *connection* is per venue, a *stream* is a venue plus a table, spelled
`hyperliquid.trades`. Gaps, lag and message rates are counted per stream because that is
what tells you which subscription died, and it is what FeedState carries to the panel.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from statistics import median
from typing import Any

import orjson
import websockets

from liqfade.config.models import Recorder
from liqfade.control.protocol import FeedState
from liqfade.log import get_logger

log = get_logger(__name__)

# (table, row), or (table, None) for a row the parser refused.
#
# A parser drops a row rather than guessing when a value it must map is not one of the
# values the venue documents: an unknown side token used to become the opposite side,
# because every mapping was a two-branch default. A flipped sign inverts the strategy and
# would survive into a backtest looking like signal, so the row is dropped and the drop is
# counted against the stream, where the parse-error alert picks it up.
ParsedRow = tuple[str, dict[str, Any] | None]
Parser = Callable[[bytes, int], list[ParsedRow]]
# table, venue, row
RowSink = Callable[[str, str, dict[str, Any]], None]
FramesFn = Callable[[Sequence[str]], list[Any]]
DiffFramesFn = Callable[[Sequence[str], Sequence[str]], list[Any]]

# Streams that must never be quiet, so silence is a data gap. Everything else is event
# driven (a liquidation feed on any venue, a 1m candle) and silence means nothing;
# for those, only the connection being up is monitored. Bybit publishes every
# liquidation since 2025-02-25, but complete is not the same as constant: on the first
# live day it went over 60s without one 24 times in four hours, each a Telegram alert.
CONTINUOUS_STREAMS = frozenset(
    {
        "hyperliquid.trades",
        "hyperliquid.bbo",
        "hyperliquid.l2",
    }
)

# Streams that track a last-message time per coin. Hyperliquid subscribes each coin
# separately, so one coin's subscription can die while the others keep the stream's
# last-message time fresh: `lag_ms` stays near zero and nothing in the metrics says which
# subscription went. These carry `worst_coin` and `worst_lag_ms` to the panel, which is
# the evidence that failure leaves. Bybit is not here: one topic per symbol, but the venue
# is a single liquidation feed and its silence is meaningful at the stream level.
PER_COIN_STREAMS = frozenset(
    {
        "hyperliquid.trades",
        "hyperliquid.bbo",
        "hyperliquid.l2",
    }
)

# Of those, the ones whose *silence* is per coin too, so a gap is counted and the outage
# alert fires per coin. Only `trades`, and only against `recorder.trades_gap_s`.
#
# The three streams arrive in three different shapes, measured on the box over the first
# 24h (docs/DECISIONS.md, 2026-09-06), and one threshold cannot fit them:
#
# - `bbo` sends on a best-quote change. Any coin's message proves the connection is
#   alive, so a gap is stream-wide silence. Counting it per coin logged 3,829 gaps in
#   18h from thin coins sitting still for 5 to 10s, against a stream whose own p99
#   between messages is 185 ms and which went over 5s five times, all reconnects.
# - `l2` pushes one snapshot per coin every 5.4s, every coin within a few ms of the rest,
#   so the stream is never quiet for long and no coin is ever quiet for less than its
#   cadence. Its threshold is `recorder.l2_gap_multiple` times the cadence it is actually
#   delivering at (see `cadence_ms`), which is the only number that moves with the feed.
# - `trades` are events, so the stream as a whole is never quiet while any coin prints,
#   and one dead subscription hides behind the other seventeen. That is the one stream
#   where the per-coin question is the right one; it just needs a threshold above a thin
#   coin's natural silence, which `recorder.trades_gap_s` is.
PER_COIN_GAP_STREAMS = frozenset({"hyperliquid.trades"})

# Streams whose delivery lag is worth a Telegram alert, which needs an exchange timestamp
# that means something. `asset_ctx` sets `ts_ms` to `rx_ms` because the payload carries no
# exchange time, so its lag is zero by construction; `l2` is a snapshot whose age is its
# own cadence rather than a delay. Both still report the figure, which is the evidence.
LAG_ALERT_STREAMS = frozenset({"hyperliquid.trades", "hyperliquid.bbo"})

# Streams whose gap threshold is measured rather than configured in seconds, because the
# venue's own cadence is the only thing it can sensibly be compared against.
CADENCE_STREAMS = frozenset({"hyperliquid.l2"})

HOUR_S = 3600.0
STABLE_CONNECTION_S = 60.0  # a connection alive this long resets the backoff ladder
MAX_LOGGED_PAYLOAD = 500  # bytes of a bad message kept in the log line
# Intervals behind the measured cadence. Enough that a stall or two cannot move the
# median, short enough that the number follows the venue rather than the last hour.
CADENCE_SAMPLES = 64
# Delivery lag samples behind each coin's reported figure. Same reason as the cadence: a
# subscribe snapshot replays trades minutes old, and one such row must not read as a coin
# falling behind. The median of 32 says the coin is behind, not that a row was.
LAG_SAMPLES = 32
# Seconds the replacement socket and the one it replaces both deliver. Long enough for
# the venue to have taken every subscribe frame and started pushing on the new one, short
# enough that the payloads held for the overlap dedupe stay small.
ROTATE_OVERLAP_S = 5.0
# How often the tasks that belong to one socket check whether it has closed. They exist to
# close it on shutdown and to ping it; both have to end when the socket does, or a rotated
# out connection leaves tasks behind and `_pump` never returns to reconnect.
SOCKET_POLL_S = 0.25
# How long a retired socket's receive loop is given to hand over the frames it had already
# been sent, before the changeover's dedupe is turned off.
RETIRE_DRAIN_S = 5.0


def backoff_delay(attempt: int, cfg: Recorder) -> float:
    """Seconds to wait before reconnect attempt `attempt` (0 based).

    Exponential from reconnect_backoff_min_s, capped at reconnect_backoff_max_s, plus
    uniform jitter of up to reconnect_jitter_frac of the delay. Jitter is added rather
    than centred so the result is never below the configured minimum: every venue
    reconnecting on the same schedule after a network blip is the thing jitter exists to
    prevent, and undershooting the minimum only risks a rate limit.
    """
    base = min(cfg.reconnect_backoff_min_s * (2.0**attempt), cfg.reconnect_backoff_max_s)
    return base * (1.0 + random.random() * cfg.reconnect_jitter_frac)  # noqa: S311 - jitter


@dataclass
class StreamStats:
    """Per stream health, shaped for control.FeedState."""

    name: str
    continuous: bool = False
    connected: bool = False
    per_coin: bool = False  # track a last-message time per coin, and report the worst
    per_coin_gaps: bool = False  # and count a gap, and alert, per coin
    cadence: bool = False  # measure how often this stream delivers, and gap off that
    last_msg_ms: int = 0
    _buckets: deque[list[float]] = field(default_factory=lambda: deque(maxlen=120))
    _gap_times: deque[float] = field(default_factory=deque)
    _reconnects: deque[float] = field(default_factory=deque)
    _parse_errors: deque[float] = field(default_factory=deque)
    _gap_open: bool = False
    # coin -> ms of its last message, and the coins whose gap is already counted. Only
    # populated for PER_COIN_STREAMS; every other stream reads its silence off last_msg_ms.
    _coin_msg_ms: dict[str, int] = field(default_factory=dict)
    _coin_gap_open: set[str] = field(default_factory=set)
    _intervals: deque[int] = field(default_factory=lambda: deque(maxlen=CADENCE_SAMPLES))
    # coin -> its recent rx_ms minus ts_ms. Populated only for PER_COIN_STREAMS, and only
    # from rows that carry both timestamps.
    _coin_lags: dict[str, deque[int]] = field(default_factory=dict)

    def record_message(
        self,
        rx_ms: int,
        coins: Iterable[str] = (),
        *,
        lags_ms: Mapping[str, int] | None = None,
        now: float | None = None,
    ) -> None:
        """One received message for this stream, and the coins it carried rows for.

        Counted once however many rows the message held: it used to be called per row, so
        `msgs_1m` for `hyperliquid.l2` read twenty times the real message rate.

        `lags_ms` is each coin's `rx_ms - ts_ms` on this message, where the row carried an
        exchange timestamp to measure against. Passing it also names the coins, so a
        caller that has it does not pass `coins` as well.
        """
        if lags_ms is not None and not coins:
            coins = lags_ms
        now = time.monotonic() if now is None else now
        if self.cadence and not self.per_coin and self.last_msg_ms:
            self._intervals.append(rx_ms - self.last_msg_ms)
        self.last_msg_ms = rx_ms
        self._gap_open = False
        if self.per_coin:
            for coin in coins:
                if coin:
                    # Per coin, because every coin's l2 snapshot lands in the same
                    # millisecond as the rest: the stream-level interval is 1 ms
                    # seventeen times out of eighteen and 5.4s once, whose median is
                    # 1 ms. The interval a single coin sees is the cadence.
                    if self.cadence and (last := self._coin_msg_ms.get(coin)):
                        self._intervals.append(rx_ms - last)
                    self._coin_msg_ms[coin] = rx_ms
                    self._coin_gap_open.discard(coin)
                    if lags_ms is not None and (lag := lags_ms.get(coin)) is not None:
                        self._coin_lags.setdefault(coin, deque(maxlen=LAG_SAMPLES)).append(lag)
        second = float(int(now))
        if self._buckets and self._buckets[-1][0] == second:
            self._buckets[-1][1] += 1
        else:
            self._buckets.append([second, 1.0])

    def forget_coin(self, coin: str) -> None:
        """Stop measuring a coin, because the universe no longer subscribes it.

        Without this an unsubscribed coin is silent forever and gaps forever.
        """
        self._coin_msg_ms.pop(coin, None)
        self._coin_gap_open.discard(coin)
        self._coin_lags.pop(coin, None)

    def record_reconnect(self, now: float | None = None) -> None:
        self._reconnects.append(time.monotonic() if now is None else now)

    def record_parse_error(self, now: float | None = None) -> None:
        """One row the parser refused, or one message it could not read at all.

        Both mean the same thing operationally: the wire stopped matching the parser, and
        rows that should be in the lake are not. Counted over the trailing hour so a rate
        can be alerted on rather than a lifetime total nobody can interpret.
        """
        self._parse_errors.append(time.monotonic() if now is None else now)

    def check_silence(self, gap_alert_s: float, now_ms: int, now: float | None = None) -> list[str]:
        """Count the gaps this stream has just opened. Idempotent.

        One silence counts once, however long it lasts, so a subscription that is down for
        an hour reads as one gap rather than 720. Returns the labels that newly gapped: a
        coin name for a per-coin stream, and `""` for the stream as a whole otherwise.
        """
        if not self.continuous:
            return []
        if self.per_coin_gaps:
            opened = []
            for coin, last in sorted(self._coin_msg_ms.items()):
                if coin in self._coin_gap_open or (now_ms - last) / 1000.0 <= gap_alert_s:
                    continue
                self._coin_gap_open.add(coin)
                self._count_gap(now)
                opened.append(coin)
            return opened
        if self.last_msg_ms == 0 or self._gap_open:
            return []
        if (now_ms - self.last_msg_ms) / 1000.0 <= gap_alert_s:
            return []
        self._gap_open = True
        self._count_gap(now)
        return [""]

    def _count_gap(self, now: float | None) -> None:
        self._gap_times.append(time.monotonic() if now is None else now)

    def worst_coin(self, now_ms: int) -> tuple[str | None, float]:
        """The coin that has been quiet longest, and for how many seconds."""
        if not self._coin_msg_ms:
            return None, 0.0
        coin, last = min(self._coin_msg_ms.items(), key=lambda pair: (pair[1], pair[0]))
        return coin, max(0.0, (now_ms - last) / 1000.0)

    def silent_s(self, now_ms: int, coin: str | None = None) -> float:
        """Seconds since the last message. 0 when nothing has ever been delivered.

        For a stream whose gaps are counted per coin, and with no coin named, this is the
        *worst* coin's silence rather than the stream's: one dead subscription out of
        twenty is what the number has to show, because the stream as a whole is never
        quiet while any coin still ticks.
        """
        if coin is not None:
            last = self._coin_msg_ms.get(coin, 0)
            return 0.0 if last == 0 else (now_ms - last) / 1000.0
        if self.per_coin_gaps and self._coin_msg_ms:
            return self.worst_coin(now_ms)[1]
        return 0.0 if self.last_msg_ms == 0 else (now_ms - self.last_msg_ms) / 1000.0

    def delivery_lag_ms(self, coin: str) -> int:
        """How far behind this coin's rows are arriving. 0 until measured.

        The median of its last `LAG_SAMPLES` `rx_ms - ts_ms`, which is a different
        question from `lag_ms` on the feed: that is how long since the stream last said
        anything, and it cannot see a coin delivering steadily but minutes late. DOGE did
        exactly that on 2026-09-05 while every other coin on the same connection was
        under a second.
        """
        samples = self._coin_lags.get(coin)
        if not samples or len(samples) < LAG_SAMPLES // 2:
            return 0
        return int(median(samples))

    def worst_delivery(self) -> tuple[str | None, int]:
        """The coin arriving furthest behind, and by how many ms."""
        lags = {coin: self.delivery_lag_ms(coin) for coin in self._coin_lags}
        if not any(lags.values()):
            return None, 0
        coin = max(lags, key=lambda name: (lags[name], name))
        return coin, lags[coin]

    def cadence_ms(self) -> int:
        """Milliseconds this stream typically leaves between messages. 0 until measured.

        The median of the last `CADENCE_SAMPLES` intervals, so one stall does not move it
        and a venue that changes its push rate is followed within a minute or so. Only
        measured for `CADENCE_STREAMS`; every other stream returns 0 and is judged
        against `recorder.gap_alert_s`.
        """
        if len(self._intervals) < CADENCE_SAMPLES // 2:
            return 0
        return int(median(self._intervals))

    def parse_errors_1h(self, now: float | None = None) -> int:
        """Refused rows plus unreadable messages in the trailing hour."""
        now = time.monotonic() if now is None else now
        self._prune(now)
        return len(self._parse_errors)

    def msgs_1m(self, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        return int(sum(count for second, count in self._buckets if now - second < 60.0))

    def _prune(self, now: float) -> None:
        for series in (self._gap_times, self._reconnects, self._parse_errors):
            while series and now - series[0] > HOUR_S:
                series.popleft()

    def feed_state(self, now_ms: int, now: float | None = None) -> FeedState:
        now = time.monotonic() if now is None else now
        self._prune(now)
        worst, worst_s = self.worst_coin(now_ms)
        slow, slow_ms = self.worst_delivery()
        return FeedState(
            name=self.name,
            connected=self.connected,
            last_msg_ms=self.last_msg_ms,
            lag_ms=0 if self.last_msg_ms == 0 else max(0, now_ms - self.last_msg_ms),
            msgs_1m=self.msgs_1m(now),
            gaps_1h=len(self._gap_times),
            reconnects_1h=len(self._reconnects),
            parse_errors_1h=len(self._parse_errors),  # already pruned above
            worst_coin=worst,
            worst_lag_ms=int(worst_s * 1000.0),
            cadence_ms=self.cadence_ms(),
            delivery_coin=slow,
            delivery_lag_ms=slow_ms,
        )


def gap_threshold_s(stat: StreamStats, cfg: Recorder) -> float:
    """Seconds of silence that count as a gap on this stream.

    One number per stream shape rather than one for all three, because the three
    Hyperliquid streams arrive in three different rhythms; see PER_COIN_GAP_STREAMS.
    A cadence stream that has not measured itself yet uses `gap_alert_s` as the stand-in
    cadence, so the first seconds of a process are judged at 3 x 5s rather than at 5s.
    """
    if stat.name in PER_COIN_GAP_STREAMS:
        return cfg.trades_gap_s
    if stat.cadence:
        return cfg.l2_gap_multiple * (stat.cadence_ms() / 1000.0 or cfg.gap_alert_s)
    return cfg.gap_alert_s


def silence_threshold_s(stat: StreamStats, cfg: Recorder) -> float:
    """Seconds of silence that raise a Telegram alert, which is never below a gap.

    `recorder.silence_alert_s` is 60s and a thin coin's trades go quiet for 92s in normal
    market, so taking the configured number alone would page the owner about a working
    feed the moment `trades` started counting gaps per coin.
    """
    return max(cfg.silence_alert_s, gap_threshold_s(stat, cfg))


async def _sleep_unless(stop: asyncio.Event, seconds: float, *also: asyncio.Event) -> bool:
    """Wait `seconds`. False when `stop` or one of `also` fired first instead.

    `stop` is the recorder shutting down. `also` is how a task that belongs to one socket
    is told that socket has gone: without it a task sleeping on a long timer holds the
    `_pump` task group open long after its connection died, and the stream neither
    delivers nor reconnects for the rest of the sleep.
    """
    events = (stop, *also)
    waiters = [asyncio.ensure_future(event.wait()) for event in events]
    try:
        await asyncio.wait(waiters, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
    return not any(event.is_set() for event in events)


def _describe(exc: BaseException) -> str:
    """Flatten an ExceptionGroup: a TaskGroup error otherwise logs as an empty mystery."""
    if isinstance(exc, BaseExceptionGroup):
        return " | ".join(_describe(inner) for inner in exc.exceptions)
    return f"{type(exc).__name__}: {exc}"


def _no_frames(_symbols: Sequence[str]) -> list[Any]:
    return []


def _no_diff(_added: Sequence[str], _removed: Sequence[str]) -> list[Any]:
    return []


class WSStream:
    """One venue connection: reconnects forever, parses, and hands rows to a sink.

    The sink is called from the receive loop, so it must not block. Buffers.add is a list
    append, which is why the recorder can get away without a queue between the two.
    """

    def __init__(
        self,
        venue: str,
        url: str,
        *,
        parse: Parser,
        sink: RowSink,
        cfg: Recorder,
        tables: Iterable[str] = (),
        on_connect: FramesFn = _no_frames,
        on_universe_change: DiffFramesFn = _no_diff,
        symbols: Iterable[str] = (),
        ping_s: float | None = None,
        ping_frame: Any = None,
        pong_marker: bytes | None = None,
        on_rtt_ms: Callable[[float], None] | None = None,
        rotate_after_s: float | None = None,
        rotate_overlap_s: float = ROTATE_OVERLAP_S,
    ) -> None:
        self.venue = venue
        self.url = url
        self.parse = parse
        self.sink = sink
        self.cfg = cfg
        self.on_connect = on_connect
        self.on_universe_change = on_universe_change
        self.symbols: list[str] = sorted(symbols)
        self.ping_s = ping_s
        self.ping_frame = ping_frame
        self.pong_marker = pong_marker
        self.on_rtt_ms = on_rtt_ms
        self.rotate_after_s = rotate_after_s
        self.rotate_overlap_s = rotate_overlap_s
        self.stats: dict[str, StreamStats] = {}
        # Declaring the tables up front means a stream that has never delivered a row
        # still shows on the panel as connected-but-silent rather than not existing.
        for table in tables:
            self._stat(table)
        self._ws: Any = None
        self._ping_sent: float | None = None
        # Raw payloads seen while two sockets overlap, so the changeover writes no row
        # twice. Empty and unused the rest of the time, which is all but a few seconds
        # every rotate_after_s.
        # Rotations currently overlapping, not a flag: a drain that runs long could
        # otherwise have one rotation's `finally` turn off the dedupe the next is using.
        self._overlaps = 0
        self._overlap_seen: set[bytes] = set()

    # --- stats ---------------------------------------------------------------

    def _stat(self, table: str) -> StreamStats:
        name = f"{self.venue}.{table}"
        stat = self.stats.get(name)
        if stat is None:
            stat = StreamStats(
                name=name,
                continuous=name in CONTINUOUS_STREAMS,
                per_coin=name in PER_COIN_STREAMS,
                per_coin_gaps=name in PER_COIN_GAP_STREAMS,
                cadence=name in CADENCE_STREAMS,
            )
            self.stats[name] = stat
        return stat

    def feed_states(self, now_ms: int) -> list[FeedState]:
        return [stat.feed_state(now_ms) for stat in self.stats.values()]

    def _set_connected(self, connected: bool) -> None:
        for stat in self.stats.values():
            stat.connected = connected

    # --- universe ------------------------------------------------------------

    async def set_symbols(self, symbols: Sequence[str]) -> None:
        """Point the stream at a new symbol set, subscribing the diff if connected.

        `self.symbols` advances only after every frame has gone out, so a send that
        raises leaves the stream on its old set and the caller can retry the whole diff.
        Re-sending a subscribe the venue already has is harmless on all three venues;
        recording a coin twice is not the failure worth guarding against here, and
        silently not recording it is.
        """
        new = sorted(symbols)
        added = [s for s in new if s not in self.symbols]
        removed = [s for s in self.symbols if s not in new]
        if not (added or removed):
            return
        if self._ws is None:
            self.symbols = new  # nothing to send; on_connect subscribes the whole set
            self._forget(removed)
            return
        for frame in self.on_universe_change(added, removed):
            await self._send(frame)
        self.symbols = new
        self._forget(removed)

    def _forget(self, removed: Sequence[str]) -> None:
        """Stop measuring per-coin silence for symbols the universe just dropped."""
        for stat in self.stats.values():
            for symbol in removed:
                stat.forget_coin(symbol)

    # --- connection ----------------------------------------------------------

    @staticmethod
    def _encode(frame: Any) -> str:
        """Frames go out as text. A binary frame is silently dropped by HL and OKX."""
        return frame if isinstance(frame, str) else orjson.dumps(frame).decode()

    async def _send(self, frame: Any) -> None:
        ws = self._ws
        if ws is None:
            return
        await ws.send(self._encode(frame))

    async def run(self, stop: asyncio.Event) -> None:
        """Connect and pump until `stop`. Never returns because of a network error."""
        attempt = 0
        while not stop.is_set():
            opened = time.monotonic()
            try:
                async with websockets.connect(
                    self.url,
                    max_size=None,  # HL l2Book frames for 20 levels are large but bounded
                    open_timeout=15,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._set_connected(True)
                    log.info("stream_connected", venue=self.venue, url=self.url)
                    for frame in self.on_connect(self.symbols):
                        await self._send(frame)
                    await self._pump(ws, stop)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("stream_disconnected", venue=self.venue, error=_describe(exc))
            else:
                log.info("stream_closed", venue=self.venue)
            finally:
                self._ws = None
                self._ping_sent = None
                self._set_connected(False)

            if stop.is_set():
                return
            for stat in self.stats.values():
                stat.record_reconnect()
            if time.monotonic() - opened > STABLE_CONNECTION_S:
                attempt = 0
            delay = backoff_delay(attempt, self.cfg)
            attempt += 1
            log.info(
                "stream_reconnecting", venue=self.venue, delay_s=round(delay, 2), attempt=attempt
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)

    async def _pump(self, ws: Any, stop: asyncio.Event) -> None:
        """Hold the venue's connection, replacing it before the venue expires it.

        Returns when no socket is live any more, which is what makes `run` reconnect. A
        rotation adds the replacement's tasks to the same group and retires the old
        socket, so the group is never empty across a changeover and `run` never sees it.
        """
        async with asyncio.TaskGroup() as group:
            self._serve(group, ws, stop)

    def _serve(self, group: asyncio.TaskGroup, ws: Any, stop: asyncio.Event) -> None:
        """Start the tasks one live socket needs. Every one of them ends when it closes.

        `closed` is set the moment this socket's reader ends, however it ended, and every
        other task of the socket waits on it alongside its own timer. Hyperliquid's
        `1000 (OK) Expired` reaches us as a clean end of `async for`, and it can arrive
        before the rotation point rather than after it, so the rotation timer is a sleep
        of up to ws_rotate_after_s (9018s on the box) that nothing else would interrupt:
        the group stayed open, `run` never reconnected, and the stream was simply dead.
        """
        closed = asyncio.Event()
        recv = group.create_task(self._recv_loop(ws), name=f"{self.venue}-recv")
        recv.add_done_callback(lambda _task: closed.set())
        if self.ping_s and self.ping_frame is not None:
            group.create_task(self._ping_loop(ws, stop, closed), name=f"{self.venue}-ping")
        group.create_task(self._close_on_stop(ws, stop), name=f"{self.venue}-stop")
        if self.rotate_after_s:
            group.create_task(
                self._rotate(group, ws, recv, stop, closed), name=f"{self.venue}-rotate"
            )

    async def _rotate(
        self,
        group: asyncio.TaskGroup,
        old: Any,
        old_recv: asyncio.Task[None],
        stop: asyncio.Event,
        closed: asyncio.Event,
    ) -> None:
        """Replace this socket before the venue expires it, so the swap loses no rows.

        Hyperliquid closes every connection with `1000 (OK) Expired` after 167 to 181
        minutes (seven measured on the box, 2026-09-05 to 2026-09-06) and each close cost
        an 18s hole in bbo and trades. The replacement is connected, subscribed and
        delivering before the old one is closed, and for the overlap both are sinked with
        the repeats dropped, because losing a row is worse than holding a few seconds of
        payloads: a duplicate can be found later and a missing row cannot.

        A rotation that cannot connect costs the hole it was meant to avoid and nothing
        else. The old socket is untouched, the venue expires it on its own schedule, and
        the reconnect ladder in `run` takes over as it did before.
        """
        after_s = self.rotate_after_s or 0.0  # `_serve` only starts this task when it is set
        if not after_s or not await _sleep_unless(stop, after_s, closed):
            return
        if old.close_code is not None:  # the venue got there first
            return
        # The dedupe goes on before the replacement is even connected, and comes off only
        # once the retired socket has handed over everything it was sent, so there is no
        # moment where two sockets are sinked and the repeats are not recognised.
        #
        # Both ends of that were wrong first and a test against a fake expiring venue
        # caught them. The venue starts broadcasting to a socket the moment its handshake
        # completes, which happens inside the connect await while this task is still
        # suspended there, so one message reached both sockets with the dedupe still off:
        # the old socket's copy was sinked and the replacement's then looked like a first
        # sight. try/finally, because a dedupe left on would grow without bound.
        self._overlaps += 1
        try:
            new = await self._connect_standby()
            if new is None:
                return
            self._ws = new
            self._serve(group, new, stop)
            for frame in self.on_connect(self.symbols):
                await new.send(self._encode(frame))
            log.info("stream_rotated", venue=self.venue, overlap_s=self.rotate_overlap_s)
            await _sleep_unless(stop, self.rotate_overlap_s)
            await old.close()
            # `close()` returns once the handshake is done, but frames the socket had
            # already received are still queued in front of its receive loop. The dedupe
            # has to outlive them: turning it off at the close let one of those through as
            # a duplicate of a message the replacement had already delivered.
            await asyncio.wait([old_recv], timeout=RETIRE_DRAIN_S)
        finally:
            self._overlaps -= 1
            if not self._overlaps:
                self._overlap_seen.clear()

    async def _connect_standby(self) -> Any:
        """The replacement socket, or None with the reason logged and the old one kept."""
        try:
            return await websockets.connect(
                self.url, max_size=None, open_timeout=15, close_timeout=5
            )
        except Exception as exc:
            log.warning("stream_rotate_failed", venue=self.venue, error=_describe(exc))
            return None

    async def _close_on_stop(self, ws: Any, stop: asyncio.Event) -> None:
        """Close this socket on shutdown, and end when it closes for any other reason.

        It used to wait on `stop` alone, so a socket that closed on its own left this task
        pending forever and the `_pump` group never emptied. On a venue with no ping loop
        to raise on the dead socket (Binance) that was a stream which quietly stopped
        reconnecting; with rotation it would be every retired connection.
        """
        while not stop.is_set() and ws.close_code is None:
            await _sleep_unless(stop, SOCKET_POLL_S)
        await ws.close()

    async def _ping_loop(self, ws: Any, stop: asyncio.Event, closed: asyncio.Event) -> None:
        every_s = self.ping_s or 0.0  # `_serve` only starts this task when it is set
        while every_s and not stop.is_set() and ws.close_code is None:
            if not await _sleep_unless(stop, every_s, closed):
                return
            # Only the socket the stream is actually using is pinged. A standby is by
            # definition fresh, and a retired one is about to go; pinging either would
            # attribute its round trip to the connection the panel is showing.
            if ws is not self._ws or ws.close_code is not None:
                continue
            self._ping_sent = time.monotonic()
            await ws.send(self._encode(self.ping_frame))

    async def _recv_loop(self, ws: Any) -> None:
        """Pump one socket until it closes. Raises only for the socket in use.

        Hyperliquid does not answer our close frame, so closing a socket we retired
        during a rotation raises `ConnectionClosedError: sent 1000 (OK); no close frame
        received` here. Letting that out took the `_pump` task group down, and with it
        the replacement's tasks, so `run` reconnected from scratch and every rotation
        still cost the hole and the resubscribe snapshot it existed to avoid. Measured on
        the box, 2026-09-06: two rotations, two reconnects.

        A socket that is no longer `self._ws` was retired by `_rotate` and its close is
        the point rather than a failure. The live connection dropping still raises, which
        is what makes `run` reconnect.
        """
        try:
            async for message in ws:
                raw = message.encode() if isinstance(message, str) else message
                # rx_ms is stamped here, before anything is parsed or queued, because
                # every no-lookahead check downstream is against this number.
                rx_ms = int(time.time() * 1000)
                self.handle(raw, rx_ms)
        except Exception as exc:
            if ws is self._ws:
                raise
            log.info("stream_retired", venue=self.venue, reason=_describe(exc))

    # --- message handling ----------------------------------------------------

    def handle(self, raw: bytes, rx_ms: int) -> None:
        """Parse one message and sink its rows. Never raises: a bad message is dropped."""
        if self._overlaps and self._duplicate_of_the_overlap(raw):
            return
        if self.pong_marker is not None and self.pong_marker in raw:
            self._record_pong()
        try:
            rows = self.parse(raw, rx_ms)
        except Exception as exc:
            # A message that will not parse cannot be attributed to a table, so it counts
            # against every table of this venue. They share one connection, so a venue
            # renaming a field makes all of them suspect anyway, and the alternative is a
            # counter nobody can find. `handle` is the only place that drops a whole
            # message; a single refused row is counted by its own table below.
            for stat in self.stats.values():
                stat.record_parse_error()
            log.warning(
                "parse_error",
                venue=self.venue,
                error=f"{type(exc).__name__}: {exc}",
                raw=raw[:MAX_LOGGED_PAYLOAD].decode("utf-8", "replace"),
            )
            return
        # Grouped by table so one message counts as one message, whatever it carried:
        # `hyperliquid.l2` emits twenty rows per frame and used to report twenty messages.
        # The coins go with it, because per-coin silence is what tells you which
        # subscription died on a venue that subscribes each coin separately.
        # coin -> the worst delivery lag this message carried for it, per table. The
        # worst rather than the last, because one message can hold a burst of replayed
        # trades and the oldest of them is the one worth measuring.
        seen: dict[str, dict[str, int]] = {}
        for table, row in rows:
            stat = self._stat(table)
            if row is None:
                stat.record_parse_error()
                continue
            coin = str(row.get("coin") or "")
            lags = seen.setdefault(table, {})
            ts_ms = row.get("ts_ms")
            lag = rx_ms - int(ts_ms) if row.get("rx_ms") and ts_ms is not None else None
            if lag is not None:
                lags[coin] = max(lags.get(coin, lag), lag)
            else:
                lags.setdefault(coin, 0)
            self.sink(table, self.venue, row)
        for table, lags in seen.items():
            self._stat(table).record_message(rx_ms, lags_ms=lags)

    def _duplicate_of_the_overlap(self, raw: bytes) -> bool:
        """True for a payload the other socket already delivered during a changeover.

        Keyed on the payload itself rather than on `(coin, ts_ms, tid)`, because the venue
        broadcasts the same bytes to both sockets and one key then covers every table:
        `bbo` and `l2` carry no id of their own, and a per table key would have to invent
        one. Held only for the overlap, then dropped.
        """
        if raw in self._overlap_seen:
            return True
        self._overlap_seen.add(raw)
        return False

    def _record_pong(self) -> None:
        if self._ping_sent is None:
            return
        rtt_ms = (time.monotonic() - self._ping_sent) * 1000.0
        self._ping_sent = None
        if self.on_rtt_ms is not None:
            self.on_rtt_ms(rtt_ms)
