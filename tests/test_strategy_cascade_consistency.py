"""The online detector against `features/episodes.py`, on the same synthetic stream.

Two independent definitions of the same event, held to the same answer. `features/episodes`
is vectorized, sees a whole episode at once and is allowed to, because it measures cascades
after the fact for research. `strategy/cascade` sees one tick at a time and can never look
forward, because it has to trade them. The research numbers in `reports/research/` describe
episodes found by the first; the backtest trades cascades found by the second. If those
drift apart, every Phase 3 result stops applying to the strategy that was built on it, and
nothing else in the suite would notice.

The stream is built so both definitions clearly apply: cascades far enough apart that the
60s join gap cannot merge two, large enough in notional and displacement to clear the
episode floors, and quiet tape in between. That is deliberately the easy case. This test is
not asking whether the two agree on marginal events, which they will not; it asks whether
they agree at all, which is the property that would break silently.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable

import polars as pl
import pytest

from liqfade.backtest.features import build_feature_grid, iter_ticks
from liqfade.config.models import Settings
from liqfade.features.episodes import group_episodes, qualify_episodes
from liqfade.strategy.cascade import CascadeDetector

SettingsFactory = Callable[..., Settings]

COIN = "BTC"
BASE_PX = 100.0
START_MS = 1_772_323_200_000  # 2026-03-01T00:00:00Z
TRADE_STEP_MS = 1000
# Long enough that the 24h z-score lookback, shortened to an hour below, has history to
# work with before the first cascade lands.
WARMUP_MS = 3_600_000
CASCADE_SPACING_MS = 900_000
N_CASCADES = 6
CASCADE_LEN_MS = 30_000
DROP_FRAC = 0.02  # 200 bps, comfortably over features.episodes.min_move_bps of 30
CASCADE_NOTIONAL = 8_000_000.0  # over min_notional_usd of 250k by a wide margin
# Background liquidations, without which the z-score cannot exist. `robust_z` scores
# against a trailing distribution, and a coin whose only liquidations are the six cascades
# has a distribution of pure zeros: median 0, MAD 0, and a null z at every tick, correctly,
# because "how unusual is this" has no answer when nothing ever happens. Real tape is never
# that quiet. Spacing is just over `features.episodes.join_gap_s` so each background print
# is its own episode rather than merging the whole window into one, and each is far under
# `min_notional_usd` so none of them qualifies.
BACKGROUND_STEP_MS = 70_000
BACKGROUND_NOTIONAL = 10_000.0
BACKGROUND_CLEARANCE_MS = 90_000  # keep background this far from any cascade print
SEED = 20260301


def cascade_starts() -> list[int]:
    return [WARMUP_MS + START_MS + n * CASCADE_SPACING_MS for n in range(N_CASCADES)]


def displacement(ts_ms: int) -> float:
    """Fractional price displacement at `ts_ms` from every cascade running.

    Falls linearly over the cascade, then recovers over ten minutes, so each one has a
    clean single extreme at its end and the tape is flat between them.
    """
    total = 0.0
    for start in cascade_starts():
        age = ts_ms - start
        if age < 0:
            continue
        if age <= CASCADE_LEN_MS:
            total -= DROP_FRAC * age / CASCADE_LEN_MS
        else:
            recovered = min((age - CASCADE_LEN_MS) / 600_000.0, 1.0)
            total -= DROP_FRAC * (1.0 - recovered)
    return total


def build_stream() -> tuple[pl.DataFrame, pl.DataFrame, int, int]:
    """(trades, liquidations, start_ms, end_ms) for the whole synthetic window."""
    rng = random.Random(SEED)
    end_ms = cascade_starts()[-1] + 1_200_000
    trades = []
    for ts_ms in range(START_MS, end_ms, TRADE_STEP_MS):
        px = BASE_PX * (1.0 + displacement(ts_ms) + rng.gauss(0.0, 0.00005))
        selling = any(0 <= ts_ms - start <= CASCADE_LEN_MS for start in cascade_starts())
        trades.append(
            {
                "venue": "hyperliquid",
                "coin": COIN,
                "ts_ms": ts_ms,
                "rx_ms": 0,
                "source": "hl_s3",
                "px": px,
                "sz": 1.0,
                "side": "sell" if selling or rng.random() < 0.5 else "buy",
                "tid": ts_ms,
            }
        )

    liquidations = []
    for ts_ms in range(START_MS, end_ms, BACKGROUND_STEP_MS):
        if any(
            -BACKGROUND_CLEARANCE_MS <= ts_ms - start <= CASCADE_LEN_MS + BACKGROUND_CLEARANCE_MS
            for start in cascade_starts()
        ):
            continue
        px = BASE_PX * (1.0 + displacement(ts_ms))
        notional = BACKGROUND_NOTIONAL * rng.uniform(0.9, 1.1)
        liquidations.append(
            {
                "venue": "hyperliquid",
                "coin": COIN,
                "venue_symbol": COIN,
                "ts_ms": ts_ms,
                "rx_ms": 0,
                "source": "hl_s3",
                "side": "long",
                "px": px,
                "sz": notional / px,
                "notional_usd": notional,
                "method": "market",
                "complete": True,
            }
        )
    for start in cascade_starts():
        # Six prints five seconds apart: one episode under a 60s join gap, and a burst the
        # 15s peak window sees several of.
        for n in range(6):
            ts_ms = start + n * 5_000
            px = BASE_PX * (1.0 + displacement(ts_ms))
            liquidations.append(
                {
                    "venue": "hyperliquid",
                    "coin": COIN,
                    "venue_symbol": COIN,
                    "ts_ms": ts_ms,
                    "rx_ms": 0,
                    "source": "hl_s3",
                    "side": "long",
                    "px": px,
                    "sz": CASCADE_NOTIONAL / 6 / px,
                    "notional_usd": CASCADE_NOTIONAL / 6,
                    "method": "market",
                    "complete": True,
                }
            )
    return pl.DataFrame(trades), pl.DataFrame(liquidations), START_MS, end_ms


@pytest.fixture(scope="module")
def stream() -> tuple[pl.DataFrame, pl.DataFrame, int, int]:
    return build_stream()


@pytest.fixture
def settings(settings_factory: SettingsFactory) -> Settings:
    """Paper config with only the z-score lookback shortened.

    An hour rather than 24, because a test that had to generate a day of warm-up tape to
    give `robust_z` a full window would take longer than the rest of the suite. Every
    threshold the two detectors are compared on is untouched.
    """
    return settings_factory(**{"features.z_lookback_h": 1.0})


def research_episodes(
    stream: tuple[pl.DataFrame, pl.DataFrame, int, int], settings: Settings
) -> pl.DataFrame:
    """What `features/episodes.py` finds, qualified by the preregistered floors."""
    trades, liquidations, start_ms, end_ms = stream
    from liqfade.features.grid import build_grid

    bars = build_grid(trades, None, start_ms=start_ms, end_ms=end_ms, step_ms=1000)
    episodes_cfg = settings.features.episodes
    grouped = group_episodes(
        liquidations,
        bars,
        join_gap_s=episodes_cfg.join_gap_s,
        peak_window_s=episodes_cfg.peak_window_s,
        participation=episodes_cfg.participation,
        participation_window_s=episodes_cfg.participation_window_s,
    )
    qualified = qualify_episodes(
        grouped,
        min_notional_usd=episodes_cfg.min_notional_usd,
        min_move_bps=episodes_cfg.min_move_bps,
        p95_lookback_d=episodes_cfg.p95_lookback_d,
        liq_z_min=episodes_cfg.liq_z_min,
    )
    return qualified.filter(pl.col("passes_floors")).sort("start_ts")


def online_cascades(
    stream: tuple[pl.DataFrame, pl.DataFrame, int, int], settings: Settings
) -> list[tuple[int, float, float]]:
    """(start_ts, anchor_px, extreme_px) for every cascade the online detector opened."""
    trades, liquidations, start_ms, end_ms = stream
    grid = build_feature_grid(
        settings=settings,
        trades=trades,
        liquidations=liquidations,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    detector = CascadeDetector(COIN, settings)
    seen: dict[int, tuple[int, float, float]] = {}
    for _now_ms, rows in iter_ticks(grid, settings):
        row = rows.get(COIN)
        if row is None:
            continue
        detector.on_tick(row)
        state = detector.active("long")
        if state is not None:
            # Keep the latest view of each cascade, so the extreme is the one it ended
            # with rather than the one it had a tick after starting.
            seen[state.start_ts] = (state.start_ts, state.anchor_px, state.extreme_px)
    return sorted(seen.values())


def test_the_synthetic_stream_contains_the_cascades_it_claims(
    stream: tuple[pl.DataFrame, pl.DataFrame, int, int], settings: Settings
) -> None:
    """Guards the fixture itself: a stream with no episodes would pass the next test."""
    episodes = research_episodes(stream, settings)
    assert episodes.height == N_CASCADES


def test_both_detectors_find_the_same_number_of_cascades(
    stream: tuple[pl.DataFrame, pl.DataFrame, int, int], settings: Settings
) -> None:
    episodes = research_episodes(stream, settings)
    online = online_cascades(stream, settings)
    assert len(online) == episodes.height


def test_each_online_cascade_starts_inside_its_research_episode(
    stream: tuple[pl.DataFrame, pl.DataFrame, int, int], settings: Settings
) -> None:
    """The online start lands during the episode, never before it and never after it ended.

    **It cannot land within one decision tick of the research start, and that is a property
    of the spec rather than a defect.** `features/episodes` stamps an episode at its first
    liquidation print, because after the fact the print is what defines it. The online
    detector cannot start there: `cascade.ret_over_rv_min` requires `abs(ret_60s) / rv_5m`
    to reach 2.0, and at the first print price has not moved yet, so the ratio is near
    zero. It crosses only once the move has developed, which on this fixture takes about
    eight seconds of a thirty second cascade.

    Nor is that fixable by making the fixture's price drop instantly. A single jump puts
    almost all of its own size into `rv_5m` as well as into `ret_60s`, so the ratio lands
    near 1.0 and the cascade never starts at all. The condition is built to reward a
    sustained directional move over a one-off gap, which is what a liquidation cascade is.

    So the honest bar is this: the same number of events, each online start inside its own
    research episode, and the same extreme. A start before the episode's first print would
    be lookahead; a start after its last print would be a different event.
    """
    episodes = research_episodes(stream, settings)
    online = online_cascades(stream, settings)
    tolerance_ms = settings.backtest.feature_grid_ms

    pairs = zip(episodes["start_ts"].to_list(), episodes["end_ts"].to_list(), online, strict=True)
    for research_start, research_end, (start_ts, _, _) in pairs:
        lag_ms = start_ts - research_start
        assert lag_ms >= 0, (
            f"online start {start_ts} precedes the first liquidation at {research_start}, "
            f"which is lookahead"
        )
        assert start_ts <= research_end + tolerance_ms, (
            f"online start {start_ts} is {lag_ms}ms after the episode began and past its "
            f"last print at {research_end}: this is a different event, not a late start"
        )


def test_extremes_agree(
    stream: tuple[pl.DataFrame, pl.DataFrame, int, int], settings: Settings
) -> None:
    """The price each cascade reached is what the target and stop are built from.

    One grid step of tolerance in time turns into a fraction of a bar's move in price, so
    this compares to within a basis point rather than exactly.
    """
    episodes = research_episodes(stream, settings)
    online = online_cascades(stream, settings)
    for expected, (_, _, extreme_px) in zip(episodes["extreme_px"].to_list(), online, strict=True):
        assert math.isclose(extreme_px, expected, rel_tol=1e-4), (
            f"online extreme {extreme_px} against research extreme {expected}"
        )
