"""The online half of the feature pipeline: the same numbers, computed incrementally.

The backtest computes features by building a polars grid over a whole window and rolling
over it. That is right for a year of tape and impossible at 4 Hz on a live feed: the
z-score alone wants a 24 hour lookback, and rebuilding an 86,400 row grid per coin every
second would cost more than the strategy it feeds.

So this module recomputes the same definitions incrementally, and `tests/test_live_features.py`
feeds one synthetic tape through both pipelines and compares every field the enabled
config path reads. That test is the contract. CLAUDE.md's single most important design
constraint is that strategy code runs against a replay unchanged, and it only holds if the
rows the strategy sees live are the rows a backtest would have handed it.

Everything follows `features/grid.py`'s half-open convention. A bucket stamped `T`
summarises events whose receive time falls in `[T - step, T)`, so a value read at `T` was
knowable strictly before `T` and the no-lookahead rule is inherited rather than restated.
The bucket is `backtest.feature_grid_ms` (1s), the decision cadence is
`features.decision_cadence_ms` (250ms), and the strategy therefore sees the same row up to
four times, exactly as it does in a backtest.

**What this engine does not compute.** Open interest deltas, VWAP displacement, the
mark-to-mid percentile, the forced-flow proxy and the support and resistance levels are
left `None`. Every one of them feeds a confluence rule or a trigger that ships disabled,
and each needs either a rolling quantile or a level detector that is a piece of work in
its own right. Leaving them `None` is only safe because it is checked: `unsupported()`
lists what a given config asks for and cannot get, and the constructor refuses rather than
letting a rule silently veto every intent for want of a number nobody computed.
"""

from __future__ import annotations

import itertools
import math
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from liqfade.backtest.features import LIVE_VENUES, FeatureWindows, trigger_venues
from liqfade.config.models import Settings
from liqfade.log import get_logger
from liqfade.strategy.types import FeatureRow

log = get_logger(__name__)

__all__ = ["LiveFeatureEngine"]

MINUTE_MS = 60_000
_MAD_TO_SIGMA = 1.4826  # the same constant features/zscore.py uses, and for the same reason
_EPS = 1e-9  # guards the imbalance ratio, as features/intensity.py does

# Rules whose features this engine does not compute, and the field each would read. The
# constructor refuses a config that enables one, because a rule reading None either vetoes
# every intent or passes every one, and both look like the strategy working.
UNSUPPORTED_RULES = {
    "oi_flush": "open interest deltas",
    "vwap_stretch": "VWAP displacement",
    "zone_proximity": "support and resistance levels",
    "level_reclaim": "support and resistance levels",
}


@dataclass(slots=True)
class _Bucket:
    """What accumulated in one grid step, before it is closed."""

    notional: float = 0.0
    signed_notional: float = 0.0
    last_trade_px: float | None = None
    last_bbo_mid: float | None = None
    book: tuple[float, float, float, float] | None = None
    ctx: tuple[float | None, float | None] = (None, None)


@dataclass(slots=True)
class _Closed:
    """One closed grid step. The only per-second state anything downstream reads."""

    ts_ms: int
    mid: float | None
    notional: float
    signed_notional: float


@dataclass(slots=True)
class _Coin:
    """Everything held for one coin. Every container here is bounded."""

    seconds: deque[_Closed]
    liqs: deque[tuple[int, str, float]] = field(default_factory=deque)
    pending: dict[int, _Bucket] = field(default_factory=dict)
    last_bbo_mid: float | None = None
    last_trade_px: float | None = None
    book: tuple[float, float, float, float] | None = None
    ctx: tuple[float | None, float | None] = (None, None)
    closed_to: int = 0
    # The z-score lookback: one sample a minute of log1p(liq_usd_w) per side, and the
    # deviation series the MAD is taken over. Both hold 24h and no more.
    samples: dict[str, deque[tuple[int, float]]] = field(default_factory=dict)
    deviations: dict[str, deque[tuple[int, float]]] = field(default_factory=dict)
    # The last two scales per side, newest last. Two, because a row stamped exactly on a
    # minute is scored against the sample *before* it, so the scale computed at that
    # minute is not yet in force and the one before it still is.
    scale: dict[str, deque[tuple[int, float | None, float | None]]] = field(default_factory=dict)


