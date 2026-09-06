"""The single place prices and sizes are rounded for Hyperliquid.

Two rules, both from `docs/HYPERLIQUID.md`, and a price has to satisfy both:

1. at most **5 significant figures**, and
2. at most **`6 - szDecimals` decimal places** for a perp.

An integer price is always valid however many significant figures it carries, which is
what lets BTC trade at 104999 rather than being pushed onto 105000. Sizes are simply
quantised to the asset's `szDecimals`.

Everything returns `Decimal`, per CLAUDE.md: floats are fine in features and the backtest,
but the value that goes on the wire is the one place a binary fraction turning into
`104999.99999999999` becomes a reject. Rounding is half-away-from-zero, not Python's
default half-to-even: a venue's tick grid has no notion of banker's rounding, and 1.2345
rounding down to 1.234 on some inputs and up on others is the kind of asymmetry that turns
into a systematic half-tick of slippage in one direction.

Nothing here reads config or state. It is pure arithmetic on a price and an integer, so the
backtest fill model and the live order manager round identically by construction.
"""

from __future__ import annotations

import math
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

__all__ = [
    "MAX_PRICE_SIG_FIGS",
    "PERP_DECIMAL_BUDGET",
    "RoundingError",
    "floor_size",
    "max_price_decimals",
    "price_is_valid",
    "round_price",
    "round_size",
    "size_is_valid",
]

# Hyperliquid's two constants. Named rather than inlined so a venue change is one edit and
# so the arithmetic below reads as the rule it implements.
MAX_PRICE_SIG_FIGS = 5
PERP_DECIMAL_BUDGET = 6  # decimals allowed = PERP_DECIMAL_BUDGET - szDecimals


class RoundingError(ValueError):
    """A price or size cannot be rounded onto the venue's grid.

    Raised rather than clamped. A non-finite price or a negative size is a bug upstream,
    and quietly turning it into something the exchange would accept is how a bug becomes
    an order.
    """


def max_price_decimals(sz_decimals: int) -> int:
    """Decimal places a perp price may carry, given the asset's `szDecimals`."""
    if not 0 <= sz_decimals <= PERP_DECIMAL_BUDGET:
        raise RoundingError(
            f"szDecimals must be 0..{PERP_DECIMAL_BUDGET} for a perp, got {sz_decimals}"
        )
    return PERP_DECIMAL_BUDGET - sz_decimals


def round_price(px: float | Decimal, sz_decimals: int) -> Decimal:
    """Round `px` onto the nearest valid Hyperliquid perp price.

    Args:
        px: the price, strictly positive and finite.
        sz_decimals: the asset's `szDecimals` from `meta.universe`.

    Returns:
        A `Decimal` satisfying both precision rules, moved by at most one tick of the
        tighter of the two. Trailing zeros are stripped, so the result is the shortest
        spelling of the value and `round_price(100.0, 2)` is `Decimal("100")`.

    Raises:
        RoundingError: `px` is not finite, not positive, or `sz_decimals` is out of range.
    """
    decimals = max_price_decimals(sz_decimals)
    value = _decimal(px, "price")
    if value <= 0:
        raise RoundingError(f"price must be positive, got {px}")

    # Significant figures first, then the decimal cap over the result. Order matters only
    # in that the second rule must be the one that gets the last word: quantising to 5
    # significant figures can leave more decimals than the asset allows (0.123456 at 5 sig
    # figs is 0.12346, which szDecimals 5 cannot express), and the reverse cannot happen.
    with_sig_figs = _to_significant_figures(value, MAX_PRICE_SIG_FIGS)
    capped = with_sig_figs.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    if capped <= 0:
        # An asset with few price decimals and a price far under one tick. Refusing beats
        # returning zero, which is a price the exchange will take and nobody meant.
        raise RoundingError(
            f"price {px} rounds to zero at szDecimals {sz_decimals} "
            f"({decimals} decimal places available)"
        )
    return _normalize(capped)


def round_size(sz: float | Decimal, sz_decimals: int) -> Decimal:
    """Round `sz` to the asset's `szDecimals`.

    Args:
        sz: the size in coin units, non-negative and finite.
        sz_decimals: the asset's `szDecimals` from `meta.universe`.

    Returns:
        A `Decimal` with at most `sz_decimals` decimal places. Zero is a valid result and
        means the size rounded away; callers must treat it as "no order", which is what
        `sizing/` does with its minimum notional check.

    Raises:
        RoundingError: `sz` is not finite, is negative, or `sz_decimals` is out of range.
    """
    if not 0 <= sz_decimals <= PERP_DECIMAL_BUDGET:
        raise RoundingError(f"szDecimals must be 0..{PERP_DECIMAL_BUDGET}, got {sz_decimals}")
    value = _decimal(sz, "size")
    if value < 0:
        raise RoundingError(f"size must be non-negative, got {sz}")
    return _normalize(value.quantize(Decimal(1).scaleb(-sz_decimals), rounding=ROUND_HALF_UP))


