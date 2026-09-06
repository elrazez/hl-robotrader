"""The small persisted risk state, and its atomic store.

Every limit in `docs/STRATEGY_AND_RISK.md` section 5 that has memory lives here: the day's
starting equity, the peak the account has ever reached, realized PnL so far today, the
losing streak, per-coin cooldowns, the current halt, and the rolling windows behind the
reconnect and reject storm gates.

**It is written on every mutation, not on a timer.** A daily loss limit that resets because
the process restarted is not a limit, and the whole reason this file exists is that a
crash at the wrong moment must not hand the strategy a fresh 3% to lose. The write is temp
file plus `os.replace`, the same pattern as `control/state.py`, so a reader sees either the
previous complete state or the new one and never half of either.

The rolling entry log behind the cluster cap is here too. It is pruned on every write, so
the file cannot grow without bound over a run: only entries inside `cluster_window_s` of
the last mutation are kept.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import orjson
from pydantic import BaseModel, ConfigDict, Field

from liqfade.strategy.types import Side

__all__ = ["ClusterEntry", "RiskState", "RiskStateStore"]


class ClusterEntry(BaseModel):
    """One entry taken, for the per-cluster exposure cap."""

    model_config = ConfigDict(extra="forbid")

    ts_ms: int
    side: Side
    notional_usd: float


class RiskState(BaseModel):
    """Everything the risk gates remember between decisions.

    Args:
        day_start_equity: equity at the start of the current UTC day. The daily loss limit
            is a fraction of this, not of current equity, so a bad day cannot enlarge its
            own budget as it goes.
        day_start_ms: midnight UTC of the current day, so a roll is detectable.
        peak_equity: the highest equity ever seen. The drawdown halt measures from here and
            it never decreases, because a drawdown limit that resets after a loss lets the
            account walk down in steps that each look small.
        daily_realized_pnl: realized PnL since `day_start_ms`, USD.
        consecutive_losses: losing exits in a row. Reset by any win.
        coin_cooldowns: coin to the millisecond it may be entered again.
        halt_reason: why entries are halted, or None.
        halt_until_ms: when the halt lifts. None with a `halt_reason` set means it needs a
            manual `/resume`, which is what the drawdown halt uses.
        reconnect_ms: timestamps of recent critical-feed reconnects.
        reject_ms: timestamps of recent exchange rejects.
        entries: recent entries, for the cluster cap.
    """

    model_config = ConfigDict(extra="forbid")

    day_start_equity: float = 0.0
    day_start_ms: int = 0
    peak_equity: float = 0.0
    daily_realized_pnl: float = 0.0
    consecutive_losses: int = 0
    coin_cooldowns: dict[str, int] = Field(default_factory=dict)
    halt_reason: str | None = None
    halt_until_ms: int | None = None
    reconnect_ms: list[int] = Field(default_factory=list)
    reject_ms: list[int] = Field(default_factory=list)
    entries: list[ClusterEntry] = Field(default_factory=list)

    def cluster_exposure(self, side: Side, now_ms: int, window_s: float) -> float:
        """Gross notional entered on `side` inside the trailing cluster window, USD."""
        since = now_ms - int(window_s * 1000)
        return sum(
            entry.notional_usd
            for entry in self.entries
            if entry.side == side and entry.ts_ms >= since
        )

    def prune(self, now_ms: int, *, cluster_window_s: float, storm_window_s: float) -> None:
        """Drop everything outside the windows that can still read it.

        Called on every mutation so the file stays a fixed size over a run of any length.
        Expired cooldowns go too: a dict keyed by coin would otherwise accumulate one entry
        per coin ever traded and never lose them.
        """
        cluster_since = now_ms - int(cluster_window_s * 1000)
        storm_since = now_ms - int(storm_window_s * 1000)
        self.entries = [entry for entry in self.entries if entry.ts_ms >= cluster_since]
        self.reconnect_ms = [ts for ts in self.reconnect_ms if ts >= storm_since]
        self.reject_ms = [ts for ts in self.reject_ms if ts >= storm_since]
        self.coin_cooldowns = {
            coin: until for coin, until in self.coin_cooldowns.items() if until > now_ms
        }


class RiskStateStore:
    """Loads `data/risk_state.json`, and writes it atomically on every mutation.

    Not async and not buffered. The write happens a handful of times per trade, and the
    cost of an fsync there is nothing against the cost of a limit that forgot itself.

    **`path=None` means in memory**, and that is what a backtest uses. A replay has no
    prior state: inheriting the live account's halts and cooldowns would make a backtest
    depend on what the bot did this morning, and writing them back could clear a halt
    nobody had resolved. Persistence belongs to the live loop, which passes a real path.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = Path(path) if path is not None else None
        self.state = self._load()

    def _load(self) -> RiskState:
        """Read the file, or start fresh when it is absent.

        A corrupt file raises rather than being replaced with a blank state: silently
        starting over would clear a halt the owner has not resolved, and the halt is the
        thing most likely to matter at the moment a file gets corrupted.
        """
        if self.path is None or not self.path.exists():
            return RiskState()
        return RiskState.model_validate(orjson.loads(self.path.read_bytes()))

    def save(self) -> None:
        """Write the state atomically, 0600, in the directory it lives in. No-op in memory."""
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        payload = orjson.dumps(self.state.model_dump(mode="json"), option=orjson.OPT_INDENT_2)
        try:
            with open(tmp, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()
