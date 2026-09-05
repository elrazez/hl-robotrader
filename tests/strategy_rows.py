"""Hand-built `FeatureRow` values shared by the strategy layer's tests.

Not a test module. It lives beside them rather than under `fixtures/` because pytest's
prepend import mode already puts `tests/` on `sys.path`, so a plain import reaches it.

Two builders, and the whole design is that they differ in exactly the conditions the spec
names: `falling_row` satisfies every long-liquidation start condition at once, so a test
takes one away and asserts nothing starts. `quiet_row` satisfies none of them, so it is
what a cascade is allowed to run through without restarting.
"""

from __future__ import annotations

from liqfade.strategy.types import FeatureRow

START_MS = 1_772_323_200_000  # 2026-03-01T00:00:00Z
TICK_MS = 250


def falling_row(ts_ms: int, mid: float, **over: float | None) -> FeatureRow:
    """A tick that satisfies every long-liquidation start condition at once.

    z 4.0 clears `cascade.liq_z_min` 3.0; ret/rv is 0.01/0.002 = 5, clearing
    `ret_over_rv_min` 2.0; imbalance +0.8 clears `imbalance_min` 0.6 on the long side.
    Override one field to take that condition away and assert the cascade does not start.
    """
    values: dict[str, float | None] = {
        "liq_long_usd_w": 4_000_000.0,
        "liq_short_usd_w": 400_000.0,
        "liq_imbalance_w": 0.8,
        "liq_z_long_w": 4.0,
        "liq_z_short_w": 0.0,
        "liq_long_usd_peak": 1_000_000.0,
        "ret_w": -0.01,
        "rv": 0.002,
        "cvd_slope": -50_000.0,
        "mark_mid_bps": 40.0,
        "mark_mid_threshold": 10.0,
        "book_imbalance": -0.5,
    }
    values.update(over)
    return FeatureRow(coin="BTC", ts_ms=ts_ms, mid=mid, **values)  # type: ignore[arg-type]


def quiet_row(ts_ms: int, mid: float, **over: float | None) -> FeatureRow:
    """A tick with no forced flow and price going nowhere."""
    values: dict[str, float | None] = {
        "liq_long_usd_w": 0.0,
        "liq_short_usd_w": 0.0,
        "liq_imbalance_w": 0.0,
        "liq_z_long_w": 0.0,
        "liq_z_short_w": 0.0,
        "liq_long_usd_peak": 0.0,
        "ret_w": 0.0,
        "rv": 0.002,
        "cvd_slope": 10_000.0,
        "mark_mid_bps": 2.0,
        "mark_mid_threshold": 10.0,
        "book_imbalance": 0.4,
    }
    values.update(over)
    return FeatureRow(coin="BTC", ts_ms=ts_ms, mid=mid, **values)  # type: ignore[arg-type]
