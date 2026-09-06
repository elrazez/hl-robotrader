"""Build the feature grid once, vectorized, then hand it to the strategy row by row.

This is the seam the spec's speed rule lives on: *vectorize feature computation on a 1s
grid, keep the fill simulation event driven*. Every function in `liqfade/features/` is a
polars transform over a whole window, so a year of ten coins is a few dozen group-bys
rather than eighty million Python calls. `FeatureRow` is what the strategy sees, and
`iter_ticks` is where the vectorized world ends and the online one begins.

**The grid step and the decision cadence are different numbers on purpose.**
`backtest.feature_grid_ms` (1000) is how finely the features are computed;
`features.decision_cadence_ms` (250) is how often the strategy is asked. On a 1s grid,
three of every four decision ticks therefore see the row they saw before. That costs
nothing under `exhaustion.mode: fixed_delay`, which is the default and which only reads the
clock; under `exhaustion.mode: rule` the `consecutive_ticks` streak completes in 750ms of
wall time either way, but the conditions it counts only really change once a second. Set
`backtest.feature_grid_ms: 250` for a run that matches live exactly, and expect it to take
four times as long and four times the memory.

**Which venues fill the liquidation columns is `cascade.trigger`.** That single key is what
separates the historical ceiling from anything tradeable:

- `exact`: Hyperliquid's own liquidations from the S3 pull. Complete, and invisible in
  real time, which is why `Settings` refuses this trigger on mainnet.
- `crossvenue`: the recorded Bybit, OKX and Binance feeds, weighted by
  `venues.*.liq_weight`. What Phase 5 runs if Q4 passes. On the historical year these exist
  on the twelve Tardis days only, and the report has to print which days it had.
- `proxy`: no liquidation feed at all. The columns come from the same cross-venue set so
  the intensity features are populated where they can be, but the trigger itself reads
  `hl_forced_proxy`, and the detector drops the imbalance condition accordingly.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence

import polars as pl

from liqfade.config.models import Settings
from liqfade.features.book import book_features
from liqfade.features.context import with_ctx
from liqfade.features.flow import cvd, realized_vol, returns
from liqfade.features.grid import build_grid
from liqfade.features.intensity import liq_intensity
from liqfade.features.proxy import hl_forced_proxy
from liqfade.features.zscore import robust_z
from liqfade.strategy.types import FeatureRow

__all__ = [
    "FeatureWindows",
    "build_feature_grid",
    "iter_grid_rows",
    "iter_ticks",
    "trigger_venues",
]

# Venues whose liquidations a live bot can actually see. Hyperliquid is absent by design:
# its own liquidations reach us through a next-day S3 pull and nothing else.
LIVE_VENUES: tuple[str, ...] = ("bybit", "okx", "binance")


class FeatureWindows:
    """The resolved window lengths, so nothing downstream re-derives them from config.

    Every one of these is a config key rather than the spec's literal, and a mismatch
    between what the grid computed and what a column is named is the kind of bug that
    produces plausible numbers. Resolving them once, here, is what stops that.
    """

    def __init__(self, settings: Settings) -> None:
        self.step_ms = settings.backtest.feature_grid_ms
        self.cascade_s = int(settings.cascade.window_s)
        self.peak_s = int(settings.features.episodes.peak_window_s)
        # The rv that `intent.k_stop` is measured in: the longest configured rv window,
        # which is the spec's rv_5m.
        self.rv_s = int(max(settings.features.rv_windows_s))
        self.cvd_s = int(settings.features.cvd_slope_window_s)
        self.oi_s = int(settings.features.proxy.oi_delta_window_s)
        self.vol_s = int(settings.sizing.vol_window_s)
        for name, seconds in (
            ("cascade.window_s", self.cascade_s),
            ("features.episodes.peak_window_s", self.peak_s),
            ("features.rv_windows_s", self.rv_s),
            ("features.cvd_slope_window_s", self.cvd_s),
            ("sizing.vol_window_s", self.vol_s),
        ):
            if seconds * 1000 % self.step_ms:
                raise ValueError(
                    f"{name} of {seconds}s is not a whole number of "
                    f"backtest.feature_grid_ms ({self.step_ms}ms) steps"
                )

    @property
    def intensity_windows(self) -> list[int]:
        """The windows `liq_intensity` computes, deduplicated and ordered."""
        return sorted({self.peak_s, self.cascade_s})


def trigger_venues(settings: Settings) -> tuple[str, ...]:
    """Which venues' liquidations fill the intensity columns, given `cascade.trigger`."""
    if settings.cascade.trigger == "exact":
        return ("hyperliquid",)
    return tuple(venue for venue in LIVE_VENUES if getattr(settings.venues, venue).enabled)


