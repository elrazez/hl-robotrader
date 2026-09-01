"""Symbol mapping between the Hyperliquid coin name and each venue's spelling.

One HL coin, several venue symbols:

    BTC   -> BTCUSDT (Binance, Bybit), BTC-USDT-SWAP (OKX), BTCUSDT_PERP (Coinalyze)
    kPEPE -> 1000PEPEUSDT, 1000PEPE-USDT-SWAP

Hyperliquid prefixes a coin with `k` when the contract is denominated in 1000 units;
the other venues spell the same thing as a `1000` prefix, so that translation lives here
and nowhere else.

Unknown venue symbols map to None rather than raising. A liquidation on a coin we do not
follow is still worth recording (cross-venue cascade context is the whole point), it is
just recorded with `coin = null` and its `venue_symbol` intact.
"""

from __future__ import annotations

from typing import Literal

Venue = Literal["hyperliquid", "binance", "bybit", "okx", "coinalyze"]

VENUES: tuple[Venue, ...] = ("hyperliquid", "binance", "bybit", "okx", "coinalyze")
# USDⓈ-M futures are quoted in either. Longest first so USDT is not matched inside USDC.
QUOTES = ("USDT", "USDC", "USD")
# OKX also lists coin margined (inverse) swaps as BTC-USD-SWAP. Their size is quoted in
# contracts of USD, not of the coin, so mixing them into the same coin would corrupt any
# notional built from px * sz. They map to None and are recorded as venue symbols only.
OKX_LINEAR_QUOTES = ("USDT", "USDC")
THOUSAND = "1000"


def _base(coin: str) -> str:
    """HL coin -> the venue's base asset. kPEPE -> 1000PEPE."""
    if len(coin) > 1 and coin[0] == "k" and coin[1].isupper():
        return THOUSAND + coin[1:]
    return coin


def _coin(base: str) -> str:
    """Venue base asset -> HL coin. 1000PEPE -> kPEPE."""
    if base.startswith(THOUSAND) and len(base) > len(THOUSAND):
        return "k" + base[len(THOUSAND) :]
    return base


def _strip_quote(symbol: str) -> str | None:
    for quote in QUOTES:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)]
    return None


def to_venue_symbol(coin: str, venue: str) -> str:
    """The symbol `venue` uses for the HL coin. Raises ValueError on an unknown venue.

    The symbol is not checked against the venue's listings: a subscription for a coin the
    venue does not list simply never delivers a message, which is cheaper than keeping a
    listing table in sync. For Coinalyze this returns the pair without the exchange
    suffix (`BTCUSDT_PERP`); the poller appends the suffixes it read from
    `future-markets`, because those codes are the vendor's and cannot be derived.
    """
    base = _base(coin)
    match venue:
        case "hyperliquid":
            return coin
        case "binance" | "bybit":
            return f"{base}USDT"
        case "okx":
            return f"{base}-USDT-SWAP"
        case "coinalyze":
            return f"{base}USDT_PERP"
        case _:
            raise ValueError(f"unknown venue {venue!r}, expected one of {VENUES}")


def to_coin(venue_symbol: str, venue: str) -> str | None:
    """The HL coin for a venue symbol, or None when it does not map to one.

    None covers both a well formed symbol for a coin HL does not list and a symbol whose
    shape we do not recognise (a spot pair, a dated future, a new venue convention).
    """
    if not venue_symbol:
        return None
    match venue:
        case "hyperliquid":
            return venue_symbol
        case "binance" | "bybit":
            base = _strip_quote(venue_symbol)
        case "okx":
            parts = venue_symbol.split("-")
            # Linear perpetual swaps only: dated futures carry an expiry as the last
            # part, and inverse swaps are quoted in USD.
            base = (
                parts[0]
                if len(parts) == 3 and parts[1] in OKX_LINEAR_QUOTES and parts[2] == "SWAP"
                else None
            )
        case "coinalyze":
            # BTCUSDT_PERP.A -> BTCUSDT. The suffix after the dot is the exchange code.
            base = _strip_quote(venue_symbol.split("_", 1)[0])
        case _:
            raise ValueError(f"unknown venue {venue!r}, expected one of {VENUES}")
    return _coin(base) if base else None
