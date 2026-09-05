"""Fee arithmetic, and funding across exactly one and across several hour boundaries.

The funding rate is read from the `HH:59` sample and no other. `docs/DATA_SOURCES.md` and
`scripts/research/funding_check.py` establish why: the `HH:00` row has already reset to the
next hour's rate and is wrong by up to 1.3e-4 per hour, which is larger than a taker fee.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from liqfade.backtest.costs import HOUR_MS, FundingBook, fee_for, funding_hours
from liqfade.config.models import Settings

SettingsFactory = Callable[..., Settings]

# 2026-03-01T10:00:00Z, an exact hour boundary, so offsets read as minutes past ten.
H10 = 1_772_359_200_000


class TestFees:
    def test_taker_and_maker_rates_come_from_config(
        self, settings_factory: SettingsFactory
    ) -> None:
        settings = settings_factory()
        assert fee_for(10_000.0, maker=False, settings=settings) == pytest.approx(
            10_000.0 * settings.backtest.taker_fee_frac
        )
        assert fee_for(10_000.0, maker=True, settings=settings) == pytest.approx(
            10_000.0 * settings.backtest.maker_fee_frac
        )

    def test_a_taker_round_trip_is_nine_bps_at_the_base_tier(
        self, settings_factory: SettingsFactory
    ) -> None:
        """0.045% a side. The number every research report is quoted net of."""
        settings = settings_factory()
        both_sides = 2 * fee_for(100_000.0, maker=False, settings=settings)
        assert both_sides == pytest.approx(100_000.0 * 0.0009)

    def test_a_fee_is_always_positive_whichever_way_the_notional_is_signed(
        self, settings_factory: SettingsFactory
    ) -> None:
        """A short's notional arrives negative from some call sites, and it still pays."""
        settings = settings_factory()
        assert fee_for(-10_000.0, maker=False, settings=settings) > 0


class TestFundingHours:
    def test_a_hold_inside_one_hour_crosses_nothing(self) -> None:
        """The normal case: the strategy's typical hold is one to sixty minutes."""
        assert funding_hours(H10 + 10 * 60_000, H10 + 50 * 60_000) == []

    def test_exactly_one_boundary(self) -> None:
        opened = H10 + 30 * 60_000
        closed = H10 + 90 * 60_000
        assert funding_hours(opened, closed) == [H10 + HOUR_MS]

    def test_multiple_boundaries(self) -> None:
        opened = H10 + 30 * 60_000
        closed = H10 + 3 * HOUR_MS + 15 * 60_000
        assert funding_hours(opened, closed) == [
            H10 + HOUR_MS,
            H10 + 2 * HOUR_MS,
            H10 + 3 * HOUR_MS,
        ]

    def test_a_position_closed_exactly_on_the_boundary_pays_for_it(self) -> None:
        assert funding_hours(H10 + 30 * 60_000, H10 + HOUR_MS) == [H10 + HOUR_MS]

    def test_a_zero_length_hold_pays_nothing(self) -> None:
        assert funding_hours(H10, H10) == []


class TestFundingBook:
    def test_only_the_hh59_sample_is_kept(self) -> None:
        """The HH:00 row carries the NEXT hour's rate and would be wrong by more than a fee."""
        funding = FundingBook()
        funding.add_sample("BTC", H10, 9.9)  # HH:00, already rolled: ignored
        funding.add_sample("BTC", H10 + 30 * 60_000, 8.8)  # mid hour: ignored
        funding.add_sample("BTC", H10 + 59 * 60_000, 1e-5)  # the applied rate
        assert funding.rate_at("BTC", H10 + HOUR_MS) == pytest.approx(1e-5)

    def test_a_null_sample_is_not_recorded(self) -> None:
        funding = FundingBook()
        funding.add_sample("BTC", H10 + 59 * 60_000, None)
        assert funding.rate_at("BTC", H10 + HOUR_MS) is None

    def test_a_long_pays_a_positive_rate(self, settings_factory: SettingsFactory) -> None:
        funding = FundingBook()
        funding.add_sample("BTC", H10 + 59 * 60_000, 1e-5)
        cost = funding.cost(
            coin="BTC",
            notional_usd=100_000.0,
            is_long=True,
            open_ms=H10 + 30 * 60_000,
            close_ms=H10 + 90 * 60_000,
            settings=settings_factory(),
        )
        assert cost == pytest.approx(100_000.0 * 1e-5)
        assert funding.applied_hours == 1

    def test_a_short_receives_the_same_rate(self, settings_factory: SettingsFactory) -> None:
        funding = FundingBook()
        funding.add_sample("BTC", H10 + 59 * 60_000, 1e-5)
        cost = funding.cost(
            coin="BTC",
            notional_usd=100_000.0,
            is_long=False,
            open_ms=H10 + 30 * 60_000,
            close_ms=H10 + 90 * 60_000,
            settings=settings_factory(),
        )
        assert cost == pytest.approx(-100_000.0 * 1e-5)

    def test_it_accrues_across_several_hours(self, settings_factory: SettingsFactory) -> None:
        funding = FundingBook()
        for hour, rate in enumerate((1e-5, 2e-5, 3e-5)):
            funding.add_sample("BTC", H10 + hour * HOUR_MS + 59 * 60_000, rate)
        cost = funding.cost(
            coin="BTC",
            notional_usd=100_000.0,
            is_long=True,
            open_ms=H10 + 30 * 60_000,
            close_ms=H10 + 3 * HOUR_MS,
            settings=settings_factory(),
        )
        assert cost == pytest.approx(100_000.0 * (1e-5 + 2e-5 + 3e-5))
        assert funding.applied_hours == 3

    def test_a_missing_hour_costs_nothing_and_is_counted(
        self, settings_factory: SettingsFactory
    ) -> None:
        """So the report can say how much of the funding line was measured.

        The archive is missing days Hyperliquid never published. Charging a guessed rate
        would be inventing a cost; charging nothing silently would claim a free hold.
        """
        funding = FundingBook()
        funding.add_sample("BTC", H10 + 59 * 60_000, 1e-5)
        cost = funding.cost(
            coin="BTC",
            notional_usd=100_000.0,
            is_long=True,
            open_ms=H10 + 30 * 60_000,
            close_ms=H10 + 3 * HOUR_MS,
            settings=settings_factory(),
        )
        assert cost == pytest.approx(100_000.0 * 1e-5)
        assert funding.applied_hours == 1
        assert funding.missing_hours == 2

    def test_apply_funding_false_switches_it_off_entirely(
        self, settings_factory: SettingsFactory
    ) -> None:
        funding = FundingBook()
        funding.add_sample("BTC", H10 + 59 * 60_000, 1e-3)
        cost = funding.cost(
            coin="BTC",
            notional_usd=100_000.0,
            is_long=True,
            open_ms=H10,
            close_ms=H10 + 5 * HOUR_MS,
            settings=settings_factory(**{"backtest.apply_funding": False}),
        )
        assert cost == 0.0

    def test_rates_are_per_coin(self, settings_factory: SettingsFactory) -> None:
        funding = FundingBook()
        funding.add_sample("BTC", H10 + 59 * 60_000, 1e-5)
        funding.add_sample("ETH", H10 + 59 * 60_000, 5e-5)
        assert funding.rate_at("BTC", H10 + HOUR_MS) == pytest.approx(1e-5)
        assert funding.rate_at("ETH", H10 + HOUR_MS) == pytest.approx(5e-5)
        assert funding.rate_at("SOL", H10 + HOUR_MS) is None
