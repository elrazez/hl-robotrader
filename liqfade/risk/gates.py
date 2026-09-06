"""One pure function per risk gate. Each returns a reason string, or None to allow.

Every gate in `docs/STRATEGY_AND_RISK.md` section 5 is here, and none of them reads a file,
a clock or the strategy. `now_ms` and the account view arrive as arguments so a gate gives
the same answer in a backtest replay as it does live, and so `tests/test_risk.py` can put
the account in any state it likes without constructing a portfolio.

The kill switch is deliberately the first thing `RiskEngine.check_entry` consults and is
implemented here rather than anywhere near `strategy/`: CLAUDE.md requires that the kill
path not depend on the strategy module, so this file imports nothing from it beyond the
`Side` type alias.
"""

from __future__ import annotations

from collections.abc import Mapping

from liqfade.config.models import Settings
from liqfade.risk.state import RiskState
from liqfade.strategy.types import Side

__all__ = [
    "cluster_exposure",
    "coin_cooldown",
    "daily_loss",
    "drawdown",
    "halted",
    "kill_switch",
    "losing_streak",
    "position_count",
    "reject_storm",
    "stale_data",
    "storm",
]

DAY_MS = 86_400_000
STORM_WINDOW_S = 600.0  # the ten minutes both storm gates count over
# Relative slack on the cluster cap comparison, for float arithmetic only.
# `sizing.coin_exposure_frac` and `risk.max_cluster_exposure_frac` are both 0.25 by
# default, so a trade sized at exactly the per-coin cap lands exactly on the cluster cap,
# and `size * price` can land a few ulps either side of it. Sizing floors the size so the
# real overshoot is gone; this covers the last bit of the multiplication. A notional a
# billionth over the cap is not over the cap.
_CAP_EPSILON = 1e-9


def kill_switch(settings: Settings) -> str | None:
    """`data/KILL` present blocks everything.

    Reads `Settings.kill_switch_present`, which answers True for any error that is not a
    clean "not found": an unreadable or unmounted data directory must not read as no kill
    file. This gate fails closed on purpose and is the only one that does.
    """
    return "kill_switch" if settings.kill_switch_present else None


def halted(state: RiskState, now_ms: int) -> str | None:
    """A halt set by an earlier gate, until it expires or is cleared by hand.

    `halt_until_ms` of None with a reason set means the halt does not expire and needs a
    manual `/resume`. That is the drawdown halt: an account down 10% from its peak should
    not resume trading because a timer elapsed.
    """
    if state.halt_reason is None:
        return None
    if state.halt_until_ms is None:
        return state.halt_reason
    if now_ms < state.halt_until_ms:
        return state.halt_reason
    return None


def daily_loss(state: RiskState, settings: Settings) -> str | None:
    """Down `max_daily_loss_frac` of the equity the day started with.

    Measured against `day_start_equity` rather than current equity, so a losing day cannot
    enlarge its own budget as it shrinks the denominator.
    """
    if state.day_start_equity <= 0:
        return None
    limit = -settings.risk.max_daily_loss_frac * state.day_start_equity
    return "daily_loss" if state.daily_realized_pnl <= limit else None


def drawdown(state: RiskState, equity: float, settings: Settings) -> str | None:
    """Down `max_dd_frac` from the highest equity ever reached. Also flattens."""
    if state.peak_equity <= 0:
        return None
    floor = (1.0 - settings.risk.max_dd_frac) * state.peak_equity
    return "drawdown" if equity <= floor else None


def losing_streak(state: RiskState, settings: Settings) -> str | None:
    """`max_consecutive_losses` in a row. The halt this sets carries its own expiry."""
    if state.consecutive_losses >= settings.risk.max_consecutive_losses:
        return "losing_streak"
    return None


def coin_cooldown(state: RiskState, coin: str, now_ms: int) -> str | None:
    """This coin was exited recently. The length depends on whether it lost."""
    until = state.coin_cooldowns.get(coin)
    return "coin_cooldown" if until is not None and now_ms < until else None


def position_count(
    open_sides: Mapping[str, Side], coin: str, side: Side, settings: Settings
) -> str | None:
    """Concurrent positions overall, and per direction.

    A coin already open is not blocked here: adding to an existing position is bounded by
    `sizing.coin_exposure_frac`, and refusing it would leave a position that could not be
    scaled into. Only a new coin counts against the concurrency limits.
    """
    if coin in open_sides:
        return None
    risk = settings.risk
    if len(open_sides) >= risk.max_concurrent_positions:
        return "max_positions"
    same_side = sum(1 for held in open_sides.values() if held == side)
    if same_side >= risk.max_positions_per_direction:
        return "max_positions_per_direction"
    return None


def stale_data(
    coin_last_ms: int | None, global_last_ms: int | None, now_ms: int, settings: Settings
) -> str | None:
    """No recent data for this coin, or none at all.

    Never having seen data is staler than having seen it long ago, so None blocks. A
    strategy that traded on a feed it has never received a message from would be trading on
    the absence of contrary evidence.
    """
    risk = settings.risk
    if global_last_ms is None or (now_ms - global_last_ms) / 1000.0 >= risk.stale_global_flatten_s:
        return "global_feed_dead"
    if (now_ms - global_last_ms) / 1000.0 >= risk.stale_global_cancel_s:
        return "global_feed_stale"
    if coin_last_ms is None or (now_ms - coin_last_ms) / 1000.0 >= risk.stale_coin_s:
        return "stale_coin_data"
    return None


def storm(state: RiskState, now_ms: int, settings: Settings) -> str | None:
    """More reconnects in ten minutes than `max_reconnects_10min` allows."""
    since = now_ms - int(STORM_WINDOW_S * 1000)
    recent = sum(1 for ts in state.reconnect_ms if ts >= since)
    return "reconnect_storm" if recent > settings.risk.max_reconnects_10min else None


def reject_storm(state: RiskState, now_ms: int, settings: Settings) -> str | None:
    """More exchange rejects in ten minutes than `max_rejects_10min` allows."""
    since = now_ms - int(STORM_WINDOW_S * 1000)
    recent = sum(1 for ts in state.reject_ms if ts >= since)
    return "reject_storm" if recent > settings.risk.max_rejects_10min else None


def cluster_exposure(
    state: RiskState,
    *,
    side: Side,
    notional_usd: float,
    equity: float,
    now_ms: int,
    settings: Settings,
) -> str | None:
    """Gross notional entered on one side inside `cluster_window_s`, capped.

    **The gate Phase 3 asked for by name.** A market-wide sweep liquidates dozens of coins
    inside a minute; the strategy sees dozens of independent cascades and takes all of
    them, and every other cap agrees, because forty coins at 2% of equity each is inside
    both the per-coin cap and the total. It is one bet. Q1's pooled +84.6 bps per episode is
    +26.7 in the isolated-coin bucket, and the difference is exactly this: cluster upside
    that would be sized as if it were fifty independent trades.

    The window is one sided. Fading forty long liquidations means forty longs, all of which
    lose together if the sweep keeps going; a short entered in the same minute is a
    different bet and does not count against it.
    """
    risk = settings.risk
    if equity <= 0:
        return "cluster_exposure"
    cap = risk.max_cluster_exposure_frac * equity
    taken = state.cluster_exposure(side, now_ms, risk.cluster_window_s)
    return "cluster_exposure" if taken + notional_usd > cap * (1.0 + _CAP_EPSILON) else None
