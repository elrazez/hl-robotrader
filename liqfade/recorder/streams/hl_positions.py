"""The public liquidation map: Hyperliquid positions polled from `clearinghouseState`.

Not a stream. `clearinghouseState` is a keyless `/info` call that answers for *any*
address, and every position it returns carries `liquidationPx`, the venue's own computed
liquidation level. Poll the addresses the tape names (`AddressBook`, fed from the
`users` field of the public trades feed) and the `hl_positions` table becomes a
forward-looking map of what is queued to be force-closed at each price, by side.

Shaped like `CoinalyzePoller`, which is the recorder's other REST poller: one stream
declared only when it will actually poll, `set_universe`, `poll_once(client)` and `run`.

Three things this deliberately does not do:

- **It does not gather.** The addresses are asked one after another, so a slow venue
  stretches a poll rather than bursting the whole minute's rate budget into one instant.
  At the defaults that is 200 calls of weight 2 per minute, 400 of the 1200 per IP.
- **It is not in `CONTINUOUS_STREAMS`.** A poll is sparse by nature; only the connection
  is monitored, exactly as for `coinalyze.xchg_ctx`.
- **It records nothing but positions.** No feature, no strategy wiring. Whether the map
  measures anything is a preregistered research question that needs weeks of this table
  before it can be asked. See docs/research/external_bots_scan.md section 1.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import Any

import httpx
import orjson

from liqfade.config.models import HlPositions
from liqfade.log import get_logger
from liqfade.recorder.addresses import AddressBook
from liqfade.recorder.streams.base import RowSink, StreamStats

log = get_logger(__name__)

VENUE = "hyperliquid"
TABLES = ("hl_positions",)
STREAM = f"{VENUE}.positions"
TIMEOUT_S = 10.0


def _f(value: Any) -> float | None:
    """HL sends every decimal as a string. None and "" become null rather than 0.0."""
    if value is None or value == "":
        return None
    return float(value)


class HlPositionsPoller:
    """Polls the tracked addresses and sinks one row per open universe position."""

    def __init__(
        self,
        cfg: HlPositions,
        sink: RowSink,
        book: AddressBook,
        info_base: str,
        *,
        book_path: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.sink = sink
        self.book = book
        self.info_base = info_base.rstrip("/")
        self.book_path = book_path
        self.coins: list[str] = []
        # A poller that will never poll declares no stream: a declared-but-idle stream
        # reads as disconnected to the health check and pages Telegram every ten minutes.
        self.active = bool(cfg.enabled)
        self.stats = {STREAM: StreamStats(name=STREAM)} if self.active else {}

    @property
    def _stat(self) -> StreamStats:
        return self.stats[STREAM]

    def set_universe(self, coins: list[str]) -> None:
        self.coins = sorted(coins)

    def feed_states(self, now_ms: int) -> list[Any]:
        return [stat.feed_state(now_ms) for stat in self.stats.values()]

    # --- one poll ------------------------------------------------------------

    async def poll_once(self, client: httpx.AsyncClient) -> int:
        """Ask every tracked address in rank order. Returns the number of rows sunk.

        An address that fails is logged, counted as a parse error on the stream and
        skipped, because one wallet answering badly is not a broken feed. A poll where
        *every* address failed is, and sets `connected` false. An empty book is neither:
        on day one there is nothing to ask and that is not a failure.
        """
        addresses = self.book.top(self.cfg.max_addresses)
        wanted = set(self.coins)
        rx_ms = int(time.time() * 1000)
        rows = 0
        failed = 0
        for address in addresses:
            try:
                state = await self._state(client, address)
            except Exception as exc:
                failed += 1
                self._stat.record_parse_error()
                log.warning(
                    "hl_positions_address_failed",
                    address=address,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            rows += self._sink_positions(address, state, wanted, rx_ms)

        self._stat.record_message(rx_ms)  # one poll is one message, whatever it returned
        if addresses and failed == len(addresses):
            self._stat.connected = False
        if self.book_path is not None:
            self.book.save(self.book_path)
        return rows

    async def _state(self, client: httpx.AsyncClient, address: str) -> dict[str, Any]:
        response = await client.post(
            f"{self.info_base}/info", json={"type": "clearinghouseState", "user": address}
        )
        response.raise_for_status()
        return orjson.loads(response.content)  # type: ignore[no-any-return]

    def _sink_positions(
        self, address: str, state: dict[str, Any], wanted: set[str], rx_ms: int
    ) -> int:
        """One row per open position in the universe. Returns how many were written."""
        ts_ms = int(state.get("time") or rx_ms)
        written = 0
        for entry in state.get("assetPositions") or []:
            position = entry.get("position") or {}
            coin = str(position.get("coin"))
            size = _f(position.get("szi"))
            # A coin the account is flat in stays in assetPositions with szi "0.0", the
            # same fact execution/hl.py::parse_clearinghouse_state already drops.
            if coin not in wanted or not size:
                continue
            leverage = position.get("leverage") or {}
            self.sink(
                "hl_positions",
                VENUE,
                {
                    "coin": coin,
                    "ts_ms": ts_ms,
                    "rx_ms": rx_ms,
                    "address": address,
                    "szi": size,
                    "entry_px": _f(position.get("entryPx")),
                    # Null stays null. A position with no liquidation level is a fact
                    # about the account; a zero would put a cluster at price zero.
                    "liquidation_px": _f(position.get("liquidationPx")),
                    "position_value_usd": _f(position.get("positionValue")),
                    "margin_used_usd": _f(position.get("marginUsed")),
                    "leverage": _f(leverage.get("value")),
                    "leverage_type": leverage.get("type"),
                    "max_leverage": position.get("maxLeverage"),
                },
            )
            written += 1
        return written

    # --- the loop ------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Poll every `poll_s`. A poll that overruns starts the next one at once."""
        if not self.active:
            log.warning(
                "hl_positions_disabled", reason="venues.hyperliquid.positions.enabled is false"
            )
            return
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            while not stop.is_set():
                started = time.monotonic()
                try:
                    rows = await self.poll_once(client)
                    self._stat.connected = True
                    log.info("hl_positions_poll", rows=rows, addresses=len(self.book.entries))
                except Exception as exc:
                    self._stat.connected = False
                    log.warning("hl_positions_poll_failed", error=f"{type(exc).__name__}: {exc}")
                # A poll that took longer than the interval starts the next one at once
                # rather than queueing polls behind each other.
                delay = max(0.0, self.cfg.poll_s - (time.monotonic() - started))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=delay)