class LiveFeatureEngine:
    """Ring buffers in, `FeatureRow` out. One per process, shared by every coin."""

    def __init__(self, settings: Settings) -> None:
        """
        Args:
            settings: resolved config. Every window comes from `FeatureWindows`, so the
                live engine and the backtest read the same keys.

        Raises:
            ValueError: the config enables a rule this engine cannot supply a feature for.
        """
        unsupported = self.unsupported(settings)
        if unsupported:
            raise ValueError(
                "the live feature engine cannot supply: "
                + "; ".join(unsupported)
                + ". Disable the rule, or compute the feature here first."
            )
        self.settings = settings
        self.windows = FeatureWindows(settings)
        self.step_ms = self.windows.step_ms
        self.venues = set(trigger_venues(settings))
        self.weights = {
            venue: float(getattr(settings.venues, venue).liq_weight)
            for venue in (*LIVE_VENUES, "hyperliquid")
            if hasattr(settings.venues, venue)
        }
        self.lookback_ms = int(settings.features.z_lookback_h * 3_600_000)
        self.robust = bool(settings.features.z_robust)
        self.depth_bps = float(settings.features.depth_bps)
        # The longest window any second-level feature reaches back over, plus a step of
        # slack so a rolling sum that needs n samples has n.
        self.seconds_kept = (
            max(self.windows.rv_s, self.windows.vol_s, self.windows.cascade_s)
            * 1000
            // self.step_ms
        ) + 2
        self.coins: dict[str, _Coin] = {}

    @staticmethod
    def unsupported(settings: Settings) -> list[str]:
        """What this config asks for that the live engine cannot compute. Empty is good."""
        reasons = []
        for name, feature in sorted(UNSUPPORTED_RULES.items()):
            if getattr(settings.strategy.rules, name).enabled:
                reasons.append(f"strategy.rules.{name} needs {feature}")
        if settings.cascade.trigger == "proxy":
            reasons.append("cascade.trigger 'proxy' needs hl_forced_proxy")
        if settings.cascade.trigger == "exact":
            # Not a missing feature but a missing feed: Hyperliquid's own liquidations
            # reach us through a next-day S3 pull. A live loop on this trigger runs with
            # the intensity columns empty forever and looks healthy doing it.
            reasons.append(
                "cascade.trigger 'exact' reads hl_s3 liquidations, which no live feed carries"
            )
        if settings.exhaustion.mode == "rule":
            reasons.append("exhaustion.mode 'rule' needs the mark-to-mid trailing percentile")
        if settings.filters.funding_extreme_bps_8h is not None:
            reasons.append("filters.funding_extreme_bps_8h needs cross-venue funding")
        return reasons

    # --- ingestion ------------------------------------------------------------

    def _coin(self, coin: str) -> _Coin:
        state = self.coins.get(coin)
        if state is None:
            state = _Coin(seconds=deque(maxlen=self.seconds_kept))
            self.coins[coin] = state
        return state

    def ingest(self, table: str, venue: str, row: Mapping[str, Any]) -> None:
        """Take one parsed row from any stream. Never raises: this runs in a receive loop.

        `rx_ms` is the time the row reached us and is what everything is bucketed on, per
        the no-lookahead rule in CLAUDE.md. A row with neither timestamp is dropped and
        counted by the caller's stream stats; inventing one would date it to 1970.
        """
        try:
            coin = row.get("coin")
            if not coin:
                return
            known = int(row.get("rx_ms") or row.get("ts_ms") or 0)
            if known <= 0:
                return
            state = self._coin(str(coin))
            bucket_ts = (known // self.step_ms) * self.step_ms + self.step_ms
            if bucket_ts <= state.closed_to:
                # It arrived after its own bucket closed. Counting it in a later bucket
                # would date it forward; dropping it is what the backtest's asof join does
                # to a row whose receive time has already passed.
                return
            bucket = state.pending.get(bucket_ts)
            if bucket is None:
                bucket = state.pending[bucket_ts] = _Bucket()

            if table == "trades":
                px, size = float(row["px"]), float(row["sz"])
                bucket.last_trade_px = px
                bucket.notional += px * size
                bucket.signed_notional += px * size * (1.0 if row.get("side") == "buy" else -1.0)
            elif table == "bbo":
                bucket.last_bbo_mid = (float(row["bid_px"]) + float(row["ask_px"])) / 2.0
            elif table == "l2":
                self._ingest_level(state, bucket_ts, row)
            elif table == "liquidations":
                # A liquidation with no notional cannot be weighed or summed. OKX sends
                # them for contracts it has no price for, roughly one an hour, and
                # counting one as zero is the same as dropping it while looking like data.
                notional = _maybe_float(row.get("notional_usd"))
                if venue in self.venues and notional is not None:
                    state.liqs.append(
                        (known, str(row["side"]), notional * self.weights.get(venue, 1.0))
                    )
            elif table == "asset_ctx":
                bucket.ctx = (
                    _maybe_float(row.get("open_interest")),
                    _maybe_float(row.get("funding")),
                )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("feature_ingest_failed", table=table, venue=venue, error=str(exc))

    def _ingest_level(self, state: _Coin, bucket_ts: int, row: Mapping[str, Any]) -> None:
        """Accumulate one L2 level into the bucket's book snapshot.

        Levels of one snapshot arrive as twenty rows. They are folded in as they come, and
        the depth band is applied against the touch seen so far, which is level zero and
        therefore always the first row of a snapshot.
        """
        bucket = state.pending[bucket_ts]
        bid_px, bid_sz = float(row["bid_px"]), float(row["bid_sz"])
        ask_px, ask_sz = float(row["ask_px"]), float(row["ask_sz"])
        if int(row.get("level", 0)) == 0:
            bucket.book = (bid_px, ask_px, 0.0, 0.0)
        if bucket.book is None:
            return
        best_bid, best_ask, depth_bid, depth_ask = bucket.book
        mid = (best_bid + best_ask) / 2.0
        band = self.depth_bps / 1e4
        if bid_px >= mid * (1 - band):
            depth_bid += bid_px * bid_sz
        if ask_px <= mid * (1 + band):
            depth_ask += ask_px * ask_sz
        bucket.book = (best_bid, best_ask, depth_bid, depth_ask)

    # --- the clock ------------------------------------------------------------

    def advance(self, now_ms: int) -> None:
        """Close every bucket whose window has ended at or before `now_ms`."""
        for coin in list(self.coins):
            self._advance_coin(self.coins[coin], now_ms)

    def _advance_coin(self, state: _Coin, now_ms: int) -> None:
        start = state.closed_to + self.step_ms if state.closed_to else self._first_bucket(state)
        if start is None:
            return
        for stamp in range(start, now_ms + 1, self.step_ms):
            self._close(state, stamp)
            state.closed_to = stamp

    def _first_bucket(self, state: _Coin) -> int | None:
        if not state.pending:
            return None
        return min(state.pending)

    def _close(self, state: _Coin, stamp: int) -> None:
        bucket = state.pending.pop(stamp, None) or _Bucket()
        if bucket.last_bbo_mid is not None:
            state.last_bbo_mid = bucket.last_bbo_mid
        if bucket.last_trade_px is not None:
            state.last_trade_px = bucket.last_trade_px
        if bucket.book is not None:
            state.book = bucket.book
        if bucket.ctx != (None, None):
            state.ctx = bucket.ctx
        # The quote wins over the tape however old it is, which is what build_grid's
        # coalesce of two backward asof joins does.
        mid = state.last_bbo_mid if state.last_bbo_mid is not None else state.last_trade_px
        state.seconds.append(
            _Closed(
                ts_ms=stamp,
                mid=mid,
                notional=bucket.notional,
                signed_notional=bucket.signed_notional,
            )
        )
        self._drop_old_liquidations(state, stamp)
        if stamp % MINUTE_MS == 0:
            self._sample_for_z(state, stamp)

    def _drop_old_liquidations(self, state: _Coin, stamp: int) -> None:
        horizon = stamp - self.windows.cascade_s * 1000
        while state.liqs and state.liqs[0][0] < horizon:
            state.liqs.popleft()

    # --- the z-score lookback -------------------------------------------------

    def _sample_for_z(self, state: _Coin, stamp: int) -> None:
        """Take one minute sample per side and roll the median and spread forward.

        `features/zscore.py` scores a value at t against the distribution of minute
        samples strictly before t, with the scale itself computed from samples strictly
        before the sample it is attached to. Both exclusions are why the deque is read
        before the new sample is appended.
        """
        for side in ("long", "short"):
            value = math.log1p(self._liq_window(state, stamp, side, self.windows.cascade_s))
            samples = state.samples.setdefault(side, deque())
            deviations = state.deviations.setdefault(side, deque())
            horizon = stamp - self.lookback_ms
            _trim(samples, horizon)
            _trim(deviations, horizon)

            centre = _centre([v for _, v in samples], robust=self.robust)
            spread = None
            if centre is not None:
                spread = _spread(
                    [d for _, d in deviations],
                    values=[v for _, v in samples],
                    centre=centre,
                    robust=self.robust,
                )
                if spread is not None and spread <= 0:
                    spread = None
            state.scale.setdefault(side, deque(maxlen=2)).append((stamp, centre, spread))
            samples.append((stamp, value))
            if centre is not None:
                # No deviation at all for the first sample of a coin's life. Its own
                # rolling median is undefined, and `features/zscore.py` gets a null there
                # that the rolling mean behind the fallback scale skips rather than
                # counts. Appending a zero instead would divide by one too many.
                deviations.append((stamp, abs(value - centre)))

    # --- reading --------------------------------------------------------------

    def _liq_window(self, state: _Coin, stamp: int, side: str, window_s: int) -> float:
        low = stamp - window_s * 1000
        return sum(
            usd for when, liq_side, usd in state.liqs if liq_side == side and low <= when < stamp
        )

    def _sum_notional(self, state: _Coin, stamp: int, window_s: int) -> float:
        low = stamp - window_s * 1000
        return sum(second.notional for second in state.seconds if low < second.ts_ms <= stamp)

    def buffer_rows(self, coin: str) -> int:
        """Total buffered rows for one coin. A bound the long-run test asserts against."""
        state = self.coins.get(coin)
        if state is None:
            return 0
        return (
            len(state.seconds)
            + len(state.liqs)
            + len(state.pending)
            + sum(len(d) for d in state.samples.values())
            + sum(len(d) for d in state.deviations.values())
        )

    def row(self, coin: str, ts_ms: int) -> FeatureRow:
        """The feature row for `coin` as of the last closed bucket at or before `ts_ms`.

        A decision at `t` reads the newest closed bucket, exactly as `iter_ticks` walks a
        grid: at the 250ms cadence three ticks out of four see the row they saw before.
        """
        state = self.coins.get(coin)
        if state is None or not state.seconds:
            return FeatureRow(coin=coin, ts_ms=ts_ms, **self._window_fields())
        stamp = state.seconds[-1].ts_ms

        long_w = self._liq_window(state, stamp, "long", self.windows.cascade_s)
        short_w = self._liq_window(state, stamp, "short", self.windows.cascade_s)
        total = long_w + short_w
        book = state.book
        spread_bps = depth_bid = depth_ask = imbalance = None
        if book is not None:
            best_bid, best_ask, depth_bid, depth_ask = book
            book_mid = (best_bid + best_ask) / 2.0
            spread_bps = 1e4 * (best_ask - best_bid) / book_mid if book_mid else None
            denominator = depth_bid + depth_ask
            imbalance = (depth_bid - depth_ask) / denominator if denominator else None

        return FeatureRow(
            coin=coin,
            ts_ms=ts_ms,
            mid=state.seconds[-1].mid,
            liq_long_usd_w=long_w,
            liq_short_usd_w=short_w,
            liq_imbalance_w=(long_w - short_w) / (total + _EPS) if total else 0.0,
            liq_z_long_w=self._z(state, "long", long_w, stamp),
            liq_z_short_w=self._z(state, "short", short_w, stamp),
            liq_long_usd_peak=self._liq_window(state, stamp, "long", self.windows.peak_s),
            liq_short_usd_peak=self._liq_window(state, stamp, "short", self.windows.peak_s),
            ret_w=self._ret(state, self.windows.cascade_s),
            rv=self._rv(state, self.windows.rv_s),
            cvd_slope=self._cvd_slope(state, stamp, self.windows.cvd_s),
            spread_bps=spread_bps,
            depth_bid=depth_bid,
            depth_ask=depth_ask,
            book_imbalance=imbalance,
            open_interest=state.ctx[0],
            funding=state.ctx[1],
            traded_usd_vol_window=self._sum_notional(state, stamp, self.windows.vol_s),
            **self._window_fields(),
        )

    def _window_fields(self) -> dict[str, int]:
        return {
            "window_s": self.windows.cascade_s,
            "peak_window_s": self.windows.peak_s,
            "rv_window_s": self.windows.rv_s,
            "oi_build_window_s": self.windows.build_s,
            "vwap_anchor_s": self.windows.anchor_s,
        }

    def _z(self, state: _Coin, side: str, value: float, stamp: int) -> float | None:
        """The z of this side's window, or None when no scale is in force yet.

        None means undefined, not "not unusual". The scale in force at `stamp` is the one
        attached to the last minute sample strictly before it.
        """
        for sampled_at, centre, spread in reversed(state.scale.get(side, ())):
            if sampled_at >= stamp:
                continue
            if centre is None or spread is None:
                return None
            return (math.log1p(value) - centre) / spread
        return None

    def _ret(self, state: _Coin, window_s: int) -> float | None:
        span = window_s * 1000 // self.step_ms
        if len(state.seconds) <= span:
            return None
        now, then = state.seconds[-1].mid, state.seconds[-1 - span].mid
        if not now or not then:
            return None
        return math.log(now / then)

    def _rv(self, state: _Coin, window_s: int) -> float | None:
        """Root of the summed squared step returns. None until the window is full."""
        span = window_s * 1000 // self.step_ms
        if len(state.seconds) < span + 1:
            return None
        total = 0.0
        window = list(state.seconds)[-(span + 1) :]
        for previous, current in itertools.pairwise(window):
            if not previous.mid or not current.mid:
                return None
            total += math.log(current.mid / previous.mid) ** 2
        return math.sqrt(total)

    def _cvd_slope(self, state: _Coin, stamp: int, window_s: int) -> float:
        """Signed USD per second of aggressor flow over the slope window, buy positive."""
        low = stamp - window_s * 1000
        total = sum(
            second.signed_notional for second in state.seconds if low < second.ts_ms <= stamp
        )
        return total / window_s


def _maybe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _trim(series: deque[tuple[int, float]], horizon: int) -> None:
    while series and series[0][0] < horizon:
        series.popleft()


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _centre(values: list[float], *, robust: bool) -> float | None:
    if not values:
        return None
    if robust:
        return _median(values)
    return sum(values) / len(values)


def _spread(
    deviations: list[float], *, values: list[float], centre: float, robust: bool
) -> float | None:
    """The scale the z divides by, with the same fallback `features/zscore.py` uses.

    Under `robust`, the MAD scaled to sigma, and where that is zero (which it is on any
    series that is zero most of the time) the mean absolute deviation about the same
    median, which a handful of nonzero samples makes positive. A lookback that is all
    zeros still has no scale at all and scores None.
    """
    if robust:
        if not deviations:
            return None
        mad = _median(deviations)
        if mad is not None and mad > 0:
            return mad * _MAD_TO_SIGMA
        return sum(deviations) / len(deviations)
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance)
