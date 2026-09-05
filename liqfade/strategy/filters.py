"""Pre-trade skip conditions, from `docs/STRATEGY_AND_RISK.md` section 3.

Each filter is a pure function returning a reason string when it blocks and None when it
does not. The engine runs them in order and logs whichever one killed an intent, so the
backtest report can say what each filter cost rather than only that something was skipped.

Two of the five are switched off by Phase 3 and still live here as code:
`skip_when_btc_cascading` (Q6: 1.25x concentration against a 2x bar, and the market-wide
bucket is where the pooled edge lives) and `funding_extreme_bps_8h` (Q6: removes 0.6% of
the sample and 0.0% of the loss tail, so it is null and the gate does not run). Deleting
them would mean re-arguing the decision from memory the next time someone wonders.

**A null feature never blocks a trade here.** The historical year has no book at all, so
`spread_bps` is null across almost the whole backtest window; a filter that read null as
"infinitely wide" would refuse every trade in the sample the strategy was measured on. The
cost model handles the unseen spread instead, by subtracting an estimated effective spread
from Hyperliquid's own impact prices. Absence of evidence is not a reason to skip; it is a
reason the cost is estimated rather than observed.
"""

from __future__ import annotations

from liqfade.config.models import Settings
from liqfade.strategy.types import AssetMeta, FeatureRow, LiqSide, Side

__all__ = [
    "check_filters",
    "coin_not_tradeable",
    "edge_too_small",
    "funding_against_us",
    "market_wide_sweep",
    "spread_too_wide",
]

HOURS_PER_FUNDING_PERIOD = 8.0


def check_filters(
    row: FeatureRow,
    *,
    side: Side,
    edge_frac: float,
    meta: AssetMeta,
    settings: Settings,
    btc_cascade_side: LiqSide | None,
) -> str | None:
    """Run every filter and return the first reason to skip, or None to proceed.

    Args:
        row: the coin's features at the decision tick.
        side: the position the intent would take.
        edge_frac: `abs(target - entry) / entry`, the expected move to the target.
        meta: the coin's venue and universe metadata.
        settings: resolved config.
        btc_cascade_side: which side BTC is currently cascading on, or None. The engine
            knows this because it holds every coin's detector; a filter cannot find out
            for itself, which is why it arrives as an argument.

    Returns:
        A short reason such as `spread_too_wide` or `edge_too_small`, or None.
    """
    checks = (
        coin_not_tradeable(meta, settings),
        spread_too_wide(row, meta, settings),
        edge_too_small(edge_frac, settings),
        market_wide_sweep(meta, side, btc_cascade_side, settings),
        funding_against_us(row, side, settings),
    )
    for reason in checks:
        if reason is not None:
            return reason
    return None


def coin_not_tradeable(meta: AssetMeta, settings: Settings) -> str | None:
    """Excluded by name, or listed too recently to have a stable volatility baseline.

    An unknown listing age counts as too young. A coin the universe cannot date is one we
    have no history for, and the whole point of the age floor is that the z-scores and
    volatility this strategy triggers on need history to be meaningful.
    """
    universe = settings.universe
    if meta.coin in universe.exclude:
        return "coin_excluded"
    if meta.listing_days is None or meta.listing_days < universe.min_listing_days:
        return "coin_too_new"
    return None


def spread_too_wide(row: FeatureRow, meta: AssetMeta, settings: Settings) -> str | None:
    """The quote has stopped looking like a liquid perp's.

    A sanity guard, not a signal filter: Q6 measured the old tiered caps removing 51.9% of
    the sample to remove 36.3% of the loss tail, so they only shrank the book. The flat 30
    bps is the core tier's own p99 effective spread rounded up. Null spread passes, per the
    module docstring.
    """
    if row.spread_bps is None:
        return None
    cap = getattr(settings.filters.max_spread_bps, meta.tier)
    return "spread_too_wide" if row.spread_bps > cap else None


def edge_too_small(edge_frac: float, settings: Settings) -> str | None:
    """The move to the target does not clear a multiple of the round trip fee.

    The round trip is priced as taker in and maker out, which is how the entry and exit
    policy actually works: entries rest as `Alo` and only high urgency falls back to
    taking, while the target exit is a resting order. Using two taker legs would be a
    stricter gate than the strategy incurs and would drop trades that pay.
    """
    fees = settings.backtest.taker_fee_frac + settings.backtest.maker_fee_frac
    return "edge_too_small" if edge_frac < settings.filters.min_edge_multiple * fees else None


def market_wide_sweep(
    meta: AssetMeta, side: Side, btc_cascade_side: LiqSide | None, settings: Settings
) -> str | None:
    """Skip an alt while BTC is cascading the same way. Off by default (Q6).

    The per-cluster exposure cap in `risk/` is what handles sweeps instead: it caps the
    total taken across a sweep rather than refusing to take any of it, which keeps the
    upside that the market-wide bucket carries (+160.4 bps against +26.7 isolated).
    """
    if not settings.filters.skip_when_btc_cascading:
        return None
    if meta.coin == "BTC" or btc_cascade_side is None:
        return None
    # Fading a long liquidation means going long; BTC cascading on the same liquidated
    # side is the same direction of market-wide forced flow.
    same_direction = (btc_cascade_side == "long") == (side == "long")
    return "btc_cascading" if same_direction else None


def funding_against_us(row: FeatureRow, side: Side, settings: Settings) -> str | None:
    """Funding is extreme against the direction we would hold. Off by default (Q6).

    `filters.funding_extreme_bps_8h` is null, which means the gate does not run at all
    rather than running at a threshold set so high it can never fire. `row.funding` is
    Hyperliquid's hourly rate as a fraction, so it is scaled to bps per 8h to compare.
    """
    threshold = settings.filters.funding_extreme_bps_8h
    if threshold is None or row.funding is None:
        return None
    bps_8h = row.funding * HOURS_PER_FUNDING_PERIOD * 1e4
    # A long pays funding when it is positive, a short when it is negative.
    against = bps_8h if side == "long" else -bps_8h
    return "funding_extreme" if against > threshold else None
