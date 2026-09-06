"""The value types the strategy layer passes around.

Frozen dataclasses, not pydantic models: this is the hot path. The engine builds one
`FeatureRow` per coin per 250ms tick, and validating each of them would cost more than
computing them. Config is where pydantic belongs; a tick is not.

The important type is `FeatureRow`, and the important thing about it is that its fields
are named by **role** rather than by window. The spec is written in terms of 15s, 60s and
300s, but every one of those is a config key (`cascade.window_s`,
`features.episodes.peak_window_s`, the rv window behind `intent.k_stop`), and a dataclass
with `liq_long_usd_60s` on it would silently mean something else the day one of them
changed. The caller resolves the windows and says which value is which; `explain()` puts
the real window back into its keys so a Telegram alert still reads in seconds.

That indirection is also what lets the backtest and the live loop share this code with no
branch between them. The backtest fills a `FeatureRow` from a row of a precomputed polars
grid; Phase 5's live loop will fill the same fields from ring buffers. Neither is visible
from here, which is the constraint CLAUDE.md calls the most important one in the project.

`None` in a float field means **absent**, never zero. No book was recorded, the trailing
distribution is not full yet, the coin has not printed. Every rule that reads a nullable
field has to decide what absence means for it, and the answer is never "treat it as zero":
a null depth read as no liquidity blocks a trade for a reason that does not exist, and read
as infinite liquidity sizes into a book nobody can see.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "AssetMeta",
    "CascadeState",
    "FeatureRow",
    "Intent",
    "LiqSide",
    "Side",
    "Tier",
    "Urgency",
    "cascade_direction",
    "fade_side",
]

# Which side of the book was forcibly closed. `long` means longs were liquidated, which is
# forced selling, so price falls into it. This is the lake's own spelling in the
# `liquidations` table and it is deliberately not the same vocabulary as `Side`.
LiqSide = Literal["long", "short"]

# The position the strategy takes. Fading a long liquidation means buying into forced
# selling, so `fade_side("long") == "long"`; the two words coinciding is a coincidence of
# English and the reason both types exist rather than one shared string.
Side = Literal["long", "short"]

Urgency = Literal["normal", "high"]
Tier = Literal["core", "major", "minor"]


def cascade_direction(liq_side: LiqSide) -> float:
    """The sign price moves in while `liq_side` is being liquidated.

    Longs liquidated is forced selling, so -1. Shorts liquidated is forced buying, so +1.
    One expression in one place: getting this sign wrong flatters or ruins every number
    downstream, and `scripts/research/_lib.py` keeps its own copy for the same reason.
    """
    return -1.0 if liq_side == "long" else 1.0


def fade_side(liq_side: LiqSide) -> Side:
    """The position that fades a cascade of `liq_side`. Opposite to the price move."""
    return "long" if liq_side == "long" else "short"


@dataclass(frozen=True, slots=True)
class AssetMeta:
    """What the venue and the universe say about one coin, at decision time.

    Args:
        coin: the Hyperliquid symbol.
        sz_decimals: `meta.universe[i].szDecimals`, for `execution/rounding.py`.
        max_leverage: `meta.universe[i].maxLeverage`. Sizing clamps to this times equity.
        tier: which `filters.max_spread_bps` entry applies.
        listing_days: days since the coin listed, for the minimum-age filter. None when
            the universe cannot say, which the filter treats as too young to trade.
    """

    coin: str
    sz_decimals: int
    max_leverage: float
    tier: Tier = "minor"
    listing_days: float | None = None


@dataclass(frozen=True, slots=True)
class FeatureRow:
    """Every feature the strategy reads, for one coin at one decision tick.

    Window-bearing fields are named by role. `_w` is the cascade window
    (`cascade.window_s`, 60s by default), `_peak` is the exhaustion peak window
    (`features.episodes.peak_window_s`, 15s), and `rv` is the realized volatility that
    `intent.k_stop` is measured in (300s). `window_s`, `peak_window_s` and `rv_window_s`
    carry the resolved lengths so `explain()` can name them.

    Args:
        coin: the Hyperliquid symbol.
        ts_ms: the decision timestamp. Every value here was knowable strictly before it.
        mid: last mid, or None before the coin's first print.
        liq_long_usd_w: liquidated long notional in the cascade window, USD.
        liq_short_usd_w: liquidated short notional in the cascade window, USD.
        liq_imbalance_w: `(long - short) / (long + short)` in `[-1, 1]`, 0 when quiet.
        liq_z_long_w: robust z of `log1p(liq_long_usd_w)` against its trailing 24h. None
            until the lookback fills, which is not the same as "not unusual".
        liq_z_short_w: the same for the short side.
        proxy_long_w: `hl_forced_proxy` for forced selling, a multiple of the trailing
            median and already gated. Only read when `cascade.trigger` is `proxy`.
        proxy_short_w: the same for forced buying.
        liq_long_usd_peak: liquidated long notional in the peak window, USD.
        liq_short_usd_peak: the same for shorts.
        ret_w: log return of mid over the cascade window.
        rv: realized volatility over the rv window, a fraction of price, so `rv * mid` is
            a price distance and is the unit `intent.k_stop` is in.
        cvd_slope: signed USD per second of aggressor flow, buy positive.
        spread_bps: quoted spread. None for the whole historical year, which has no book.
        depth_bid: resting USD within `features.depth_bps` of the touch on the bid.
        depth_ask: the same on the ask.
        book_imbalance: `(bid - ask) / (bid + ask)` in `[-1, 1]`.
        open_interest: the venue's own open interest in contracts, both sides of the book,
            as Hyperliquid publishes it. None where the coin has no context sample yet.
        oi_delta_w: fractional change in `open_interest` over the cascade window, so -0.02
            is two percent of the open interest closed. None until the window is full.
        oi_delta_rv: the same over the rv window (300s), which is the window `oi_flush`
            asks whether positions were flushed over.
        oi_delta_build: the same over the longest configured OI window (3600s), which is
            the crowding that was there to be flushed. It is measured to *now*, so it is
            net of the flush: a build of +0.05 after a -0.02 flush means open interest is
            still five percent above where it was an hour ago.
        disp_anchor_bps: mid against the VWAP of the window before the last one, in bps,
            signed. `features.vwap_anchor_s` both ways, so at the defaults it is the 300s
            of tape that ended 300s ago. None when nothing traded in that window.
        disp_day_bps: mid against the session VWAP accumulated from 00:00 UTC, in bps,
            signed. None before the day's first trade.
        mark_mid_bps: `abs(mark - mid) / mid` in bps.
        mark_mid_threshold: the trailing percentile `mark_mid_bps` is judged against. Which
            percentile depends on the caller: the exhaustion rule wants
            `exhaustion.mark_mid_pct_threshold`, the proxy gate wants the proxy's own.
        funding: the venue's hourly funding rate as a fraction.
        traded_usd_vol_window: HL notional traded over `sizing.vol_window_s`, for the
            volume participation cap.
        support_px: price of the nearest pre-existing support level, or None when there is
            none within `features.levels.max_dist_bps`. A price rather than a distance,
            because the `zone_proximity` rule measures from the cascade extreme and not
            from this row's mid, and a distance computed here would answer the wrong
            question by a whole cascade's width.
        resistance_px: the same above.
        window_s: the resolved cascade window in seconds.
        peak_window_s: the resolved peak window in seconds.
        rv_window_s: the resolved rv window in seconds.
        oi_build_window_s: the resolved window behind `oi_delta_build`, in seconds.
        vwap_anchor_s: the resolved window behind `disp_anchor_bps`, in seconds.
    """

    coin: str
    ts_ms: int
    mid: float | None = None

    liq_long_usd_w: float = 0.0
    liq_short_usd_w: float = 0.0
    liq_imbalance_w: float = 0.0
    liq_z_long_w: float | None = None
    liq_z_short_w: float | None = None
    proxy_long_w: float | None = None
    proxy_short_w: float | None = None
    liq_long_usd_peak: float = 0.0
    liq_short_usd_peak: float = 0.0

    ret_w: float | None = None
    rv: float | None = None
    cvd_slope: float | None = None

    spread_bps: float | None = None
    depth_bid: float | None = None
    depth_ask: float | None = None
    book_imbalance: float | None = None

    open_interest: float | None = None
    oi_delta_w: float | None = None
    oi_delta_rv: float | None = None
    oi_delta_build: float | None = None

    disp_anchor_bps: float | None = None
    disp_day_bps: float | None = None

    mark_mid_bps: float | None = None
    mark_mid_threshold: float | None = None
    funding: float | None = None
    traded_usd_vol_window: float | None = None

    support_px: float | None = None
    resistance_px: float | None = None

    window_s: int = 60
    peak_window_s: int = 15
    rv_window_s: int = 300
    oi_build_window_s: int = 3600
    vwap_anchor_s: int = 300

    def liq_usd_w(self, liq_side: LiqSide) -> float:
        """Liquidated notional on one side over the cascade window."""
        return self.liq_long_usd_w if liq_side == "long" else self.liq_short_usd_w

    def liq_usd_peak(self, liq_side: LiqSide) -> float:
        """Liquidated notional on one side over the peak window."""
        return self.liq_long_usd_peak if liq_side == "long" else self.liq_short_usd_peak

    def liq_z(self, liq_side: LiqSide) -> float | None:
        """The z-score of this side's liquidation notional. None means undefined."""
        return self.liq_z_long_w if liq_side == "long" else self.liq_z_short_w

    def proxy(self, liq_side: LiqSide) -> float | None:
        """The forced flow proxy for this side. None means it could not be computed."""
        return self.proxy_long_w if liq_side == "long" else self.proxy_short_w

    def depth(self, side: Side) -> float | None:
        """Resting depth on the side an entry of `side` would take liquidity from.

        Going long lifts the ask, so a long reads `depth_ask`. Getting this backwards
        would size against the depth we are not going to touch.
        """
        return self.depth_ask if side == "long" else self.depth_bid


