"""Fees and funding. Every number the backtest reports is net of both.

CLAUDE.md rule 2: fees and latency are in every backtest number, and a result that only
works without them is not a result. This module is where the fee half of that lives.

**Fees** are `taker_fee_frac` or `maker_fee_frac` of notional, per fill, per side. The
defaults are Hyperliquid's base tier as of 2026-08, so a taker round trip is 0.09% of
notional. Phase 5 will read the live values from `userFees` and the numbers here become the
fallback rather than the truth.

**Funding** accrues at each hour boundary a position is open, at the rate Hyperliquid
actually applied. That rate is the `HH:59` `asset_ctx` sample and no other: the `HH:00`
sample has already reset to the next hour and is wrong by up to 1.3e-4 per hour, while at
the right offset the error is under 1.4e-6 per hour, which is under 0.4% of one taker fee.
`docs/DATA_SOURCES.md` and `scripts/research/funding_check.py` establish that, and it is
why no second funding series is pulled.

A long pays funding when the rate is positive. Where no rate can be found for an hour the
cost is zero and the run counts it, so the report can say how much of the funding line was
measured and how much was simply absent rather than quietly claiming a free hold.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from liqfade.config.models import Settings

__all__ = ["FundingBook", "fee_for", "funding_hours"]

HOUR_MS = 3_600_000
# Minutes past the hour the applied funding rate is sampled at. See the module docstring:
# the HH:00 sample has already rolled to the next hour's rate.
FUNDING_SAMPLE_MINUTE = 59


def fee_for(notional_usd: float, *, maker: bool, settings: Settings) -> float:
    """The fee on one fill, in USD. Always positive; the caller subtracts it."""
    rate = settings.backtest.maker_fee_frac if maker else settings.backtest.taker_fee_frac
    return abs(notional_usd) * rate


def funding_hours(open_ms: int, close_ms: int) -> list[int]:
    """The hour boundaries a position open over `[open_ms, close_ms)` crosses.

    Returns the timestamp of each boundary, not of the sample. A position opened at 10:30
    and closed at 12:15 crosses 11:00 and 12:00, so it pays twice. One opened at 10:10 and
    closed at 10:50 crosses nothing and pays nothing, which is the normal case for a
    strategy whose typical hold is one to sixty minutes.
    """
    if close_ms <= open_ms:
        return []
    first = ((open_ms // HOUR_MS) + 1) * HOUR_MS
    return list(range(first, close_ms + 1, HOUR_MS)) if first <= close_ms else []


@dataclass
class FundingBook:
    """The applied hourly funding rate per coin per hour, read from `HH:59` samples.

    Built once per run from the `asset_ctx` table and then queried per position. Hours with
    no sample are absent rather than zero, and `missing_hours` counts every time one was
    asked for, so the report can distinguish a genuinely free hold from an unmeasured one.
    """

    rates: dict[tuple[str, int], float] = field(default_factory=dict)
    missing_hours: int = 0
    applied_hours: int = 0

    def add_sample(self, coin: str, ts_ms: int, funding: float | None) -> None:
        """Record a sample if it is the `HH:59` one for its hour.

        Every other sample in the hour is ignored on purpose. Taking the nearest sample, or
        the last one before the boundary, would pick up the `HH:00` row for any window
        whose samples are sparse, and that row carries the *next* hour's rate.
        """
        if funding is None:
            return
        minute = (ts_ms % HOUR_MS) // 60_000
        if minute != FUNDING_SAMPLE_MINUTE:
            return
        hour = (ts_ms // HOUR_MS) * HOUR_MS
        self.rates[(coin, hour)] = funding

    def rate_at(self, coin: str, boundary_ms: int) -> float | None:
        """The rate applied at an hour boundary, from the sample one minute before it."""
        return self.rates.get((coin, boundary_ms - HOUR_MS))

    def cost(
        self,
        *,
        coin: str,
        notional_usd: float,
        is_long: bool,
        open_ms: int,
        close_ms: int,
        settings: Settings,
    ) -> float:
        """Funding paid over the life of a position, USD. Positive means we paid.

        Args:
            coin: the symbol.
            notional_usd: position notional, held constant over the life of the position.
                A scalp of one to sixty minutes rarely crosses an hour at all, so marking
                the notional per hour would be precision the rest of the model does not
                have.
            is_long: a long pays when the rate is positive.
            open_ms: when the position opened.
            close_ms: when it closed.
            settings: resolved config. `apply_funding` false returns zero.
        """
        if not settings.backtest.apply_funding:
            return 0.0
        direction = 1.0 if is_long else -1.0
        total = 0.0
        for boundary in funding_hours(open_ms, close_ms):
            rate = self.rate_at(coin, boundary)
            if rate is None:
                self.missing_hours += 1
                continue
            self.applied_hours += 1
            total += direction * rate * abs(notional_usd)
        return total
