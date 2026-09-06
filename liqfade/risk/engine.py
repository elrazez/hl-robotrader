"""The risk engine: run every gate, and keep the state the gates remember.

`check_entry` is the one place an entry is allowed or refused. It runs the gates in a fixed
order, kill switch first, and returns the first reason to refuse along with whatever action
the caller has to take beyond not entering: some gates only block, and two of them also
require the book to be flattened.

The mutators (`on_fill`, `on_exit`, `on_reject`, `on_reconnect`, `on_equity`) are what give
the gates something to remember, and every one of them persists before returning. That is
the whole point of `risk/state.py`: a limit that a restart clears is not a limit.

Nothing here imports `strategy/`. CLAUDE.md requires the kill path to stand on its own, and
the cheapest way to keep that true is for the module to have no way to reach the strategy
at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from liqfade.config.models import Settings
from liqfade.risk import gates
from liqfade.risk.state import ClusterEntry, RiskState, RiskStateStore
from liqfade.strategy.types import Side

__all__ = ["AccountView", "EntryRequest", "RiskAction", "RiskEngine", "RiskVerdict"]


class RiskAction(Enum):
    """What the caller must do beyond declining the entry."""

    NONE = "none"
    CANCEL_RESTING = "cancel_resting"
    FLATTEN = "flatten"


# Gates whose refusal also requires action. Everything else only blocks new entries.
ACTIONS: dict[str, RiskAction] = {
    "kill_switch": RiskAction.FLATTEN,
    "drawdown": RiskAction.FLATTEN,
    "global_feed_dead": RiskAction.FLATTEN,
    "global_feed_stale": RiskAction.CANCEL_RESTING,
}

# Gates that latch a halt rather than being re-evaluated every tick. The drawdown halt has no
# expiry and needs a manual resume; the streak halt expires on its own.
LATCHING = ("drawdown", "daily_loss", "losing_streak", "global_feed_dead")


@dataclass(frozen=True, slots=True)
class EntryRequest:
    """One proposed entry, already sized.

    Args:
        coin: the symbol.
        side: the position to take.
        notional_usd: what sizing decided, USD. The cluster cap needs it.
        now_ms: the decision timestamp.
    """

    coin: str
    side: Side
    notional_usd: float
    now_ms: int


@dataclass(frozen=True, slots=True)
class AccountView:
    """What the gates need to know about the account and the feeds right now.

    Args:
        equity: live equity, USD.
        open_sides: coin to the side of the position currently open in it.
        coin_last_data_ms: coin to the last timestamp data arrived for it. A coin absent
            from this mapping has never been seen, which is staler than stale.
        global_last_data_ms: last timestamp any data arrived. None means never.
    """

    equity: float
    open_sides: Mapping[str, Side]
    coin_last_data_ms: Mapping[str, int]
    global_last_data_ms: int | None


@dataclass(frozen=True, slots=True)
class RiskVerdict:
    """Allowed or not, why, and what else to do about it."""

    reason: str | None
    action: RiskAction = RiskAction.NONE

    @property
    def allowed(self) -> bool:
        return self.reason is None


class RiskEngine:
    """Holds the persisted state and applies every gate to it."""

    def __init__(self, settings: Settings, *, path: Path | None = None) -> None:
        """
        Args:
            settings: resolved config.
            path: where the state lives. **None means in memory**, which is what a backtest
                wants: a replay has no prior halts or cooldowns, and inheriting the live
                account's would make it depend on what the bot did this morning. The live
                loop passes `settings.risk_state_path` explicitly.
        """
        self.settings = settings
        self.store = RiskStateStore(path)

    @property
    def state(self) -> RiskState:
        return self.store.state

    # --- decisions ------------------------------------------------------------

    def check_entry(self, request: EntryRequest, account: AccountView) -> RiskVerdict:
        """Run every gate in order and return the first refusal.

        Order matters twice. The kill switch is first because nothing may outrank it. The
        cluster cap is last of the blocking gates because it is the only one that depends
        on the proposed size, so a trade refused for any other reason never touches it.
        """
        settings, state, now = self.settings, self.state, request.now_ms
        self._roll_day(account.equity, now)
        self._expire_halt(now)

        reasons = (
            gates.kill_switch(settings),
            gates.halted(state, now),
            gates.drawdown(state, account.equity, settings),
            gates.daily_loss(state, settings),
            gates.losing_streak(state, settings),
            gates.storm(state, now, settings),
            gates.reject_storm(state, now, settings),
            gates.stale_data(
                account.coin_last_data_ms.get(request.coin),
                account.global_last_data_ms,
                now,
                settings,
            ),
            gates.coin_cooldown(state, request.coin, now),
            gates.position_count(account.open_sides, request.coin, request.side, settings),
            gates.cluster_exposure(
                state,
                side=request.side,
                notional_usd=request.notional_usd,
                equity=account.equity,
                now_ms=now,
                settings=settings,
            ),
        )
        for reason in reasons:
            if reason is not None:
                self._latch(reason, now)
                return RiskVerdict(reason, ACTIONS.get(reason, RiskAction.NONE))
        return RiskVerdict(None)

    # --- mutations ------------------------------------------------------------

    def on_entry(self, request: EntryRequest) -> None:
        """Record an entry that was actually taken, for the cluster cap."""
        self.state.entries.append(
            ClusterEntry(ts_ms=request.now_ms, side=request.side, notional_usd=request.notional_usd)
        )
        self._persist(request.now_ms)

    def on_exit(self, coin: str, realized_pnl: float, now_ms: int) -> None:
        """A position closed. Updates the day's PnL, the streak and the coin's cooldown."""
        state = self.state
        state.daily_realized_pnl += realized_pnl
        risk = self.settings.risk
        if realized_pnl < 0:
            state.consecutive_losses += 1
            cooldown_min = risk.coin_cooldown_after_loss_min
        else:
            state.consecutive_losses = 0
            cooldown_min = risk.coin_cooldown_after_exit_min
        # The loss cooldown is the longer of the two, and a win still gets the short one:
        # re-entering the same coin in the same minute is the same cascade, not a new one.
        state.coin_cooldowns[coin] = now_ms + int(
            max(cooldown_min, risk.coin_cooldown_after_exit_min) * 60_000
        )
        if state.consecutive_losses >= risk.max_consecutive_losses:
            self._halt("losing_streak", now_ms + int(risk.cooldown_after_streak_min * 60_000))
        self._persist(now_ms)

    def on_reject(self, now_ms: int) -> None:
        """An exchange reject. Enough of them in ten minutes halts entries."""
        self.state.reject_ms.append(now_ms)
        self._persist(now_ms)

    def on_reconnect(self, now_ms: int) -> None:
        """A critical feed reconnected. Enough of them in ten minutes halts entries."""
        self.state.reconnect_ms.append(now_ms)
        self._persist(now_ms)

    def on_equity(self, equity: float, now_ms: int) -> None:
        """Mark equity, rolling the day and raising the peak.

        The peak never decreases. A drawdown limit that reset after a loss would let the
        account walk down in steps that each looked small.
        """
        self._roll_day(equity, now_ms)
        if equity > self.state.peak_equity:
            self.state.peak_equity = equity
            self._persist(now_ms)

    def resume(self, now_ms: int) -> str | None:
        """Clear a halt. Returns the reason cleared, or None when nothing was halted.

        Refuses while the kill file is present: `/resume` must not be a way to trade
        through a kill switch, and the file is the one control that outranks every other.
        """
        if self.settings.kill_switch_present:
            return None
        reason = self.state.halt_reason
        self.state.halt_reason = None
        self.state.halt_until_ms = None
        self._persist(now_ms)
        return reason

    # --- internals ------------------------------------------------------------

    def _latch(self, reason: str, now_ms: int) -> None:
        """Turn a gate refusal into a persisted halt, where that gate latches."""
        if reason not in LATCHING or self.state.halt_reason == reason:
            return
        risk = self.settings.risk
        if reason == "daily_loss":
            # Until the next UTC day, which is when day_start_equity resets.
            until: int | None = self.state.day_start_ms + gates.DAY_MS
        elif reason == "losing_streak":
            until = now_ms + int(risk.cooldown_after_streak_min * 60_000)
        else:
            # Drawdown and a dead feed need a person to look, so they do not expire.
            until = None
        self._halt(reason, until)
        self._persist(now_ms)

    def _expire_halt(self, now_ms: int) -> None:
        """Clear a halt whose cooldown has elapsed, and the counter that would re-arm it.

        The losing streak needs both. Lifting the halt while `consecutive_losses` still sat
        at the limit would let `losing_streak` re-halt on the very next check, making a
        cooldown that is supposed to last two hours last forever. Serving the cooldown is
        the penalty, so afterwards the count starts again from zero.
        """
        state = self.state
        if state.halt_reason is None or state.halt_until_ms is None:
            return
        if now_ms < state.halt_until_ms:
            return
        if state.halt_reason == "losing_streak":
            state.consecutive_losses = 0
        state.halt_reason = None
        state.halt_until_ms = None
        self._persist(now_ms)

    def _halt(self, reason: str, until_ms: int | None) -> None:
        self.state.halt_reason = reason
        self.state.halt_until_ms = until_ms

    def _roll_day(self, equity: float, now_ms: int) -> None:
        """Start a new UTC day when one begins, resetting the daily budget."""
        midnight = _midnight_ms(now_ms)
        state = self.state
        if state.day_start_ms == midnight:
            return
        state.day_start_ms = midnight
        state.day_start_equity = equity
        state.daily_realized_pnl = 0.0
        if state.peak_equity <= 0:
            state.peak_equity = equity
        if state.halt_reason == "daily_loss":
            # The only halt a new day clears by itself; every other one outlives midnight.
            state.halt_reason = None
            state.halt_until_ms = None
        self._persist(now_ms)

    def _persist(self, now_ms: int) -> None:
        self.state.prune(
            now_ms,
            cluster_window_s=self.settings.risk.cluster_window_s,
            storm_window_s=gates.STORM_WINDOW_S,
        )
        self.store.save()


def _midnight_ms(now_ms: int) -> int:
    """Midnight UTC of the day `now_ms` falls in."""
    moment = datetime.fromtimestamp(now_ms / 1000.0, UTC)
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)