def floor_size(sz: float | Decimal, sz_decimals: int) -> Decimal:
    """Round `sz` **down** to the asset's `szDecimals`. What a cap must use.

    `round_size` rounds to nearest, which can round *up*, and a size rounded up past a cap
    is an order larger than every cap said was allowed. On a 25,000 USD limit that showed
    up as a notional of 25,000.00005: five hundredths of a cent, and enough to make the
    per-cluster gate refuse a trade that the per-coin cap had just approved.

    Half a tick is nothing on its own. It matters because a cap is a promise, and a promise
    that is kept to within half a tick in the wrong direction is not one. Sizing floors;
    everything else rounds to nearest.
    """
    if not 0 <= sz_decimals <= PERP_DECIMAL_BUDGET:
        raise RoundingError(f"szDecimals must be 0..{PERP_DECIMAL_BUDGET}, got {sz_decimals}")
    value = _decimal(sz, "size")
    if value < 0:
        raise RoundingError(f"size must be non-negative, got {sz}")
    return _normalize(value.quantize(Decimal(1).scaleb(-sz_decimals), rounding=ROUND_DOWN))


def price_is_valid(px: Decimal, sz_decimals: int) -> bool:
    """True when `px` is a price Hyperliquid would accept for this asset.

    The check the property test asserts against and the one an order path can assert on
    before submitting. Written from the rules rather than by calling `round_price`, so it
    is capable of disagreeing with it.
    """
    if px <= 0 or not px.is_finite():
        return False
    digits = px.normalize().as_tuple()
    exponent = digits.exponent
    if not isinstance(exponent, int):  # NaN or Infinity carry a string exponent
        return False
    if exponent >= 0:
        return True  # an integer price is always valid
    if -exponent > max_price_decimals(sz_decimals):
        return False
    return len(digits.digits) <= MAX_PRICE_SIG_FIGS


def size_is_valid(sz: Decimal, sz_decimals: int) -> bool:
    """True when `sz` has no more than `sz_decimals` decimal places and is non-negative."""
    if sz < 0 or not sz.is_finite():
        return False
    exponent = sz.normalize().as_tuple().exponent
    if not isinstance(exponent, int):
        return False
    return exponent >= -sz_decimals


def _decimal(value: float | Decimal, what: str) -> Decimal:
    """Exact Decimal for a float, via its shortest repr rather than its binary expansion.

    `Decimal(0.1)` is 0.1000000000000000055511151231257827, and quantising that is
    arithmetic about a representation error rather than about the number the caller meant.
    `Decimal(repr(0.1))` is `Decimal("0.1")`.
    """
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise RoundingError(f"{what} must be finite, got {value}")
        return value
    if not math.isfinite(value):
        raise RoundingError(f"{what} must be finite, got {value}")
    try:
        return Decimal(repr(value))
    except InvalidOperation as exc:  # pragma: no cover - repr of a float always parses
        raise RoundingError(f"{what} {value!r} is not a number") from exc


def _to_significant_figures(value: Decimal, figures: int) -> Decimal:
    """Quantise to `figures` significant figures, half away from zero.

    Integers are returned untouched: Hyperliquid accepts any integer price, so pushing
    104999 onto 105000 would be this module inventing a restriction the venue does not
    have and moving every BTC order a dollar.
    """
    if value == value.to_integral_value():
        return value
    exponent = value.adjusted()  # floor(log10(abs(value)))
    places = figures - 1 - exponent
    if places <= 0:
        # Five significant figures land at or above the ones place, so the value is an
        # integer multiple of a power of ten and the decimal cap has nothing left to do.
        return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _normalize(value: Decimal) -> Decimal:
    """Strip trailing zeros without letting `normalize` reach for exponent notation.

    `Decimal("100").normalize()` is `Decimal("1E+2")`, which is the same number and the
    wrong string to put in a JSON order payload.
    """
    normalized = value.normalize()
    exponent = normalized.as_tuple().exponent
    if isinstance(exponent, int) and exponent > 0:
        return normalized.quantize(Decimal(1))
    return normalized
