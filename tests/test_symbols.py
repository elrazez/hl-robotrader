"""Symbol map round trips and the None path for symbols that map to no HL coin."""

from __future__ import annotations

import pytest

from liqfade.data.symbols import VENUES, to_coin, to_venue_symbol

COINS = ("BTC", "ETH", "SOL", "HYPE", "kPEPE", "kBONK")


@pytest.mark.parametrize("venue", VENUES)
@pytest.mark.parametrize("coin", COINS)
def test_round_trip(venue: str, coin: str) -> None:
    assert to_coin(to_venue_symbol(coin, venue), venue) == coin


def test_the_hl_thousand_prefix_maps_to_the_venue_spelling() -> None:
    assert to_venue_symbol("kPEPE", "binance") == "1000PEPEUSDT"
    assert to_venue_symbol("kPEPE", "okx") == "1000PEPE-USDT-SWAP"
    assert to_coin("1000PEPEUSDT", "bybit") == "kPEPE"


def test_usdc_quoted_binance_symbols_map_too() -> None:
    assert to_coin("BTCUSDC", "binance") == "BTC"


def test_coinalyze_exchange_suffix_is_stripped() -> None:
    assert to_coin("BTCUSDT_PERP.A", "coinalyze") == "BTC"
    assert to_coin("ETHUSD_PERP.6", "coinalyze") == "ETH"


def test_unmappable_symbols_return_none_rather_than_raising() -> None:
    assert to_coin("FOOBAR", "binance") is None
    assert to_coin("BTC-USD-240628", "okx") is None
    assert to_coin("", "bybit") is None


def test_an_unknown_venue_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="unknown venue"):
        to_venue_symbol("BTC", "kraken")
    with pytest.raises(ValueError, match="unknown venue"):
        to_coin("BTCUSD", "kraken")