def build_feature_grid(
    *,
    settings: Settings,
    trades: pl.DataFrame,
    liquidations: pl.DataFrame,
    start_ms: int,
    end_ms: int,
    bbo: pl.DataFrame | None = None,
    l2: pl.DataFrame | None = None,
    asset_ctx: pl.DataFrame | None = None,
    levels: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Compute every feature the strategy reads, for every coin, over `[start_ms, end_ms)`.

    Args:
        settings: resolved config. `cascade.trigger` decides which venues' liquidations
            are used; every window comes from `FeatureWindows`.
        trades: canonical `trades` rows, Hyperliquid only. The tape the grid is built on.
        liquidations: canonical `liquidations` rows across every venue. Filtered here.
        start_ms: first grid timestamp, inclusive.
        end_ms: end of the grid, exclusive.
        bbo: canonical `bbo`, or None. Absent for the whole historical year.
        l2: canonical `l2`, or None. Hourly snapshots on four coins historically.
        asset_ctx: canonical `asset_ctx`, or None. Funding, mark and open interest.
        levels: `features/levels.py` output, or None. Only read by `zone_proximity`.

    Returns:
        One row per coin per `backtest.feature_grid_ms`, carrying every column
        `iter_ticks` reads. Warm-up rows near `start_ms` have nulls where a trailing
        window is not yet full; that is absence, and the strategy treats it as such.
    """
    windows = FeatureWindows(settings)
    if trades.is_empty():
        return pl.DataFrame()

    grid = build_grid(trades, bbo, start_ms=start_ms, end_ms=end_ms, step_ms=windows.step_ms)
    grid = _with_intensity(grid, liquidations, settings, windows)
    grid = returns(grid, windows_s=[windows.cascade_s], step_ms=windows.step_ms)
    grid = realized_vol(grid, windows_s=[windows.rv_s], step_ms=windows.step_ms)
    grid = cvd(
        grid,
        windows_s=[windows.cvd_s],
        slope_window_s=windows.cvd_s,
        step_ms=windows.step_ms,
    )
    grid = book_features(grid, _frame(l2), depth_bps=settings.features.depth_bps)
    grid = with_ctx(
        grid,
        _frame(asset_ctx),
        windows_s=[windows.oi_s],
        step_ms=windows.step_ms,
        # The exhaustion rule is the only consumer of `mark_mid_threshold`, so the grid is
        # built at its percentile. The proxy's own gate wants a different one and gets a
        # second pass below, which only runs when the proxy trigger is selected.
        quantile=settings.exhaustion.mark_mid_pct_threshold / 100.0,
    )
    grid = _with_traded_volume(grid, windows)
    if settings.cascade.trigger == "proxy":
        grid = _with_proxy(grid, settings, windows, _frame(asset_ctx))
    return _with_levels(grid, levels, settings)


def active_mask(grid: pl.DataFrame, settings: Settings) -> pl.Series:
    """Which grid rows a cascade could possibly involve. A deliberate **superset**.

    Almost every second of a year is a market in which nothing can happen: no forced flow,
    no move, nothing for the detector to do but be handed a row and put it down. Building a
    `FeatureRow` for each of those is most of a run's wall time, and this is what stops it.

    The mask is a superset of the detector's own start conditions on purpose. It drops the
    direction test that pairs the move with the liquidated side, and it takes either side's
    z-score, so a row the detector *would* have acted on can never be filtered away. Being
    too generous here costs a little speed; being too strict would silently change results,
    which is why the test asserts a filtered run matches an unfiltered one trade for trade.

    Every row within `exhaustion.max_age_s` after a candidate is kept too, because that is
    how long the detector can still be tracking the cascade that started there.
    """
    windows = FeatureWindows(settings)
    cascade, exhaustion = settings.cascade, settings.exhaustion
    w, rv_s = windows.cascade_s, windows.rv_s

    if cascade.trigger == "proxy":
        long_side = pl.col(f"hl_forced_proxy_long_{w}s") >= cascade.proxy_min
        short_side = pl.col(f"hl_forced_proxy_short_{w}s") >= cascade.proxy_min
        flow = long_side.fill_null(False) | short_side.fill_null(False)
        lopsided = pl.lit(True)
    else:
        long_side = pl.col(f"liq_long_usd_{w}s_z") >= cascade.liq_z_min
        short_side = pl.col(f"liq_short_usd_{w}s_z") >= cascade.liq_z_min
        flow = long_side.fill_null(False) | short_side.fill_null(False)
        lopsided = (pl.col(f"liq_imbalance_{w}s").abs() >= cascade.imbalance_min).fill_null(False)

    moved = (
        pl.col(f"ret_{w}s").abs() / pl.col(f"rv_{rv_s}s") >= cascade.ret_over_rv_min
    ).fill_null(False)
    candidate = flow & moved & lopsided
    # +1 step so the row that starts a cascade is itself inside the window it opens.
    span = int(exhaustion.max_age_s * 1000 // windows.step_ms) + 1
    return (
        grid.sort("coin", "ts_ms")
        .with_columns(candidate.cast(pl.Int8).alias("_candidate"))
        .with_columns(
            pl.col("_candidate")
            .rolling_max(window_size=span, min_samples=1)
            .over("coin")
            .alias("_needed")
        )
        .get_column("_needed")
        .cast(pl.Boolean)
    )


def iter_grid_rows(
    grid: pl.DataFrame, settings: Settings, *, coins: Sequence[str] | None = None
) -> Iterator[tuple[int, dict[str, FeatureRow]]]:
    """Walk the grid one timestamp at a time, yielding the coins that updated at it.

    **One entry per grid step, not per decision tick.** The engine carries the last row
    forward between them, which is the same thing `iter_ticks` used to do by copying a
    mapping four times a second: a month of ten coins is ten million ticks, and building a
    dict for each was most of the run's wall time for no information at all.

    Columns are pulled out as Python lists once rather than through `iter_rows(named=True)`,
    which builds a dict per row. That is the difference between roughly 18 and roughly 3
    microseconds a row, and there are 86,400 rows per coin per day.

    Yields `(ts_ms, {coin: FeatureRow})` in time order, coins within a timestamp in name
    order, so a replay is reproducible.
    """
    if grid.is_empty():
        return
    sorted_grid = grid.sort("coin", "ts_ms")
    frame = sorted_grid.filter(active_mask(sorted_grid, settings)).sort("ts_ms", "coin")
    if coins is not None:
        frame = frame.filter(pl.col("coin").is_in(sorted(set(coins))))
    if frame.is_empty():
        return

    windows = FeatureWindows(settings)
    columns = _row_columns(windows)
    present = {name: frame.get_column(name).to_list() for name in columns if name in frame.columns}
    missing = {name: [None] * frame.height for name in columns if name not in frame.columns}
    values = present | missing
    stamps = frame.get_column("ts_ms").to_list()
    names = frame.get_column("coin").to_list()

    batch: dict[str, FeatureRow] = {}
    current = stamps[0]
    for index in range(frame.height):
        stamp = stamps[index]
        if stamp != current:
            yield current, batch
            batch, current = {}, stamp
        batch[names[index]] = _row_at(values, index, names[index], stamp, windows)
    if batch:
        yield current, batch


def iter_ticks(
    grid: pl.DataFrame, settings: Settings, *, coins: Sequence[str] | None = None
) -> Iterator[tuple[int, dict[str, FeatureRow]]]:
    """Walk the grid at the decision cadence, carrying the last row forward.

    Kept for callers that want the per-tick view directly, and for the consistency test.
    The replay engine uses `iter_grid_rows` and carries rows forward itself, so it never
    pays for the duplicate mappings this produces.

    A decision at `t` reads the last grid row whose timestamp is at or before `t`. Nothing
    from after `t` can reach it: `build_grid`'s half-open convention already guarantees a
    row stamped `T` was knowable at `T`.
    """
    cadence_ms = settings.features.decision_cadence_ms
    step_ms = settings.backtest.feature_grid_ms
    by_ts = list(iter_grid_rows(grid, settings, coins=coins))
    if not by_ts:
        return

    latest: dict[str, FeatureRow] = {}
    index = 0
    first, last = by_ts[0][0], by_ts[-1][0]
    for now_ms in range(first, last + step_ms, cadence_ms):
        while index < len(by_ts) and by_ts[index][0] <= now_ms:
            latest.update(by_ts[index][1])
            index += 1
        if latest:
            yield now_ms, dict(latest)


# --- internals ----------------------------------------------------------------


def _frame(value: pl.DataFrame | None) -> pl.DataFrame:
    """An empty frame stands in for a missing table, which every feature handles."""
    return value if value is not None else pl.DataFrame()


def _with_intensity(
    grid: pl.DataFrame, liquidations: pl.DataFrame, settings: Settings, windows: FeatureWindows
) -> pl.DataFrame:
    """Liquidation notional, imbalance and z-score, from the trigger's venue set."""
    venues = trigger_venues(settings)
    selected = (
        liquidations.filter(pl.col("venue").is_in(list(venues)))
        if not liquidations.is_empty()
        else liquidations
    )
    columns = [
        f"liq_{side}_usd_{window}s"
        for window in windows.intensity_windows
        for side in ("long", "short")
    ]
    if selected.is_empty():
        # No liquidations for this trigger over this window. Zero notional is a real
        # measurement; the z-score of it is not, and stays null so the detector reads it
        # as "unknown" rather than "not unusual".
        blanks = [pl.lit(0.0, dtype=pl.Float64).alias(name) for name in columns]
        blanks += [
            pl.lit(0.0, dtype=pl.Float64).alias(f"liq_imbalance_{window}s")
            for window in windows.intensity_windows
        ]
        blanks += [pl.lit(None, dtype=pl.Float64).alias(f"{name}_z") for name in columns]
        return grid.with_columns(blanks)

    weights = {venue: getattr(settings.venues, venue).liq_weight for venue in venues}
    out = liq_intensity(
        selected,
        grid,
        windows_s=windows.intensity_windows,
        step_ms=windows.step_ms,
        weights=weights,
    )
    cascade_columns = [f"liq_{side}_usd_{windows.cascade_s}s" for side in ("long", "short")]
    out = robust_z(
        out,
        cascade_columns,
        lookback_h=settings.features.z_lookback_h,
        sample_s=int(settings.features.z_grid_s),
        robust=settings.features.z_robust,
    )
    # The peak window gets no z-score: nothing reads one, and a second robust_z pass over
    # a year is minutes of work for a column no rule consults.
    return out


def _with_traded_volume(grid: pl.DataFrame, windows: FeatureWindows) -> pl.DataFrame:
    """Trailing HL notional over `sizing.vol_window_s`, for the volume participation cap."""
    span = windows.vol_s * 1000 // windows.step_ms
    return grid.sort("coin", "ts_ms").with_columns(
        pl.col("notional_usd")
        .rolling_sum(window_size=span, min_samples=1)
        .over("coin")
        .alias(f"traded_usd_{windows.vol_s}s")
    )


def _with_proxy(
    grid: pl.DataFrame, settings: Settings, windows: FeatureWindows, ctx: pl.DataFrame
) -> pl.DataFrame:
    """The gated forced flow proxy. Only built when the proxy trigger is selected.

    Needs `mark_mid_over_p95` at the *proxy's* percentile, and the grid already carries the
    exhaustion rule's. Rather than compute the whole context twice, this runs `with_ctx`
    again on a minimal frame and takes the one boolean column back. It is the price of two
    consumers wanting two different percentiles of the same series, and it is only paid on
    the trigger that is off by default.
    """
    gate_source = with_ctx(
        grid.select("coin", "ts_ms", "mid"),
        ctx,
        windows_s=[windows.oi_s],
        step_ms=windows.step_ms,
        quantile=settings.features.proxy.mark_mid_pct_threshold / 100.0,
    ).select("coin", "ts_ms", "mark_mid_over_p95")
    gated = grid.drop("mark_mid_over_p95", strict=False).join(
        gate_source, on=["coin", "ts_ms"], how="left"
    )
    return hl_forced_proxy(
        gated,
        window_s=windows.cascade_s,
        median_lookback_s=int(settings.features.proxy.vol_median_lookback_s),
        step_ms=windows.step_ms,
        gated=True,
        oi_window_s=windows.oi_s,
    )


def _with_levels(
    grid: pl.DataFrame, levels: pl.DataFrame | None, settings: Settings
) -> pl.DataFrame:
    """Nearest support and resistance price per row, for the `zone_proximity` rule.

    Skipped entirely when the rule is disabled, which it is by default: joining every level
    against every grid row is the most expensive thing in this module, and Q7 says the
    answer it computes is not one we act on.
    """
    if not settings.strategy.rules.zone_proximity.enabled or levels is None or levels.is_empty():
        return grid.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("support_px"),
            pl.lit(None, dtype=pl.Float64).alias("resistance_px"),
        )
    out = grid
    for kind, column in (("support", "support_px"), ("resistance", "resistance_px")):
        side = (
            levels.filter(pl.col("kind") == kind)
            .select("coin", pl.col("px").alias(column), "known_from")
            .sort("known_from")
        )
        if side.is_empty():
            out = out.with_columns(pl.lit(None, dtype=pl.Float64).alias(column))
            continue
        # A level is only usable once it is knowable, which `known_from` records, so the
        # join is as-of on that rather than a plain join on coin.
        out = (
            out.sort("ts_ms")
            .join_asof(side, left_on="ts_ms", right_on="known_from", by="coin", strategy="backward")
            .drop("known_from", strict=False)
        )
    return out.sort("coin", "ts_ms")


