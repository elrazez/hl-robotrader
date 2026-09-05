"""The robust z-score: how unusual this much forced flow is for this coin.

The distribution is sampled once a minute over a trailing lookback and the current value
is scored against it, so a z is available at every tick while the distribution behind it
moves only once a minute. Every number below is hand-derived from an alternating series
whose median and MAD are countable.
"""

from __future__ import annotations

import math

import polars as pl
import pytest

from liqfade.features.zscore import robust_z

DAY = 1_772_323_200_000
MINUTE = 60_000


def alternating(minutes: int, *, spike_at: tuple[int, int] | None = None) -> pl.DataFrame:
    """One row a second. Even minutes have log1p(value) 0, odd minutes 4.

    Six consecutive minute samples therefore hold three of each, so their median in log
    space is 2 and every deviation from it is 2, which makes the MAD 2 as well.
    """
    rows = []
    for minute in range(minutes):
        for second in range(60):
            rows.append(
                {
                    "coin": "BTC",
                    "ts_ms": DAY + minute * MINUTE + second * 1000,
                    "flow": math.expm1(0.0 if minute % 2 == 0 else 4.0),
                }
            )
    frame = pl.DataFrame(rows)
    if spike_at is not None:
        minute, second = spike_at
        frame = frame.with_columns(
            pl.when(pl.col("ts_ms") == DAY + minute * MINUTE + second * 1000)
            .then(math.expm1(10.0))
            .otherwise(pl.col("flow"))
            .alias("flow")
        )
    return frame


def z_at(frame: pl.DataFrame, minute: int, second: int) -> float | None:
    value: float | None = frame.filter(pl.col("ts_ms") == DAY + minute * MINUTE + second * 1000)[
        "flow_z"
    ][0]
    return value


def test_a_value_is_scored_against_the_minute_distribution_that_preceded_it() -> None:
    """log1p 10 against a trailing median of 2 and a MAD of 2: (10 - 2) / (1.4826 * 2)."""
    scored = robust_z(
        alternating(25, spike_at=(20, 30)), ["flow"], lookback_h=0.1, sample_s=60, robust=True
    )
    assert z_at(scored, 20, 30) == pytest.approx(2.698, rel=1e-3)


def test_a_coin_with_no_spread_in_its_trailing_flow_has_no_defined_z() -> None:
    """A flat 24h gives a zero MAD, and dividing by it would invent a signal."""
    flat = pl.DataFrame(
        {
            "coin": ["BTC"] * 1500,
            "ts_ms": [DAY + i * 1000 for i in range(1500)],
            "flow": [0.0] * 1500,
        }
    )
    scored = robust_z(flat, ["flow"], lookback_h=0.1, sample_s=60, robust=True)
    assert scored["flow_z"].drop_nulls().to_list() == []


def test_z_is_finite_wherever_it_is_defined_and_blind_to_what_comes_after() -> None:
    early = alternating(25, spike_at=(20, 30))
    late = alternating(30, spike_at=(20, 30)).with_columns(
        pl.when(pl.col("ts_ms") > DAY + 25 * MINUTE)
        .then(1e12)
        .otherwise(pl.col("flow"))
        .alias("flow")
    )
    kwargs = {"lookback_h": 0.1, "sample_s": 60, "robust": True}
    cut = DAY + 25 * MINUTE
    before = robust_z(early, ["flow"], **kwargs).filter(pl.col("ts_ms") < cut)
    after = robust_z(late, ["flow"], **kwargs).filter(pl.col("ts_ms") < cut)
    assert before.to_dicts() == after.to_dicts()
    assert before["flow_z"].drop_nulls().is_finite().all()


def test_the_non_robust_variant_scores_against_the_mean_and_standard_deviation() -> None:
    """config features.z_robust turns this on; the alternating series has mean 2, sd 2."""
    scored = robust_z(
        alternating(25, spike_at=(20, 30)), ["flow"], lookback_h=0.1, sample_s=60, robust=False
    )
    # Sample standard deviation of three 0s and three 4s is sqrt(24 / 5) = 2.1909.
    assert z_at(scored, 20, 30) == pytest.approx((10 - 2) / math.sqrt(24 / 5), rel=1e-3)
