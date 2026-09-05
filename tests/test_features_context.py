"""Open interest, mark divergence, funding and session, all from asset_ctx onto the grid.

The archive samples asset_ctx once a minute, so every value here is the last sample
strictly before the grid time, never the one stamped on it. Two of these columns are gates
on the forced-flow proxy, so a lookahead here would show up as a proxy that predicts
cascades it has already seen.
"""

from __future__ import annotations

import polars as pl
import pytest

from liqfade.data.schemas import SOURCE_HL_ARCHIVE, rows_to_table
from liqfade.features.context import session_hour, with_ctx

DAY = 1_772_323_200_000
MINUTE = 60_000
STEP = 1000


def ctx(*rows: tuple[int, float, float, float]) -> pl.DataFrame:
    """(offset_minutes, mid_px, mark_px, open_interest) as canonical asset_ctx rows."""
    return pl.from_arrow(
        rows_to_table(
            "asset_ctx",
            [
                {
                    "coin": "BTC",
                    "ts_ms": DAY + minute * MINUTE,
                    "rx_ms": 0,
                    "mark_px": mark,
                    "oracle_px": mid,
                    "mid_px": mid,
                    "funding": 1.25e-5,
                    "open_interest": interest,
                    "day_ntl_vlm": 1e9,
                    "premium": 0.0002,
                    "impact_bid_px": mid * 0.9999,
                    "impact_ask_px": mid * 1.0001,
                }
                for minute, mid, mark, interest in rows
            ],
            SOURCE_HL_ARCHIVE,
        )
    )


def grid(minutes: int, coin: str = "BTC") -> pl.DataFrame:
    steps = minutes * 60
    return pl.DataFrame(
        {
            "coin": [coin] * steps,
            "ts_ms": [DAY + i * STEP for i in range(steps)],
            "mid": [100.0] * steps,
        }
    )


def at(frame: pl.DataFrame, second: int, column: str) -> float | None:
    value: float | None = frame.filter(pl.col("ts_ms") == DAY + second * STEP)[column][0]
    return value


def test_open_interest_delta_is_a_fraction_of_the_venues_own_series() -> None:
    """Hyperliquid publishes both sides of the book. A same-venue delta must not halve it.

    Halving cancels in a ratio, so the fraction is the same either way, and halving inside
    a same-venue feature would only make the raw open interest wrong.
    """
    frame = with_ctx(
        grid(6),
        ctx((0, 100.0, 100.0, 1000.0), (1, 100.0, 100.0, 900.0)),
        windows_s=[60],
        step_ms=STEP,
    )
    # At 110s the current sample is the one minute mark (900) and the one 60s earlier is
    # the zero minute mark (1000), so the window straddles the drop.
    assert at(frame, 110, "open_interest") == 900.0
    assert at(frame, 110, "oi_delta_60s") == pytest.approx(-0.1)
    assert at(frame, 130, "oi_delta_60s") == 0.0  # both ends now on the same sample


def test_a_value_at_t_comes_from_the_sample_before_t_and_never_the_one_on_it() -> None:
    frame = with_ctx(
        grid(4),
        ctx((0, 100.0, 100.0, 1000.0), (2, 100.0, 101.0, 1000.0)),
        windows_s=[60],
        step_ms=STEP,
    )
    assert at(frame, 119, "mark_px") == 100.0  # one second before the 2 minute sample
    assert at(frame, 120, "mark_px") == 100.0  # the sample stamped at t is not yet knowable
    assert at(frame, 121, "mark_px") == 101.0


def test_the_mark_divergence_percentile_excludes_the_sample_being_scored() -> None:
    """The proxy's third gate: is mark against mid further than it usually is?"""
    samples = [(minute, 100.0, 100.01, 1000.0) for minute in range(10)]
    samples.append((10, 100.0, 105.0, 1000.0))  # a divergence far outside the trailing run
    frame = with_ctx(grid(12), ctx(*samples), windows_s=[60], step_ms=STEP, quantile_lookback_s=600)
    # Just before the ten minute sample the divergence is still the usual 1 bp, and the
    # trailing p95 of ten identical 1 bp samples is 1 bp, so nothing is over it.
    assert at(frame, 599, "mark_mid_bps") == pytest.approx(1.0, rel=1e-3)
    assert at(frame, 599, "mark_mid_over_p95") is False
    assert at(frame, 601, "mark_mid_bps") == pytest.approx(500.0, rel=1e-3)
    assert at(frame, 601, "mark_mid_over_p95") is True


def test_a_coin_with_no_asset_ctx_gets_nulls_rather_than_a_guess() -> None:
    """The honest NaN contract: absent is absent, and a caller must handle it."""
    frame = with_ctx(
        grid(3, coin="PEPE"), ctx((0, 100.0, 100.0, 1000.0)), windows_s=[60], step_ms=STEP
    )
    assert frame["mark_px"].null_count() == frame.height
    assert frame["oi_delta_60s"].null_count() == frame.height


def test_the_session_bucket_is_the_utc_hour() -> None:
    hours = session_hour(grid(2))
    assert hours["session_hour"].unique().to_list() == [0]
