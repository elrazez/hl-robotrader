"""How unusual a value is for this coin, scored against its own recent history.

Liquidation notional is fat tailed and its scale differs by two orders of magnitude
between BTC and a minor coin, so a raw threshold in dollars means a different thing on
every coin. The z-score is what makes one cascade rule work across the universe.

Three choices, all from `docs/STRATEGY_AND_RISK.md` section 2 and the preregistration:

- **log1p first.** Notional spans decades and is zero most of the time; `log1p` is defined
  at zero and turns a multiplicative distribution into one a median and a spread describe.
- **Median and MAD, not mean and standard deviation.** A cascade in the lookback would
  inflate a standard deviation and hide the next one. Scaled by 1.4826 so that on normal
  data the result is a z-score and a 3 means three sigma, which is what the config
  threshold assumes. `robust=False` switches to mean and sample standard deviation, which
  is `features.z_robust` in the config.
- **A fallback scale for a series that is mostly zero.** `liq_long_usd_60s` is zero in 82%
  of grid seconds, so its rolling median is zero and so is the median of the absolute
  deviations from it: the MAD is exactly zero on almost every real row, and the shipped
  robust score was therefore null everywhere and the `exact` trigger never fired. Where the
  MAD is zero but the lookback holds any nonzero sample, the scale becomes the **mean**
  absolute deviation about the same median, which a handful of nonzero samples is enough to
  make positive. It is not the 1.4826-scaled sigma estimate and a 3 on it does not mean
  three sigma; what a threshold means on this scale is a measurement, recorded in
  `docs/DECISIONS.md`. A lookback that is all zeros still scores null, because a series
  that has never moved has no scale at all.
- **The distribution is sampled once a minute, the value scored is current.** A 24h
  lookback at 1s resolution is 86400 windows per point and computes in hours; at 1 minute
  it is 1440 and computes in seconds, and a distribution that moves within a minute is not
  a 24h distribution anyway.

The MAD is the streaming form: the median of `abs(v - m)` where `m` is the rolling median
at each sample, rather than the window's own median re-applied to every point in it. The
exact form needs a median of medians per window and does not vectorize; over a slowly
moving median the two agree closely. Recorded in `docs/DECISIONS.md`.

No lookahead: every window is `closed="left"`, so a sample is never part of the
distribution it is scored against, and the scale applied at time t comes from the last
minute sample strictly before t.
"""

from __future__ import annotations

from collections.abc import Sequence

import polars as pl

__all__ = ["robust_z"]

# 1 / Phi^-1(0.75). Makes the MAD a consistent estimator of sigma on normal data, so a
# threshold of 3 means three sigma rather than three MADs, which is closer to two.
_MAD_TO_SIGMA = 1.4826


def robust_z(
    frame: pl.DataFrame,
    columns: Sequence[str],
    *,
    lookback_h: float,
    sample_s: int,
    robust: bool,
) -> pl.DataFrame:
    """Score each column against its own trailing distribution.

    Args:
        frame: any `coin`/`ts_ms` grid carrying the columns. Values must be >= 0, since
            they are passed through `log1p`.
        columns: the columns to score. Each gains a `{column}_z`.
        lookback_h: hours of trailing minute samples behind the distribution.
        sample_s: seconds between distribution samples. 60 per the spec.
        robust: median and MAD when true, mean and sample standard deviation when false.
            Under `robust`, a zero MAD falls back to the mean absolute deviation about the
            median, which is what makes the score exist at all on a mostly-zero series.

    Returns:
        `frame` plus one `{column}_z` per input column. The z is null where the scale is
        zero or the lookback is not yet full enough to have one, which is the first
        `lookback_h` of any coin and any coin whose flow never varies at all. Null means
        undefined, not zero: a caller must not read it as "not unusual".
    """
    sample_ms = sample_s * 1000
    window = f"{int(lookback_h * 3_600_000)}ms"
    logged = [pl.col(column).log1p().alias(f"_log_{column}") for column in columns]
    frame = frame.sort("coin", "ts_ms").with_columns(logged)

    samples = (
        frame.filter(pl.col("ts_ms") % sample_ms == 0)
        .select("coin", "ts_ms", *[f"_log_{column}" for column in columns])
        .with_columns(pl.col("ts_ms").cast(pl.Datetime("ms")).alias("when"))
        .sort("when")
    )
    for column in columns:
        value = pl.col(f"_log_{column}")
        if robust:
            centre = value.rolling_median_by("when", window_size=window, closed="left")
            samples = samples.with_columns(centre.over("coin").alias(f"_m_{column}"))
            deviation = (value - pl.col(f"_m_{column}")).abs()
            mad = deviation.rolling_median_by("when", window_size=window, closed="left")
            # The mean absolute deviation about the same median. On a series that is zero
            # most of the time the median of the deviations is zero and the mean of them is
            # not, so this is the scale that exists where the MAD does not.
            aad = deviation.rolling_mean_by("when", window_size=window, closed="left")
            samples = samples.with_columns(
                (mad.over("coin") * _MAD_TO_SIGMA).alias(f"_mad_{column}"),
                aad.over("coin").alias(f"_aad_{column}"),
            )
            samples = samples.with_columns(
                pl.when(pl.col(f"_mad_{column}") > 0)
                .then(pl.col(f"_mad_{column}"))
                .otherwise(pl.col(f"_aad_{column}"))
                .alias(f"_s_{column}")
            )
            samples = samples.drop(f"_mad_{column}", f"_aad_{column}")
        else:
            centre = value.rolling_mean_by("when", window_size=window, closed="left")
            spread = value.rolling_std_by("when", window_size=window, closed="left")
            samples = samples.with_columns(
                centre.over("coin").alias(f"_m_{column}"),
                spread.over("coin").alias(f"_s_{column}"),
            )

    # The scale in force at t is the last minute sample strictly before t, so a decision at
    # t never uses a distribution that included t.
    scales = samples.select(
        "coin",
        pl.col("ts_ms").alias("sampled_at"),
        *[f"_{part}_{column}" for column in columns for part in ("m", "s")],
    ).sort("sampled_at")
    scored = (
        frame.with_columns((pl.col("ts_ms") - 1).alias("before"))
        .sort("before")
        .join_asof(scales, left_on="before", right_on="sampled_at", by="coin", strategy="backward")
    )
    for column in columns:
        scale = pl.col(f"_s_{column}")
        scored = scored.with_columns(
            pl.when(scale > 0)
            .then((pl.col(f"_log_{column}") - pl.col(f"_m_{column}")) / scale)
            .otherwise(None)
            .alias(f"{column}_z")
        )
    drop = ["before", "sampled_at"] + [
        name for column in columns for name in (f"_log_{column}", f"_m_{column}", f"_s_{column}")
    ]
    return scored.drop(drop).sort("coin", "ts_ms")
