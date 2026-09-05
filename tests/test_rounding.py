"""Price and size rounding against Hyperliquid's two precision rules.

The expected values here are worked out from `docs/HYPERLIQUID.md` by hand, not read off
the implementation: at most 5 significant figures, at most `6 - szDecimals` decimal places,
integers always valid, sizes at exactly `szDecimals`. A test that recomputed the rule the
way the code does could never disagree with it.
"""

from __future__ import annotations

import math
import random
from decimal import Decimal

import pytest

from liqfade.execution.rounding import (
    RoundingError,
    max_price_decimals,
    price_is_valid,
    round_price,
    round_size,
    size_is_valid,
)

# `szDecimals` and a plausible price range per asset, in the shape a real `meta.universe`
# payload implies. The two travel together on purpose: szDecimals is large for an expensive
# coin (BTC trades in 0.00001 units) and zero for a cheap one, so the decimal budget of
# `6 - szDecimals` is only ever tight where prices are correspondingly large. Sampling BTC
# at 0.001 would test a price that cannot exist rather than the rounding rule.
CANNED_META = {
    "BTC": (5, 20_000.0, 200_000.0),
    "ETH": (4, 800.0, 9_000.0),
    "SOL": (2, 5.0, 900.0),
    "HYPE": (2, 1.0, 200.0),
    "PEPE": (0, 1e-6, 1.0),
    "kSHIB": (0, 1e-4, 10.0),
}


class TestRoundPrice:
    """Five significant figures, then `6 - szDecimals` decimals, then integers."""

    @pytest.mark.parametrize(
        ("px", "sz_decimals", "expected"),
        [
            # Five significant figures is the binding rule well above 1.
            (104999.6, 5, "105000"),
            (1234.567, 2, "1234.6"),
            (1234.44, 2, "1234.4"),
            # The decimal cap binds below 1, where 5 sig figs would allow more places.
            # szDecimals 5 leaves one decimal, so a price under 1 collapses onto it.
            (0.123456, 5, "0.1"),
            (0.123456, 2, "0.1235"),
            # The decimal cap has the last word: 5 significant figures would give
            # 0.000012346, which is nine decimals, and szDecimals 0 allows six. This is
            # why a coin this cheap trades on Hyperliquid as kPEPE rather than PEPE.
            (0.000012345678, 0, "0.000012"),
            # szDecimals 0 leaves six decimals, and 5 sig figs is the tighter of the two
            # for anything at or above 0.1.
            (0.55555555, 0, "0.55556"),
            # An integer is always valid however many significant figures it carries.
            (123456789.0, 2, "123456789"),
            (1000000.0, 5, "1000000"),
            # Exactly representable values come back unchanged rather than re-quantised.
            (2.5, 4, "2.5"),
            (100.0, 2, "100"),
        ],
    )
    def test_known_values(self, px: float, sz_decimals: int, expected: str) -> None:
        assert round_price(px, sz_decimals) == Decimal(expected)

    def test_half_goes_away_from_zero(self) -> None:
        """Banker's rounding would send 1.2345 to 1.234, which is not what a venue does."""
        assert round_price(1.2345, 2) == Decimal("1.2345")
        assert round_price(1.23455, 2) == Decimal("1.2346")

    def test_rejects_non_positive(self) -> None:
        for bad in (0.0, -1.0):
            with pytest.raises(RoundingError):
                round_price(bad, 2)

    def test_rejects_non_finite(self) -> None:
        for bad in (float("nan"), float("inf")):
            with pytest.raises(RoundingError):
                round_price(bad, 2)


class TestRoundSize:
    @pytest.mark.parametrize(
        ("sz", "sz_decimals", "expected"),
        [
            (1.23456789, 5, "1.23457"),
            (1.23456789, 2, "1.23"),
            (1.9, 0, "2"),
            (0.4, 0, "0"),
            (0.0, 3, "0"),
        ],
    )
    def test_known_values(self, sz: float, sz_decimals: int, expected: str) -> None:
        assert round_size(sz, sz_decimals) == Decimal(expected)

    def test_rejects_negative(self) -> None:
        with pytest.raises(RoundingError):
            round_size(-0.5, 2)


class TestMaxPriceDecimals:
    def test_is_six_minus_sz_decimals(self) -> None:
        assert [max_price_decimals(n) for n in range(7)] == [6, 5, 4, 3, 2, 1, 0]

    def test_rejects_out_of_range(self) -> None:
        for bad in (-1, 7):
            with pytest.raises(RoundingError):
                max_price_decimals(bad)


class TestPropertyOverCannedMeta:
    """1000 random prices per asset land on valid values and never move by over one tick."""

    def test_rounds_to_valid_and_within_one_tick(self) -> None:
        rng = random.Random(0)
        for coin, (sz_decimals, low, high) in CANNED_META.items():
            for _ in range(1000):
                px = 10 ** rng.uniform(math.log10(low), math.log10(high))
                rounded = round_price(px, sz_decimals)
                assert price_is_valid(rounded, sz_decimals), f"{coin} {px} -> {rounded}"
                tick = _tick_at(rounded, sz_decimals)
                moved = abs(Decimal(repr(px)) - rounded)
                assert moved <= tick, f"{coin} {px} -> {rounded} moved {moved} > {tick}"

    def test_sizes_round_to_valid(self) -> None:
        rng = random.Random(1)
        for sz_decimals, _, _ in CANNED_META.values():
            for _ in range(1000):
                sz = 10 ** rng.uniform(-6.0, 6.0)
                rounded = round_size(sz, sz_decimals)
                assert size_is_valid(rounded, sz_decimals)

    def test_price_below_one_tick_raises_rather_than_returning_zero(self) -> None:
        """BTC has one decimal place, so 0.001 is not a price it can express.

        Zero is a number the exchange would accept and nobody meant, so this refuses. The
        case cannot arise from a real asset, and the guard is here because the day it does
        arise it must not become an order.
        """
        with pytest.raises(RoundingError, match="rounds to zero"):
            round_price(0.001, 5)


def _tick_at(px: Decimal, sz_decimals: int) -> Decimal:
    """The spacing of valid prices around `px`, independently of how rounding got there.

    Whichever of the two rules is tighter at this magnitude sets the spacing: the decimal
    cap gives a fixed 10^-d, and five significant figures give a step that grows with the
    exponent. This is written from the rule rather than shared with the implementation so
    the property test can disagree with it.
    """
    decimal_tick = Decimal(1).scaleb(-max_price_decimals(sz_decimals))
    exponent = len(px.normalize().as_tuple().digits) + px.normalize().as_tuple().exponent
    sigfig_tick = Decimal(1).scaleb(exponent - 5)
    return max(decimal_tick, sigfig_tick)
