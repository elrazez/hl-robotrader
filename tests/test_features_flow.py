"""Price and flow features, all pure transforms of the grid.

Every window here is `[t - w, t)` and every expected number is arithmetic done by hand on
a series short enough to count, so a test can disagree with the implementation.
"""

from __future__ import annotations

import math

import polars as pl
import pytest

from liqfade.features.flow import cvd, displacement, realized_vol, returns

DAY = 1_772_323_200_000
STEP = 1000


def grid(mids: list[float], **columns: list[float]) -> pl.DataFrame:
    rows = len(mids)
    frame = pl.DataFrame(
        {
            "coin": ["BTC"] * rows,
            "ts_ms": [DAY + i * STEP for i in range(rows)],
            "mid": mids,
        }
    )
    for name, values in columns.items():
        frame = frame.with_columns(pl.Series(name, values))
    return frame


def at(frame: pl.DataFrame, second: int, column: str) -> float:
    return frame.filter(pl.col("ts_ms") == DAY + second * STEP)[column][0]


def test_a_return_is_the_log_change_in_mid_over_the_whole_window() -> None:
    flat_then_up = [100.0] * 10 + [110.0] * 10
    rets = returns(grid(flat_then_up), windows_s=[5], step_ms=STEP)
    assert at(rets, 9, "ret_5s") == 0.0
    assert at(rets, 12, "ret_5s") == pytest.approx(math.log(1.1))
    assert at(rets, 16, "ret_5s") == 0.0  # both ends now above the step


def test_realized_vol_is_the_root_of_the_squared_one_second_moves_and_never_negative() -> None:
    """Two 1% steps inside the window: sqrt(2) * ln(1.01)."""
    mids = [100.0] * 5 + [101.0] * 3 + [102.01] * 12
    rv = realized_vol(grid(mids), windows_s=[5], step_ms=STEP)
    assert at(rv, 9, "rv_5s") == pytest.approx(math.sqrt(2) * math.log(1.01))
    assert at(rv, 19, "rv_5s") == 0.0
    assert rv["rv_5s"].min() >= 0.0


def test_cvd_nets_the_aggressor_sides_and_its_slope_is_that_net_per_second() -> None:
    signed = [0.0] * 5 + [3e6] + [0.0] * 4 + [-1e6] + [0.0] * 9
    flow = cvd(
        grid([100.0] * 20, signed_notional=signed),
        windows_s=[15],
        slope_window_s=15,
        step_ms=STEP,
    )
    assert at(flow, 9, "cvd_15s") == 3e6  # the sell at second 10 is not in this window yet
    assert at(flow, 14, "cvd_15s") == 2e6
    assert at(flow, 14, "cvd_slope_15s") == pytest.approx(2e6 / 15)


def test_displacement_measures_the_mid_against_the_vwap_before_the_window() -> None:
    """VWAP of 200 at 100 and 100 at 103 is 101, and 104 sits 297 bps above it."""
    notional = [0.0] * 4 + [200 * 100.0, 100 * 103.0] + [0.0] * 14
    volume = [0.0] * 4 + [200.0, 100.0] + [0.0] * 14
    mids = [100.0] * 10 + [104.0] * 10
    disp = displacement(
        grid(mids, notional_usd=notional, volume=volume),
        windows_s=[5],
        anchor_s=5,
        step_ms=STEP,
    )
    assert at(disp, 12, "disp_5s_bps") == pytest.approx(1e4 * (104 - 101) / 101, rel=1e-9)


def test_no_flow_feature_can_see_a_bar_that_has_not_closed() -> None:
    """One test over all four: prices and volume after the cut, every value before it fixed."""
    early = grid(
        [100.0] * 20, signed_notional=[1e5] * 20, notional_usd=[1e5] * 20, volume=[1.0] * 20
    )
    late = early.with_columns(
        pl.when(pl.col("ts_ms") > DAY + 15 * STEP).then(1e9).otherwise(pl.col("mid")).alias("mid")
    )
    cut = pl.col("ts_ms") <= DAY + 15 * STEP
    kwargs = {"windows_s": [5], "step_ms": STEP}
    assert (
        returns(early, **kwargs).filter(cut).to_dicts()
        == returns(late, **kwargs).filter(cut).to_dicts()
    )
    assert (
        realized_vol(early, **kwargs).filter(cut).to_dicts()
        == realized_vol(late, **kwargs).filter(cut).to_dicts()
    )
    flooded = early.with_columns(
        pl.when(pl.col("ts_ms") > DAY + 15 * STEP)
        .then(1e9)
        .otherwise(pl.col("signed_notional"))
        .alias("signed_notional"),
        pl.when(pl.col("ts_ms") > DAY + 15 * STEP)
        .then(1e9)
        .otherwise(pl.col("notional_usd"))
        .alias("notional_usd"),
    )
    cvd_kwargs = {"windows_s": [5], "slope_window_s": 5, "step_ms": STEP}
    assert (
        cvd(early, **cvd_kwargs).filter(cut).to_dicts()
        == cvd(flooded, **cvd_kwargs).filter(cut).to_dicts()
    )
    disp_kwargs = {"windows_s": [5], "anchor_s": 5, "step_ms": STEP}
    assert (
        displacement(early, **disp_kwargs).filter(cut).to_dicts()
        == displacement(flooded, **disp_kwargs).filter(cut).to_dicts()
    )