@dataclass(frozen=True, slots=True)
class CascadeState:
    """One cascade being tracked, per coin per side.

    Args:
        coin: the Hyperliquid symbol.
        side: which side is being liquidated, so `long` means price is falling.
        start_ts: the tick the start conditions first all held.
        anchor_px: mid at `start_ts`. The reversion target is measured back towards this.
        extreme_px: the furthest mid reached in the cascade direction so far.
        extreme_ts: when `extreme_px` was set. Feeds the "no new extreme" exhaustion rule.
        peak_liq_15s: the largest liquidated notional seen in one peak window since the
            start, in USD. Named for the spec's 15s default even though the window is
            `features.episodes.peak_window_s`, because the spec and the reports call it
            that and renaming it here would break the link between them.
        exhaustion_streak: consecutive ticks all exhaustion conditions have held for.
        total_liq_usd: liquidated notional accumulated since the start, USD. What the
            `min_cascade_notional` rule reads.
        peak_liq_z: the highest liquidation z-score reached during the cascade, or None
            where it was never defined. Urgency is read from this and not from the z at
            the entry tick: under `exhaustion.mode: fixed_delay` the entry is two minutes
            after the start, by which time the z has decayed to nothing, and "large
            cascades revert faster" is a claim about the cascade rather than about the
            minute we happened to enter in.
    """

    coin: str
    side: LiqSide
    start_ts: int
    anchor_px: float
    extreme_px: float
    extreme_ts: int
    peak_liq_15s: float = 0.0
    exhaustion_streak: int = 0
    total_liq_usd: float = 0.0
    peak_liq_z: float | None = None

    @property
    def direction(self) -> float:
        """The sign price moves in while this cascade runs. -1 for a long liquidation."""
        return cascade_direction(self.side)

    def age_s(self, now_ms: int) -> float:
        """Seconds since the cascade started."""
        return (now_ms - self.start_ts) / 1000.0

    def move_bps(self) -> float:
        """How far the cascade has pushed price from its anchor, in bps, always positive."""
        if self.anchor_px <= 0:
            return 0.0
        return 1e4 * abs(self.anchor_px - self.extreme_px) / self.anchor_px


