"""Intent to notional: one pure function, five caps and two floors.

`size_intent` is the whole surface. It takes an `Intent`, live equity, the coin's
liquidity, its venue metadata and the config, and returns the notional to send. It reads
no state, opens no files and never sees the clock, so the backtest and Phase 5's live loop
size identically by construction.

The five caps, from `docs/STRATEGY_AND_RISK.md` section 4, and the size is the smallest:

| cap                  | value                                                  |
|----------------------|--------------------------------------------------------|
| `risk_budget`        | `risk_frac * equity / stop_distance_frac`               |
| `vol_cap`            | `vol_participation * traded notional over vol_window_s` |
| `depth_cap`          | `depth_participation * resting depth on the side we hit`|
| `coin_exposure`      | `coin_exposure_frac * equity`, less what is already on  |
| `total_exposure`     | `total_exposure_frac * equity`, less gross exposure     |
| `venue_max_leverage` | `maxLeverage * equity` for the asset                    |

Then the size is rounded to the asset's `szDecimals` and the notional recomputed from the
rounded size, because the order carries the size and the notional that gets reported has to
be the one that size actually buys.

**Capital is never hardcoded.** Every number above is a fraction of live equity, and the
only dollar amount in this module is Hyperliquid's own 10 USDC minimum order value. The
owner scales by depositing. Leverage is an output: it is whatever `notional / equity`
comes to, reported and never targeted.

**Size on 30 bps, not 85.** Phase 3's pooled headline of +84.6 bps per episode is +26.7 in
the isolated-coin bucket and +34.0 with the top five clusters removed, and the difference
is cluster upside. A market-wide sweep liquidates dozens of coins inside a minute and the
strategy would be in all of them at once, so it is one bet. What stops that being sized as
fifty is the per-cluster exposure cap in `risk/`, not anything here; this module sizes one
trade at a time and cannot see the others. See `docs/DECISIONS.md`, 2026-09-05.

**A null is not a zero.** The historical year records no book, so `depth_usd` is null over
almost the whole backtest window. Reading that as zero depth would size every trade to
nothing and make the strategy look untradeable for a reason that is about the archive
rather than about the market, so the depth cap is skipped where depth is unknown and
`SizeDecision.depth_cap_applied` says so, for the report to count. Observed zero volume is
different: that is a measurement, and it means no participation budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from liqfade.config.models import Settings
from liqfade.execution.rounding import RoundingError, round_size
from liqfade.strategy.types import AssetMeta, Intent

__all__ = ["LiquidityStats", "SizeDecision", "size_intent"]


@dataclass(frozen=True, slots=True)
class LiquidityStats:
    """What the coin's tape and book say about how much we can take.

    Args:
        traded_usd_window: HL notional traded over `sizing.vol_window_s`, USD. Zero is a
            real measurement of a dead tape and blocks; None means the column was never
            computed, which is a caller bug and is treated the same way.
        depth_usd: resting notional within `features.depth_bps` on the side the entry
            would take liquidity from, USD. None means no book was recorded, and the depth
            cap is then not applied.
    """

    traded_usd_window: float | None
    depth_usd: float | None


@dataclass(frozen=True, slots=True)
class SizeDecision:
    """What to send, and why it is not larger.

    Args:
        notional_usd: USD notional to enter, recomputed from the rounded size. Zero means
            no order.
        size: the size in coin units, rounded to the asset's `szDecimals`.
        leverage_implied: `notional_usd / equity`. Reported, never targeted.
        reason: None when an order should be sent. When the notional is zero, the name of
            the cap or floor that blocked it: `risk_budget`, `vol_cap`, `depth_cap`,
            `coin_exposure`, `total_exposure`, `venue_max_leverage` or `min_notional`.
        depth_cap_applied: whether resting depth was known and the depth cap was therefore
            in force. False across almost the whole historical year, and the backtest
            report prints the share so a reader knows how much of the sizing was measured
            against a book and how much was not.
    """

    notional_usd: float
    size: Decimal
    leverage_implied: float
    reason: str | None
    depth_cap_applied: bool


def size_intent(
    intent: Intent,
    *,
    equity: float,
    stats: LiquidityStats,
    meta: AssetMeta,
    settings: Settings,
    coin_exposure_usd: float = 0.0,
    total_exposure_usd: float = 0.0,
) -> SizeDecision:
    """Size one intent. Pure: same inputs, same decision, every time.

    Args:
        intent: the entry, with the stop that sets the risk denominator.
        equity: live account equity in USD. Never hardcoded anywhere; it arrives here.
        stats: the coin's traded volume and resting depth.
        meta: the coin's `szDecimals` and `maxLeverage`.
        settings: resolved config.
        coin_exposure_usd: gross notional already open in this coin.
        total_exposure_usd: gross notional already open across every coin.

    Returns:
        A `SizeDecision`. `notional_usd` is zero exactly when `reason` is set.
    """
    sizing = settings.sizing
    caps: list[tuple[str, float]] = []

    stop_frac = intent.stop_distance_frac
    if equity <= 0 or stop_frac <= 0:
        # A zero-width stop would divide the risk budget by nothing and size the whole
        # account into one trade. There is no sensible clamp for that; there is no trade.
        return _blocked("risk_budget")
    caps.append(("risk_budget", sizing.risk_frac * equity / stop_frac))

    traded = stats.traded_usd_window or 0.0
    caps.append(("vol_cap", sizing.vol_participation * traded))

    depth_known = stats.depth_usd is not None
    if depth_known:
        caps.append(("depth_cap", sizing.depth_participation * float(stats.depth_usd or 0.0)))

    caps.append(("coin_exposure", sizing.coin_exposure_frac * equity - coin_exposure_usd))
    caps.append(("total_exposure", sizing.total_exposure_frac * equity - total_exposure_usd))
    if sizing.respect_venue_max_leverage:
        caps.append(("venue_max_leverage", meta.max_leverage * equity))

    binding, notional = min(caps, key=lambda pair: pair[1])
    if notional <= 0:
        return _blocked(binding, depth_cap_applied=depth_known)

    try:
        size = round_size(notional / intent.entry_px, meta.sz_decimals)
    except RoundingError:
        # An entry price that cannot produce a size is not a tradeable intent. The strategy
        # already refuses a non-positive entry, so reaching here means something upstream
        # changed and the safe answer is no order rather than a guessed one.
        return _blocked("min_notional", depth_cap_applied=depth_known)

    # The order carries the size, so the notional that gets reported is the one the rounded
    # size buys. Reporting the pre-rounding number would put a small permanent error into
    # every PnL number downstream.
    rounded_notional = float(size) * intent.entry_px
    if rounded_notional < settings.filters.min_notional_usd:
        return _blocked("min_notional", depth_cap_applied=depth_known)

    return SizeDecision(
        notional_usd=rounded_notional,
        size=size,
        leverage_implied=rounded_notional / equity,
        reason=None,
        depth_cap_applied=depth_known,
    )


def _blocked(reason: str, *, depth_cap_applied: bool = False) -> SizeDecision:
    return SizeDecision(
        notional_usd=0.0,
        size=Decimal(0),
        leverage_implied=0.0,
        reason=reason,
        depth_cap_applied=depth_cap_applied,
    )