def _row_columns(windows: FeatureWindows) -> tuple[str, ...]:
    """Every grid column a `FeatureRow` reads, so the walk pulls each out exactly once."""
    cascade_s, peak_s = windows.cascade_s, windows.peak_s
    return (
        "mid",
        f"liq_long_usd_{cascade_s}s",
        f"liq_short_usd_{cascade_s}s",
        f"liq_imbalance_{cascade_s}s",
        f"liq_long_usd_{cascade_s}s_z",
        f"liq_short_usd_{cascade_s}s_z",
        f"hl_forced_proxy_long_{cascade_s}s",
        f"hl_forced_proxy_short_{cascade_s}s",
        f"liq_long_usd_{peak_s}s",
        f"liq_short_usd_{peak_s}s",
        f"ret_{cascade_s}s",
        f"rv_{windows.rv_s}s",
        f"cvd_slope_{windows.cvd_s}s",
        "spread_bps",
        "depth_bid",
        "depth_ask",
        "book_imbalance",
        "mark_mid_bps",
        "mark_mid_threshold",
        "funding",
        f"traded_usd_{windows.vol_s}s",
        "support_px",
        "resistance_px",
    )


def _row_at(
    values: Mapping[str, list[object]],
    index: int,
    coin: str,
    ts_ms: int,
    windows: FeatureWindows,
) -> FeatureRow:
    """One grid row as a `FeatureRow`. The only place column names become field names."""
    cascade_s, peak_s = windows.cascade_s, windows.peak_s

    def number(name: str) -> float | None:
        value = values[name][index]
        # bool is a subclass of int and several grid columns are booleans, so it is
        # excluded explicitly rather than silently becoming 1.0.
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return float(value)

    def zero(name: str) -> float:
        value = number(name)
        return 0.0 if value is None else value

    return FeatureRow(
        coin=coin,
        ts_ms=ts_ms,
        mid=number("mid"),
        liq_long_usd_w=zero(f"liq_long_usd_{cascade_s}s"),
        liq_short_usd_w=zero(f"liq_short_usd_{cascade_s}s"),
        liq_imbalance_w=zero(f"liq_imbalance_{cascade_s}s"),
        liq_z_long_w=number(f"liq_long_usd_{cascade_s}s_z"),
        liq_z_short_w=number(f"liq_short_usd_{cascade_s}s_z"),
        proxy_long_w=number(f"hl_forced_proxy_long_{cascade_s}s"),
        proxy_short_w=number(f"hl_forced_proxy_short_{cascade_s}s"),
        liq_long_usd_peak=zero(f"liq_long_usd_{peak_s}s"),
        liq_short_usd_peak=zero(f"liq_short_usd_{peak_s}s"),
        ret_w=number(f"ret_{cascade_s}s"),
        rv=number(f"rv_{windows.rv_s}s"),
        cvd_slope=number(f"cvd_slope_{windows.cvd_s}s"),
        spread_bps=number("spread_bps"),
        depth_bid=number("depth_bid"),
        depth_ask=number("depth_ask"),
        book_imbalance=number("book_imbalance"),
        mark_mid_bps=number("mark_mid_bps"),
        mark_mid_threshold=number("mark_mid_threshold"),
        funding=number("funding"),
        traded_usd_vol_window=number(f"traded_usd_{windows.vol_s}s"),
        support_px=number("support_px"),
        resistance_px=number("resistance_px"),
        window_s=cascade_s,
        peak_window_s=peak_s,
        rv_window_s=windows.rv_s,
    )