@dataclass(frozen=True, slots=True)
class Intent:
    """One decision to enter, with everything the layers below it need.

    Args:
        coin: the Hyperliquid symbol.
        side: the position to take. `long` fades a long liquidation.
        entry_px: the touch we would rest at. The order manager handles repricing.
        target_px: `extreme + k_target * (anchor - extreme)`, the partial reversion exit.
        stop_px: `k_stop * rv * mid` beyond the extreme, never inside it.
        time_stop_s: seconds after which the position is flattened regardless.
        urgency: `high` when the cascade is large. Only high urgency may take liquidity.
        ts_ms: the decision tick. The latency model turns this into an order at
            `ts_ms + backtest.latency_ms`.
        explain: every feature value that crossed a threshold, plus every rule's values.
            This is what Telegram posts and what the trade log keeps, so a trade can be
            argued about a month later without rerunning anything.
    """

    coin: str
    side: Side
    entry_px: float
    target_px: float
    stop_px: float
    time_stop_s: float
    urgency: Urgency
    ts_ms: int
    explain: Mapping[str, float] = field(default_factory=dict)

    @property
    def stop_distance_frac(self) -> float:
        """`abs(entry - stop) / entry`, the denominator of the risk-based notional."""
        if self.entry_px <= 0:
            return 0.0
        return abs(self.entry_px - self.stop_px) / self.entry_px

    @property
    def edge_frac(self) -> float:
        """Expected move to target as a fraction of entry, **signed in the trade's own
        direction**: positive when the target is ahead of the entry, negative when it has
        already been passed.

        It was `abs()`, and that was a defect rather than a convenience. A long's target is
        normally above its entry and a short's below, so the absolute value agreed with the
        signed one on every trade the fixed-delay entry produced. Under
        `exhaustion.mode: rule`, and more so with the `level_reclaim` rule, the entry lands
        after the bounce has started: with `intent.target_from: extreme` the target sits at
        a fixed point on the way back to the anchor and price can already be past it, and
        `abs()` then reported the distance *back* to a target behind us as edge. The year
        run of 2026-09-06 took 31 such trades whose median hold was 1.4 seconds, every one
        of them a loss by construction. `filters.edge_too_small` now blocks a non-positive
        edge outright, with its own reason, so it can never be mistaken for a small one.
        """
        if self.entry_px <= 0:
            return 0.0
        ahead = (
            self.target_px - self.entry_px
            if self.side == "long"
            else self.entry_px - self.target_px
        )
        return ahead / self.entry_px
