"""The public liquidation map: Hyperliquid positions polled from `clearinghouseState`.

Not a stream. `clearinghouseState` is a keyless `/info` call that answers for *any*
address, and every position it returns carries `liquidationPx`, the venue's own computed
liquidation level. Poll the addresses the tape names (`AddressBook`, fed from the
`users` field of the public trades feed) and the `hl_positions` table becomes a
forward-looking map of what is queued to be force-closed at each price, by side.

Every cycle asks two tiers, both drawn by `select_addresses`. The *priority* set is the
addresses whose last snapshot held a liquidation level within `priority_pct` of that
coin's mark, biggest position first: the ones whose forced flow can actually arrive soon.
The *sweep* is a rotating cursor over the whole book, so every harvested address comes
round on a fixed period and can be promoted. Ranking on cumulative traded notional alone
spent the budget on positions nowhere near their liquidation price, which cannot be this
hour's cascade fuel: on 2026-09-09 only $143M of $1,561M tracked notional sat within 5%
of mark. See docs/DECISIONS.md, 2026-09-09.

Shaped like `CoinalyzePoller`, which is the recorder's other REST poller: one stream
declared only when it will actually poll, `set_universe`, `poll_once(client)` and `run`.

Three things this deliberately does not do:

- **It does not gather.** The addresses are asked one after another, so a slow venue
  stretches a poll rather than bursting the whole minute's rate budget into one instant.
  At the defaults that is 400 calls of weight 2 per minute, 800 of the 1200 per IP.
  What it really spends is measured, not asserted: `StreamStats.weight_per_min` is
  logged every cycle and the config is sized off that. docs/HYPERLIQUID.md carries the
  split of the shared budget across the processes on the box.
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
from collections.abc import Mapping, Sequence
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
# `clearinghouseState` costs 2 of the 1200 weight per minute the whole box shares.
WEIGHT_PER_ADDRESS = 2


def select_addresses(
    book: AddressBook,
    last_snapshot: Sequence[Mapping[str, Any]],
    marks: Mapping[str, float],
    cfg: HlPositions,
    *,
    cursor: int = 0,
) -> tuple[list[str], int]:
    """The addresses to ask this cycle, in order, and where the sweep resumes next time.

    Args:
        book: the ranked address book.
        last_snapshot: the rows the previous cycle sank, which is what priority is built
            from. An empty one yields a pure sweep, which is what a restart gets.
        marks: coin -> last mark price, USD.
        cfg: the poller config.
        cursor: index into the ranked book where the sweep resumes.

    Returns:
        The addresses, priority first, and the next cursor.
    """
    total = min(cfg.max_addresses, cfg.priority_addresses + cfg.sweep_addresses)
    picked = _priority(last_snapshot, marks, cfg)[: cfg.priority_addresses]
    asked = set(picked)
    # A priority set short of its cap hands the slack to the sweep rather than under-
    # spending the budget: on a restart the priority set is empty and the whole cycle is
    # sweep, which is how the poller heals itself.
    ranked = book.top(len(book.entries))
    if ranked:
        index = cursor % len(ranked)
        for _ in range(len(ranked)):
            if len(picked) >= total:
                break
            address = ranked[index]
            index = (index + 1) % len(ranked)
            if address in asked:
                continue
            picked.append(address)
            asked.add(address)
        cursor = index
    return picked, cursor


def _marks(rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """coin -> mark price, USD, read back out of the rows themselves.

    `positionValue` is the venue's own `abs(szi) * markPx`, so a poll that saw any
    position in a coin already knows that coin's mark to within its own rounding and the
    poller needs no second feed to judge how near a liquidation level is. Up to `poll_s`
    stale, which a band as wide as `priority_pct` absorbs.
    """
    marks: dict[str, float] = {}
    for row in rows:
        size = abs(float(row.get("szi") or 0.0))
        value = float(row.get("position_value_usd") or 0.0)
        if size and value:
            marks[str(row["coin"])] = value / size
    return marks


def _priority(
    last_snapshot: Sequence[Mapping[str, Any]],
    marks: Mapping[str, float],
    cfg: HlPositions,
) -> list[str]:
    """Addresses holding a position whose liquidation level is within `priority_pct` of
    that coin's mark, biggest position first. These are the ones whose forced flow can
    actually arrive soon; a position 60% away cannot be this hour's cascade fuel."""
    near: dict[str, float] = {}
    for row in last_snapshot:
        liquidation_px = row.get("liquidation_px")
        mark = marks.get(str(row.get("coin")))
        # A null liquidation level is a fact about the account, not a level at price zero.
        if liquidation_px is None or not mark:
            continue
        if abs(float(liquidation_px) - mark) / mark * 100.0 > cfg.priority_pct:
            continue
        address = str(row.get("address"))
        value = float(row.get("position_value_usd") or 0.0)
        near[address] = max(near.get(address, 0.0), value)
    return [address for address, _ in sorted(near.items(), key=lambda item: (-item[1], item[0]))]


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
        # Rebuilt from the last poll and never persisted: a restart simply has no priority
        # set, fills its first cycles from the sweep and heals itself within one sweep period.
        self.last_rows: list[dict[str, Any]] = []
        self.sweep_cursor = 0
        self.asked = 0  # addresses the last cycle asked about. Logged, not acted on.
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
        addresses, self.sweep_cursor = select_addresses(
            self.book,
            self.last_rows,
            _marks(self.last_rows),
            self.cfg,
            cursor=self.sweep_cursor,
        )
        self.asked = len(addresses)
        wanted = set(self.coins)
        started_ms = int(time.time() * 1000)
        fresh: list[dict[str, Any]] = []
        rows = 0
        failed = 0
        for address in addresses:
            # Recorded before the ask: a request the venue rejects has still been paid for.
            self._stat.record_weight(WEIGHT_PER_ADDRESS)
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
            # Stamped per address, after its response arrives, and not once for the poll.
            # The addresses are walked sequentially, so a poll of 200 spans several
            # seconds (6.5s measured on the box 2026-09-08): one rx_ms for the whole poll
            # dated every row after the first up to that much *earlier* than it actually
            # arrived, which is lookahead, and CLAUDE.md rule 1 has no tolerance for it.
            written = self._sink_positions(address, state, wanted, int(time.time() * 1000))
            fresh.extend(written)
            rows += len(written)

        # What the next cycle ranks its priority set from. Replaced rather than merged: a
        # position closed since the last poll must stop counting as fuel, and an address the
        # sweep did not reach this cycle keeps no stale row to be picked on.
        self.last_rows = fresh
        # One poll is one message, whatever it returned, timed from when it began.
        self._stat.record_message(started_ms)
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
    ) -> list[dict[str, Any]]:
        """One row per open position in the universe. Returns the rows written."""
        ts_ms = int(state.get("time") or rx_ms)
        written: list[dict[str, Any]] = []
        for entry in state.get("assetPositions") or []:
            position = entry.get("position") or {}
            coin = str(position.get("coin"))
            size = _f(position.get("szi"))
            # A coin the account is flat in stays in assetPositions with szi "0.0", the
            # same fact execution/hl.py::parse_clearinghouse_state already drops.
            if coin not in wanted or not size:
                continue
            leverage = position.get("leverage") or {}
            row = {
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
            }
            self.sink("hl_positions", VENUE, row)
            written.append(row)
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
                    log.info(
                        "hl_positions_poll",
                        rows=rows,
                        book=len(self.book.entries),
                        asked=self.asked,
                        weight_per_min=self._stat.weight_per_min(),
                        cycle_s=round(time.monotonic() - started, 2),
                    )
                except Exception as exc:
                    self._stat.connected = False
                    log.warning("hl_positions_poll_failed", error=f"{type(exc).__name__}: {exc}")
                # A poll that took longer than the interval starts the next one at once
                # rather than queueing polls behind each other.
                delay = max(0.0, self.cfg.poll_s - (time.monotonic() - started))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=delay)
