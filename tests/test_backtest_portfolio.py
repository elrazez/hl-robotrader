"""The portfolio: marks, exposure, and a round trip whose net PnL is checkable by hand."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from liqfade.backtest.fills import Fill
from liqfade.backtest.portfolio import Portfolio
from liqfade.config.models import Settings
from liqfade.strategy.types import Intent

SettingsFactory = Callable[..., Settings]

T0 = 1_772_323_200_000
EQUITY = 100_000.0


def intent(coin: str = "BTC", side: str = "long") -> Intent:
    return Intent(
        coin=coin,
        side=side,  # type: ignore[arg-type]
        entry_px=100.0,
        target_px=102.0,
        stop_px=98.0,
        time_stop_s=900.0,
        urgency="normal",
        ts_ms=T0,
        explain={"cascade_notional_usd": 4_000_000.0},
    )


def fill(**over: object) -> Fill:
    values: dict[str, object] = {
        "cloid": "c1",
        "coin": "BTC",
        "ts_ms": T0,
        "px": 100.0,
        "size": 10.0,
        "is_buy": True,
        "maker": True,
    }
    values.update(over)
    return Fill(**values)  # type: ignore[arg-type]


class TestRoundTrip:
    def test_a_winning_long_nets_gross_minus_both_fees(
        self, settings_factory: SettingsFactory
    ) -> None:
        """10 units bought at 100 and sold at 102 is 20 gross, less two maker fees.

        Each fee is 0.015% of that leg's notional: 0.15 on the way in, 0.153 out.
        """
        settings = settings_factory()
        book = Portfolio(settings, starting_equity=EQUITY)
        book.apply_entry(fill(), intent())
        trade = book.apply_exit(fill(px=102.0, is_buy=False, tag="target"), "target")

        assert trade is not None
        maker = settings.backtest.maker_fee_frac
        expected_fees = 10.0 * 100.0 * maker + 10.0 * 102.0 * maker
        assert trade.gross_pnl == pytest.approx(20.0)
        assert trade.fees_usd == pytest.approx(expected_fees)
        assert trade.net_pnl == pytest.approx(20.0 - expected_fees)
        assert book.cash == pytest.approx(EQUITY + trade.net_pnl)

    def test_a_short_makes_money_when_price_falls(self, settings_factory: SettingsFactory) -> None:
        book = Portfolio(settings_factory(), starting_equity=EQUITY)
        book.apply_entry(fill(is_buy=False), intent(side="short"))
        trade = book.apply_exit(fill(px=98.0, is_buy=True), "target")
        assert trade is not None
        assert trade.gross_pnl == pytest.approx(20.0)
        assert trade.side == "short"

    def test_net_bps_is_against_entry_notional(self, settings_factory: SettingsFactory) -> None:
        """The unit every Phase 3 report quotes, so the backtest has to agree on it."""
        book = Portfolio(settings_factory(), starting_equity=EQUITY)
        book.apply_entry(fill(), intent())
        trade = book.apply_exit(fill(px=102.0, is_buy=False), "target")
        assert trade is not None
        assert trade.net_bps == pytest.approx(1e4 * trade.net_pnl / 1000.0)

    def test_the_trade_carries_its_explain_for_later_argument(
        self, settings_factory: SettingsFactory
    ) -> None:
        book = Portfolio(settings_factory(), starting_equity=EQUITY)
        book.apply_entry(fill(), intent())
        trade = book.apply_exit(fill(px=102.0, is_buy=False), "target")
        assert trade is not None
        assert trade.cascade_notional_usd == 4_000_000.0
        assert trade.explain["cascade_notional_usd"] == 4_000_000.0

    def test_hold_time_is_seconds(self, settings_factory: SettingsFactory) -> None:
        book = Portfolio(settings_factory(), starting_equity=EQUITY)
        book.apply_entry(fill(), intent())
        trade = book.apply_exit(fill(ts_ms=T0 + 300_000, px=102.0, is_buy=False), "target")
        assert trade is not None and trade.hold_s == 300.0


class TestScalingIn:
    def test_a_second_entry_fill_weights_the_entry_price_by_size(
        self, settings_factory: SettingsFactory
    ) -> None:
        book = Portfolio(settings_factory(), starting_equity=EQUITY)
        book.apply_entry(fill(size=10.0, px=100.0), intent())
        book.apply_entry(fill(size=30.0, px=104.0), intent())
        position = book.positions["BTC"]
        assert position.size == 40.0
        assert position.entry_px == pytest.approx((10 * 100 + 30 * 104) / 40)

    def test_slippage_is_weighted_the_same_way(self, settings_factory: SettingsFactory) -> None:
        book = Portfolio(settings_factory(), starting_equity=EQUITY)
        book.apply_entry(fill(size=10.0, slippage_bps=0.0), intent())
        book.apply_entry(fill(size=10.0, slippage_bps=10.0), intent())
        assert book.positions["BTC"].entry_slippage_bps == pytest.approx(5.0)

    def test_one_degraded_fill_marks_the_whole_position(
        self, settings_factory: SettingsFactory
    ) -> None:
        """A position half priced off the tape is not a position priced off a book."""
        book = Portfolio(settings_factory(), starting_equity=EQUITY)
        book.apply_entry(fill(size=10.0, no_book=False), intent())
        book.apply_entry(fill(size=10.0, no_book=True), intent())
        assert book.positions["BTC"].no_book_entry is True


class TestMarks:
    def test_equity_marks_open_positions_to_mid(self, settings_factory: SettingsFactory) -> None:
        settings = settings_factory()
        book = Portfolio(settings, starting_equity=EQUITY)
        book.apply_entry(fill(), intent())
        fee = 10.0 * 100.0 * settings.backtest.maker_fee_frac
        assert book.equity({"BTC": 101.0}) == pytest.approx(EQUITY - fee + 10.0)

    def test_a_coin_with_no_mid_is_marked_at_its_entry(
        self, settings_factory: SettingsFactory
    ) -> None:
        """Marking it at zero would be a fictional loss; marking it out would hide it."""
        settings = settings_factory()
        book = Portfolio(settings, starting_equity=EQUITY)
        book.apply_entry(fill(), intent())
        fee = 10.0 * 100.0 * settings.backtest.maker_fee_frac
        assert book.equity({"BTC": None}) == pytest.approx(EQUITY - fee)
        assert book.equity({}) == pytest.approx(EQUITY - fee)

    def test_gross_exposure_sums_every_coin(self, settings_factory: SettingsFactory) -> None:
        book = Portfolio(settings_factory(), starting_equity=EQUITY)
        book.apply_entry(fill(coin="BTC", size=10.0, px=100.0), intent("BTC"))
        book.apply_entry(fill(coin="ETH", size=5.0, px=200.0), intent("ETH"))
        assert book.gross_exposure({"BTC": 100.0, "ETH": 200.0}) == pytest.approx(2_000.0)

    def test_coin_exposure_is_zero_for_a_coin_with_no_position(
        self, settings_factory: SettingsFactory
    ) -> None:
        book = Portfolio(settings_factory(), starting_equity=EQUITY)
        assert book.coin_exposure("BTC", 100.0) == 0.0

    def test_open_sides_reports_what_the_risk_gates_need(
        self, settings_factory: SettingsFactory
    ) -> None:
        book = Portfolio(settings_factory(), starting_equity=EQUITY)
        book.apply_entry(fill(coin="BTC", is_buy=True), intent("BTC"))
        book.apply_entry(fill(coin="ETH", is_buy=False), intent("ETH", "short"))
        assert book.open_sides() == {"BTC": "long", "ETH": "short"}


class TestPartialExit:
    def test_a_partial_exit_leaves_the_position_open_and_produces_no_trade(
        self, settings_factory: SettingsFactory
    ) -> None:
        book = Portfolio(settings_factory(), starting_equity=EQUITY)
        book.apply_entry(fill(size=10.0), intent())
        assert book.apply_exit(fill(size=4.0, px=102.0, is_buy=False), "target") is None
        assert book.positions["BTC"].size == pytest.approx(6.0)
        assert book.trades == []

    def test_the_closing_fill_produces_the_trade(self, settings_factory: SettingsFactory) -> None:
        book = Portfolio(settings_factory(), starting_equity=EQUITY)
        book.apply_entry(fill(size=10.0), intent())
        book.apply_exit(fill(size=4.0, px=102.0, is_buy=False), "target")
        trade = book.apply_exit(fill(size=6.0, px=102.0, is_buy=False), "target")
        assert trade is not None
        assert "BTC" not in book.positions
        assert len(book.trades) == 1

    def test_the_row_is_the_whole_round_trip_not_the_closing_print(
        self, settings_factory: SettingsFactory
    ) -> None:
        """10 units out as 4 at 101 then 6 at 103 is one row of 10, summing to the cash.

        The closing print used to be the whole row: 6 units of size, 6 units of gross, an
        exit price of 103 and the first partial's realised PnL sitting in cash and in no
        row at all. A trade table that does not sum to the account it traded is not a
        trade table. Found 2026-09-08 by the paper fill-price chat.
        """
        settings = settings_factory()
        book = Portfolio(settings, starting_equity=EQUITY)
        book.apply_entry(fill(size=10.0), intent())
        book.apply_exit(fill(size=4.0, px=101.0, is_buy=False), "target")
        trade = book.apply_exit(fill(size=6.0, px=103.0, is_buy=False), "target")

        assert trade is not None
        maker = settings.backtest.maker_fee_frac
        assert trade.size == pytest.approx(10.0)
        assert trade.size * trade.entry_px == pytest.approx(1_000.0)
        assert trade.exit_px == pytest.approx((4 * 101.0 + 6 * 103.0) / 10)
        assert trade.gross_pnl == pytest.approx(4 * 1.0 + 6 * 3.0)
        assert trade.fees_usd == pytest.approx(maker * (10 * 100.0 + 4 * 101.0 + 6 * 103.0))
        assert trade.net_pnl == pytest.approx(book.cash - EQUITY)

    def test_exit_slippage_is_size_weighted_and_one_degraded_print_marks_the_exit(
        self, settings_factory: SettingsFactory
    ) -> None:
        """The exit side gets the treatment the entry side already had.

        Reporting the closing print's slippage would let a clean last fill hide a costly
        first one, and `metrics.stop_slippage_bps` reads this field.
        """
        book = Portfolio(settings_factory(), starting_equity=EQUITY)
        book.apply_entry(fill(size=10.0), intent())
        book.apply_exit(
            fill(size=4.0, px=102.0, is_buy=False, slippage_bps=20.0, no_book=True), "stop"
        )
        trade = book.apply_exit(
            fill(size=6.0, px=102.0, is_buy=False, slippage_bps=0.0, no_book=False), "stop"
        )
        assert trade is not None
        assert trade.exit_slippage_bps == pytest.approx(8.0)
        assert trade.no_book_exit is True

    def test_an_exit_with_no_position_is_ignored(self, settings_factory: SettingsFactory) -> None:
        book = Portfolio(settings_factory(), starting_equity=EQUITY)
        assert book.apply_exit(fill(is_buy=False), "target") is None


class TestNoBookAccounting:
    def test_the_share_of_degraded_fills_is_tracked(
        self, settings_factory: SettingsFactory
    ) -> None:
        """The number the report prints per split and flags a ten point gap on."""
        book = Portfolio(settings_factory(), starting_equity=EQUITY)
        book.apply_entry(fill(no_book=True), intent())
        book.apply_exit(fill(px=102.0, is_buy=False, no_book=False), "target")
        assert book.total_fills == 2
        assert book.no_book_fills == 1
        assert book.no_book_share == pytest.approx(0.5)

    def test_the_share_is_zero_before_any_fill(self, settings_factory: SettingsFactory) -> None:
        book = Portfolio(settings_factory(), starting_equity=EQUITY)
        assert book.no_book_share == 0.0


class TestFunding:
    def test_a_hold_crossing_an_hour_pays_funding_out_of_cash(
        self, settings_factory: SettingsFactory
    ) -> None:
        settings = settings_factory()
        book = Portfolio(settings, starting_equity=EQUITY)
        hour = 1_772_359_200_000  # 10:00Z
        book.funding.add_sample("BTC", hour + 59 * 60_000, 1e-4)
        book.apply_entry(fill(ts_ms=hour + 30 * 60_000), intent())
        trade = book.apply_exit(fill(ts_ms=hour + 90 * 60_000, px=102.0, is_buy=False), "target")
        assert trade is not None
        assert trade.funding_usd == pytest.approx(1000.0 * 1e-4)
        assert trade.net_pnl == pytest.approx(trade.gross_pnl - trade.fees_usd - trade.funding_usd)
