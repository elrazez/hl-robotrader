"""Supply and demand zones from 1m candles: fractals and rolling extremes, nothing cleverer.

The load-bearing rule is when a level becomes knowable. A fractal at bar i needs N bars on
each side of it, so it does not exist until bar i+N closes; using it before that is
lookahead of exactly N minutes and would make every zone result meaningless. The tests
below pin that first.
"""

from __future__ import annotations

import polars as pl
import pytest

from liqfade.data.schemas import SOURCE_DERIVED, rows_to_table
from liqfade.features.levels import range_levels, swing_levels, zone_features

DAY = 1_772_323_200_000
MINUTE = 60_000


def candles(highs: list[float], lows: list[float] | None = None, start: int = DAY) -> pl.DataFrame:
    lows = lows if lows is not None else [high - 1.0 for high in highs]
    return pl.from_arrow(
        rows_to_table(
            "candles_1m",
            [
                {
                    "coin": "BTC",
                    "ts_ms": start + index * MINUTE,
                    "o": low,
                    "h": high,
                    "l": low,
                    "c": (high + low) / 2,
                    "v": 10.0,
                    "n": 5,
                }
                for index, (high, low) in enumerate(zip(highs, lows, strict=True))
            ],
            SOURCE_DERIVED,
        )
    )


def test_a_swing_high_is_a_bar_that_beats_the_n_bars_on_each_side() -> None:
    """Two bars either side: the peak at index 3 qualifies, the shoulder at index 5 does not."""
    highs = [10.0, 11.0, 12.0, 20.0, 12.0, 13.0, 11.0, 10.0]
    levels = swing_levels(candles(highs), fractal_n=2).filter(pl.col("kind") == "resistance")
    assert levels["px"].to_list() == [20.0]
    assert levels["ts_ms"].to_list() == [DAY + 3 * MINUTE]


def test_a_swing_low_is_the_same_rule_upside_down() -> None:
    lows = [10.0, 9.0, 8.0, 2.0, 8.0, 9.0, 10.0, 11.0]
    levels = swing_levels(candles([low + 1.0 for low in lows], lows), fractal_n=2).filter(
        pl.col("kind") == "support"
    )
    assert levels["px"].to_list() == [2.0]


def test_a_level_is_not_knowable_until_the_bars_that_confirm_it_have_closed() -> None:
    """N bars after the swing, at that bar's close, which is one minute after its open."""
    highs = [10.0, 11.0, 20.0, 11.0, 10.0, 9.0]
    levels = swing_levels(candles(highs), fractal_n=2)
    resistance = levels.filter(pl.col("kind") == "resistance").row(0, named=True)
    assert resistance["ts_ms"] == DAY + 2 * MINUTE
    assert resistance["known_from"] == DAY + 5 * MINUTE  # bar 4 closes when bar 5 opens


def test_a_flat_run_produces_no_swing_at_all() -> None:
    """Strictly greater, not greater or equal: a plateau has no peak."""
    assert swing_levels(candles([10.0] * 9), fractal_n=2).is_empty()


def test_range_edges_are_the_prior_days_high_and_low_knowable_from_midnight() -> None:
    first = candles([10.0] * 60 + [30.0] + [10.0] * 60, start=DAY)
    second = candles([12.0] * 60, start=DAY + 86_400_000)
    levels = range_levels(pl.concat([first, second]), range_d=5)
    prior = levels.filter(pl.col("source") == "prior_day")
    assert prior.filter(pl.col("kind") == "resistance")["px"].to_list() == [30.0]
    assert prior["known_from"].unique().to_list() == [DAY + 86_400_000]


def levels_frame(*rows: tuple[float, str, int]) -> pl.DataFrame:
    """(px, kind, known_from_offset_minutes) as a levels frame."""
    return pl.DataFrame(
        {
            "coin": ["BTC"] * len(rows),
            "ts_ms": [DAY + offset * MINUTE for _, _, offset in rows],
            "px": [px for px, _, _ in rows],
            "kind": [kind for _, kind, _ in rows],
            "known_from": [DAY + offset * MINUTE for _, _, offset in rows],
            "source": ["swing"] * len(rows),
        }
    )


def queries(px: float, minute: int) -> pl.DataFrame:
    return pl.DataFrame(
        {"key": ["e1"], "coin": ["BTC"], "ts_ms": [DAY + minute * MINUTE], "px": [px]}
    )


def test_distance_to_the_nearest_level_each_side_in_basis_points() -> None:
    """Support at 99 and resistance at 101 around a price of 100: 100 bps each way."""
    found = zone_features(
        levels_frame((99.0, "support", 0), (101.0, "resistance", 0)),
        queries(100.0, 10),
        candles([100.5] * 20, [99.5] * 20),
        max_dist_bps=200.0,
        zone_width_bps=15.0,
    ).row(0, named=True)
    assert found["dist_to_support_bps"] == pytest.approx(100.0)
    assert found["dist_to_resistance_bps"] == pytest.approx(100.0)
    assert found["extreme_in_zone"] is False


def test_a_level_further_than_the_cap_is_reported_as_no_level_at_all() -> None:
    found = zone_features(
        levels_frame((50.0, "support", 0)),
        queries(100.0, 10),
        candles([100.5] * 20, [99.5] * 20),
        max_dist_bps=200.0,
        zone_width_bps=15.0,
    ).row(0, named=True)
    assert found["dist_to_support_bps"] is None


def test_a_price_inside_the_zone_width_is_in_the_zone() -> None:
    found = zone_features(
        levels_frame((99.9, "support", 0)),
        queries(100.0, 10),
        candles([100.5] * 20, [99.5] * 20),
        max_dist_bps=200.0,
        zone_width_bps=15.0,
    ).row(0, named=True)
    assert found["dist_to_support_bps"] == pytest.approx(10.0)
    assert found["extreme_in_zone"] is True


def test_a_level_formed_after_the_query_is_invisible_to_it() -> None:
    """The no-lookahead rule for zones: a cascade cannot land in a level that did not exist."""
    found = zone_features(
        levels_frame((99.9, "support", 20)),
        queries(100.0, 10),
        candles([100.5] * 30, [99.5] * 30),
        max_dist_bps=200.0,
        zone_width_bps=15.0,
    ).row(0, named=True)
    assert found["dist_to_support_bps"] is None
    assert found["extreme_in_zone"] is False


def test_touches_are_counted_only_between_the_levels_birth_and_the_query() -> None:
    """A fresh level is one price has not come back to. Freshness is what Q7 is testing."""
    highs = [100.5] * 5 + [101.5] * 3 + [100.5] * 12
    lows = [99.5] * 5 + [100.5] * 3 + [99.5] * 12
    found = zone_features(
        levels_frame((101.0, "resistance", 0)),
        queries(100.9, 10),
        candles(highs, lows),
        max_dist_bps=200.0,
        zone_width_bps=15.0,
    ).row(0, named=True)
    assert found["resistance_touches"] == 3
    assert found["resistance_fresh"] is False
