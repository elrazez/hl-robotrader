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
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
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
# driven (a liquidation feed on a calm venue, a 1m candle) and silence means nothing;
# for those, only the connection being up is monitored. Bybit publishes every
# liquidation since 2025-02-25, which is why it is here and Binance and OKX are not.
CONTINUOUS_STREAMS = frozenset(
    {
        "hyperliquid.trades",
        "hyperliquid.bbo",
        "hyperliquid.l2",
        "bybit.liquidations",
    }
)

HOUR_S = 3600.0
STABLE_CONNECTION_S = 60.0  # a connection alive this long resets the backoff ladder
MAX_LOGGED_PAYLOAD = 500  # bytes of a bad message kept in the log line


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
    last_msg_ms: int = 0
    gaps_1h: int = 0
    _buckets: deque[list[float]] = field(default_factory=lambda: deque(maxlen=120))
    _gap_times: deque[float] = field(default_factory=deque)
    _reconnects: deque[float] = field(default_factory=deque)
    _parse_errors: deque[float] = field(default_factory=deque)
    _gap_open: bool = False

    def record_message(self, rx_ms: int, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.last_msg_ms = rx_ms
        self._gap_open = False
        second = float(int(now))
        if self._buckets and self._buckets[-1][0] == second:
            self._buckets[-1][1] += 1
        else:
            self._buckets.append([second, 1.0])

    def record_reconnect(self, now: float | None = None) -> None:
        self._reconnects.append(time.monotonic() if now is None else now)

    def record_parse_error(self, now: float | None = None) -> None:
        """One row the parser refused, or one message it could not read at all.

        Both mean the same thing operationally: the wire stopped matching the parser, and
        rows that should be in the lake are not. Counted over the trailing hour so a rate
        can be alerted on rather than a lifetime total nobody can interpret.
        """
        self._parse_errors.append(time.monotonic() if now is None else now)

    def check_silence(self, gap_alert_s: float, now_ms: int, now: float | None = None) -> bool:
        """Count a gap when a continuous stream has been quiet too long. Idempotent.

        One silence counts once, however long it lasts, so a stream that is down for an
        hour reads as one gap rather than 720. Returns True when this call counted one.
        """
        if not self.continuous or self.last_msg_ms == 0:
            return False
        if self._gap_open or (now_ms - self.last_msg_ms) / 1000.0 <= gap_alert_s:
            return False
        self._gap_open = True
        self.gaps_1h += 1
        self._gap_times.append(time.monotonic() if now is None else now)
        return True

    def silent_s(self, now_ms: int) -> float:
        """Seconds since the last message. 0 when the stream has never delivered one."""
        return 0.0 if self.last_msg_ms == 0 else (now_ms - self.last_msg_ms) / 1000.0

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
        return FeedState(
            name=self.name,
            connected=self.connected,
            last_msg_ms=self.last_msg_ms,
            lag_ms=0 if self.last_msg_ms == 0 else max(0, now_ms - self.last_msg_ms),
            msgs_1m=self.msgs_1m(now),
            gaps_1h=len(self._gap_times),
            reconnects_1h=len(self._reconnects),
            parse_errors_1h=len(self._parse_errors),
        )


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
        self.stats: dict[str, StreamStats] = {}
        # Declaring the tables up front means a stream that has never delivered a row
        # still shows on the panel as connected-but-silent rather than not existing.
        for table in tables:
            self._stat(table)
        self._ws: Any = None
        self._ping_sent: float | None = None

    # --- stats ---------------------------------------------------------------

    def _stat(self, table: str) -> StreamStats:
        name = f"{self.venue}.{table}"
        stat = self.stats.get(name)
        if stat is None:
            stat = StreamStats(name=name, continuous=name in CONTINUOUS_STREAMS)
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
            return
        for frame in self.on_universe_change(added, removed):
            await self._send(frame)
        self.symbols = new

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
        async with asyncio.TaskGroup() as group:
            group.create_task(self._recv_loop(ws), name=f"{self.venue}-recv")
            if self.ping_s and self.ping_frame is not None:
                group.create_task(self._ping_loop(ws, stop), name=f"{self.venue}-ping")
            group.create_task(self._close_on_stop(ws, stop), name=f"{self.venue}-stop")

    async def _close_on_stop(self, ws: Any, stop: asyncio.Event) -> None:
        await stop.wait()
        await ws.close()

    async def _ping_loop(self, ws: Any, stop: asyncio.Event) -> None:
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.ping_s)
            if stop.is_set():
                return
            self._ping_sent = time.monotonic()
            await ws.send(self._encode(self.ping_frame))

    async def _recv_loop(self, ws: Any) -> None:
        async for message in ws:
            raw = message.encode() if isinstance(message, str) else message
            # rx_ms is stamped here, before anything is parsed or queued, because every
            # no-lookahead check downstream is against this number.
            rx_ms = int(time.time() * 1000)
            self.handle(raw, rx_ms)

    # --- message handling ----------------------------------------------------

    def handle(self, raw: bytes, rx_ms: int) -> None:
        """Parse one message and sink its rows. Never raises: a bad message is dropped."""
        if self.pong_marker is not None and self.pong_marker in raw:
            self._record_pong()
        try:
            rows = self.parse(raw, rx_ms)
        except Exception as exc:
            log.warning(
                "parse_error",
                venue=self.venue,
                error=f"{type(exc).__name__}: {exc}",
                raw=raw[:MAX_LOGGED_PAYLOAD].decode("utf-8", "replace"),
            )
            return
        for table, row in rows:
            stat = self._stat(table)
            if row is None:
                stat.record_parse_error()
                continue
            stat.record_message(rx_ms)
            self.sink(table, self.venue, row)

    def _record_pong(self) -> None:
        if self._ping_sent is None:
            return
        rtt_ms = (time.monotonic() - self._ping_sent) * 1000.0
        self._ping_sent = None
        if self.on_rtt_ms is not None:
            self.on_rtt_ms(rtt_ms)
