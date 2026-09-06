"""A tiny lake whose backtest result can be worked out on paper.

The point of a golden test is that the expected numbers come from somewhere other than the
code being tested. Every other fixture in this repo is realistic and therefore
unhand-checkable: `research_lake` has fifty randomised cascades, and the consistency
fixture's prices carry gaussian noise. This one has **no noise at all**. Prices move on a
schedule, liquidations arrive in fixed amounts, and the entry, target, stop and exit of
every trade can be derived from `config/default.yaml` with a calculator.

The shape, per coin:

```
       100.00  ------\\                              /------ 100.00  (anchor)
                      \\                            /
                       \\____ 98.00 (extreme) _____/            recovery over 10 min
              |<- 30s ->|
              cascade: 6 liquidation prints, 5s apart, 1,000,000 USD each
```

One coin, GOLD, three cascades far enough apart never to interact. Every cascade is
identical, so the three trades are identical too, which is what makes the total checkable:
whatever one trade earns, the run earns three times.

Background liquidations run through the quiet stretches every 70 seconds. They are not
decoration. `robust_z` scores liquidation notional against its own trailing distribution,
and a coin whose only liquidations are the cascades has a distribution of pure zeros, a MAD
of zero, and a null z-score at every tick, correctly. 70 seconds is over
`features.episodes.join_gap_s`, so each background print is its own episode and none of
them merges with a cascade or qualifies as one.

Their **sizes cycle through a fixed pattern** rather than being constant, and that is not
decoration either: a constant background gives a distribution with a median and no spread,
so the MAD is again zero and the z is again null. The cycle is a repeating list rather than
a seeded random draw, so the fixture stays exactly reproducible by hand. None of it touches
the price path, so every number in the derivation below is unaffected.

What the numbers work out to, from config. The first step is the one that is easy to get
wrong on paper, so it is spelled out.

**The anchor is not 100.00.** It is mid at the moment the detector *starts* the cascade,
which is not the moment the price began falling. A start needs
`abs(ret_60s) / rv_5m >= cascade.ret_over_rv_min`, and at the first liquidation print the
price has not moved yet, so the ratio is near zero. On this fixture's constant slope the
ratio grows as roughly the square root of the seconds elapsed, clearing 2.0 about four
seconds in and landing, once grid and tick alignment are counted, at about **7 seconds**.
By then mid is 100.00 - 2.00 * (7 / 30) = **99.667**, and that is the anchor. The detector
cannot use a price from before it knew anything was happening; that would be lookahead.

From there:

- **extreme** is 98.00, the low the cascade reaches at +30s.
- **target** is `extreme + k_target * (anchor - extreme)` = 98.00 + 0.8 * 1.667 = **99.333**.
- **entry** is `exhaustion.fixed_delay_s` after the start, so about +127s. Mid there is
  98.00 + 2.00 * (127 - 30) / 600 = 98.323, and the fill pays `backtest.slip_stale_bps`
  on top because this lake records no book, giving 98.323 * 1.0005 = **98.372**.
- **stop** is `extreme - k_stop * rv_300s * mid`, a few tenths below 98.00, and the price
  never returns there, so every trade exits on target.
- the target is reached when the recovery passes (99.333 - 98.00) / 2.00 = 66.7%, at
  30 + 0.667 * 600 = **+430s**, so the hold is about 430 - 127 = **303 seconds**.

So: three identical trades, all `long`, all exiting on `target` at 99.333, entered at
98.372 and held about five minutes. `tests/test_backtest_golden.py` asserts those.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

__all__ = [
    "ANCHOR_PX",
    "CASCADE_STARTS",
    "DAY_MS",
    "EXTREME_PX",
    "N_CASCADES",
    "build",
    "expected_target_px",
    "write_lake",
]

DAY_MS = 86_400_000

COIN = "GOLD"
START_MS = 1_772_323_200_000  # 2026-03-01T00:00:00Z
TRADE_STEP_MS = 1000

ANCHOR_PX = 100.0
EXTREME_PX = 98.0
DROP = ANCHOR_PX - EXTREME_PX

CASCADE_LEN_MS = 30_000
RECOVERY_MS = 600_000
N_CASCADES = 3
# Far enough apart that a cascade has fully recovered and its trade has closed before the
# next one starts, so the three are genuinely independent and identical.
CASCADE_SPACING_MS = 3_600_000
# Enough warm-up for the z-score's trailing window, which the test shortens to one hour.
WARMUP_MS = 3_600_000

CASCADE_NOTIONAL = 1_000_000.0
CASCADE_PRINTS = 6
BACKGROUND_STEP_MS = 70_000
# A repeating pattern, not a random draw: `robust_z` needs a spread to divide by, and a
# constant background has none. Fixed values keep the fixture hand-checkable.
BACKGROUND_PATTERN = (8_000.0, 12_000.0, 9_500.0, 11_000.0, 10_000.0, 13_000.0, 8_500.0)
BACKGROUND_CLEARANCE_MS = 90_000

CASCADE_STARTS: list[int] = [
    START_MS + WARMUP_MS + index * CASCADE_SPACING_MS for index in range(N_CASCADES)
]
END_MS = CASCADE_STARTS[-1] + RECOVERY_MS + 600_000


def expected_target_px(k_target: float) -> float:
    """`extreme + k_target * (anchor - extreme)`, the price every trade exits at."""
    return EXTREME_PX + k_target * DROP


def mid_at(ts_ms: int) -> float:
    """The price, exactly. No noise, so every number downstream is reproducible by hand.

    Falls linearly to the extreme over the cascade, then recovers linearly to the anchor
    over ten minutes. Piecewise linear rather than smooth because a straight line is the
    thing a reader can invert without a solver.
    """
    price = ANCHOR_PX
    for start in CASCADE_STARTS:
        age = ts_ms - start
        if age < 0:
            continue
        if age <= CASCADE_LEN_MS:
            price = ANCHOR_PX - DROP * (age / CASCADE_LEN_MS)
        else:
            recovered = min((age - CASCADE_LEN_MS) / RECOVERY_MS, 1.0)
            price = EXTREME_PX + DROP * recovered
    return price


def build() -> tuple[pl.DataFrame, pl.DataFrame, int, int]:
    """`(trades, liquidations, start_ms, end_ms)` for the whole window."""
    trades = [
        {
            "coin": COIN,
            "ts_ms": ts_ms,
            "rx_ms": 0,
            "px": mid_at(ts_ms),
            "sz": 100.0,
            # Alternating, so cumulative volume delta stays near zero and no flow feature
            # accidentally becomes the thing under test.
            "side": "sell" if (ts_ms // TRADE_STEP_MS) % 2 else "buy",
            "tid": ts_ms,
        }
        for ts_ms in range(START_MS, END_MS, TRADE_STEP_MS)
    ]

    liquidations: list[dict[str, object]] = []
    for index, ts_ms in enumerate(range(START_MS, END_MS, BACKGROUND_STEP_MS)):
        near_cascade = any(
            -BACKGROUND_CLEARANCE_MS <= ts_ms - start <= CASCADE_LEN_MS + BACKGROUND_CLEARANCE_MS
            for start in CASCADE_STARTS
        )
        if not near_cascade:
            liquidations.append(_liq(ts_ms, BACKGROUND_PATTERN[index % len(BACKGROUND_PATTERN)]))
    for start in CASCADE_STARTS:
        for index in range(CASCADE_PRINTS):
            liquidations.append(_liq(start + index * 5_000, CASCADE_NOTIONAL))

    return pl.DataFrame(trades), pl.DataFrame(liquidations), START_MS, END_MS


def write_lake(root: Path, days: int) -> tuple[int, int]:
    """Write the same tape to a real Parquet lake, repeated over `days` UTC days.

    Returns `(start_ms, end_ms)` of the whole window. Each day is an independent copy of
    the one-day fixture, shifted by a whole day, so a run over three days must find
    `3 * N_CASCADES` cascades and every one of them must produce the trade the golden
    derivation above predicts. That is what makes a streaming run checkable: the day loop,
    the cache and the window maths cannot quietly drop or duplicate a day without the
    trade count saying so.

    The tape runs to midnight rather than stopping at `END_MS`, so a day boundary falls in
    the middle of quiet tape rather than in the middle of a cascade. A cascade that
    straddled midnight would be a good test of the spill, and it is a separate one; this
    fixture is the baseline the spill is measured against.
    """
    from liqfade.data.schemas import SOURCE_HL_S3
    from liqfade.recorder.writer import write_rows

    trades, liquidations, start_ms, _ = build()
    parquet = Path(root) / "parquet"
    day_start = datetime.fromtimestamp(START_MS / 1000, UTC).date()
    for offset in range(days):
        shift = offset * DAY_MS
        date = (day_start + timedelta(days=offset)).isoformat()
        for table, frame in (("trades", trades), ("liquidations", liquidations)):
            rows = frame.with_columns(pl.col("ts_ms") + shift).to_dicts()
            write_rows(parquet, (table, "hyperliquid", date, 0), rows, source=SOURCE_HL_S3)
    return start_ms, start_ms + days * DAY_MS


def _liq(ts_ms: int, notional: float) -> dict[str, object]:
    px = mid_at(ts_ms)
    return {
        "venue": "hyperliquid",
        "coin": COIN,
        "ts_ms": ts_ms,
        "rx_ms": 0,
        # Longs liquidated is forced selling, which is the direction the price moves.
        "side": "long",
        "px": px,
        "sz": notional / px,
        "notional_usd": notional,
    }
