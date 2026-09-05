"""The fill model's edge cases, which are where a backtest lies if it is going to.

Latency, maker no-fill and partial fill, queue pessimism, the taker VWAP walk, the stale
book penalty, the degraded no-book fill, and a stop gapping through. Every expected price is
worked out by hand from the book in the test.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from liqfade.backtest.fills import (
    BookState,
    OrderKind,
    SimOrder,
    fill_maker,
    fill_taker,
    gapped_stop_order,
    stop_triggered,
    walk_book,
)
from liqfade.config.models import Settings

SettingsFactory = Callable[..., Settings]

T0 = 1_772_323_200_000


def order(**over: object) -> SimOrder:
    values: dict[str, object] = {
        "cloid": "c1",
        "coin": "BTC",
        "side": "long",
        "kind": OrderKind.ALO,
        "px": 100.0,
        "size": 10.0,
        "visible_ms": T0,
    }
    values.update(over)
    return SimOrder(**values)  # type: ignore[arg-type]


def book(**over: object) -> BookState:
    """Asks at 101/102/103, bids at 99/98/97, one unit each, fresh as of T0."""
    values: dict[str, object] = {
        "asks": ((101.0, 1.0), (102.0, 1.0), (103.0, 1.0)),
        "bids": ((99.0, 1.0), (98.0, 1.0), (97.0, 1.0)),
        "snapshot_ms": T0,
        "last_px": 100.0,
        "last_trade_ms": T0,
    }
    values.update(over)
    return BookState(**values)  # type: ignore[arg-type]


class TestLatency:
    def test_an_order_cannot_fill_from_a_trade_one_ms_before_it_is_visible(
        self, settings_factory: SettingsFactory
    ) -> None:
        """The prompt's bar, and the engine's most load-bearing pessimism.

        The edge being measured lives in the seconds after a cascade extreme. An entry that
        fills a millisecond early is measuring a trade nobody could have made.
        """
        settings = settings_factory()
        visible = T0 + settings.backtest.latency_ms
        resting = order(visible_ms=visible)

        early = fill_maker(
            resting,
            trade_px=99.0,
            trade_sz=10.0,
            trade_is_buy=False,
            now_ms=visible - 1,
            settings=settings,
        )
        assert early is None

        on_time = fill_maker(
            resting,
            trade_px=99.0,
            trade_sz=10.0,
            trade_is_buy=False,
            now_ms=visible,
            settings=settings,
        )
        assert on_time is not None

    def test_a_taker_order_is_equally_invisible(self, settings_factory: SettingsFactory) -> None:
        settings = settings_factory()
        resting = order(kind=OrderKind.IOC, visible_ms=T0 + 1000)
        assert fill_taker(resting, book(), now_ms=T0 + 999, settings=settings) is None
        assert fill_taker(resting, book(), now_ms=T0 + 1000, settings=settings) is not None


class TestMaker:
    def test_a_touch_without_trading_through_does_not_fill(
        self, settings_factory: SettingsFactory
    ) -> None:
        """Price reaching our bid is not the same as the queue in front of us clearing."""
        settings = settings_factory()
        resting = order(px=100.0, queue_ahead=50.0)
        fill = fill_maker(
            resting,
            trade_px=100.0,
            trade_sz=1.0,
            trade_is_buy=False,
            now_ms=T0,
            settings=settings,
        )
        assert fill is None
        assert resting.filled == 0.0

    def test_trading_through_fills_the_whole_remainder(
        self, settings_factory: SettingsFactory
    ) -> None:
        """A print below our bid means everything at our price was consumed."""
        settings = settings_factory()
        resting = order(px=100.0, queue_ahead=1e9)
        fill = fill_maker(
            resting,
            trade_px=99.9,
            trade_sz=0.1,
            trade_is_buy=False,
            now_ms=T0,
            settings=settings,
        )
        assert fill is not None
        assert fill.size == 10.0
        assert fill.px == 100.0, "a maker fill is at our price, never better"
        assert fill.maker is True

    def test_the_queue_must_drain_before_anything_reaches_us(
        self, settings_factory: SettingsFactory
    ) -> None:
        settings = settings_factory(**{"backtest.maker_queue_pessimism": 1.0})
        resting = order(px=100.0, queue_ahead=5.0)
        args = {"trade_px": 100.0, "trade_is_buy": False, "now_ms": T0, "settings": settings}

        assert fill_maker(resting, trade_sz=3.0, **args) is None  # type: ignore[arg-type]
        assert fill_maker(resting, trade_sz=2.0, **args) is None  # type: ignore[arg-type]
        # 5 of queue is now exactly used up; the next print is ours.
        fill = fill_maker(resting, trade_sz=4.0, **args)  # type: ignore[arg-type]
        assert fill is not None and fill.size == 4.0

    def test_a_partial_fill_is_the_normal_case_at_our_own_price(
        self, settings_factory: SettingsFactory
    ) -> None:
        settings = settings_factory()
        resting = order(px=100.0, size=10.0, queue_ahead=0.0)
        fill = fill_maker(
            resting,
            trade_px=100.0,
            trade_sz=3.0,
            trade_is_buy=False,
            now_ms=T0,
            settings=settings,
        )
        assert fill is not None and fill.size == 3.0
        assert resting.remaining == 7.0

    def test_queue_pessimism_scales_the_assumed_queue(
        self, settings_factory: SettingsFactory
    ) -> None:
        """The knob that says how much of the displayed size to believe."""
        optimistic = settings_factory(**{"backtest.maker_queue_pessimism": 1.0})
        pessimistic = settings_factory(**{"backtest.maker_queue_pessimism": 3.0})
        args = {"trade_px": 100.0, "trade_sz": 6.0, "trade_is_buy": False, "now_ms": T0}

        easy = order(px=100.0, queue_ahead=5.0)
        assert fill_maker(easy, settings=optimistic, **args) is not None  # type: ignore[arg-type]

        hard = order(px=100.0, queue_ahead=5.0)
        assert fill_maker(hard, settings=pessimistic, **args) is None, (  # type: ignore[arg-type]
            "at 3x pessimism the queue is 15 and a 6 unit print does not reach us"
        )

    def test_a_print_on_our_own_side_never_fills_us(
        self, settings_factory: SettingsFactory
    ) -> None:
        """Our bid is lifted by a seller, not by another buyer."""
        settings = settings_factory()
        resting = order(px=100.0, side="long", queue_ahead=0.0)
        assert (
            fill_maker(
                resting,
                trade_px=99.0,
                trade_sz=10.0,
                trade_is_buy=True,
                now_ms=T0,
                settings=settings,
            )
            is None
        )

    def test_a_short_rests_on_the_offer_and_fills_on_a_buy_through(
        self, settings_factory: SettingsFactory
    ) -> None:
        settings = settings_factory()
        resting = order(px=100.0, side="short", queue_ahead=1e9)
        fill = fill_maker(
            resting,
            trade_px=100.1,
            trade_sz=1.0,
            trade_is_buy=True,
            now_ms=T0,
            settings=settings,
        )
        assert fill is not None and fill.size == 10.0 and fill.is_buy is False


class TestTakerWalk:
    def test_it_vwaps_the_levels_it_consumes(self, settings_factory: SettingsFactory) -> None:
        """2.5 units into asks of 101, 102, 103 at one unit each.

        1 at 101, 1 at 102, 0.5 at 103 = 254.5 over 2.5 = 101.8.
        """
        settings = settings_factory()
        walk = walk_book(book(), side="long", size=2.5, now_ms=T0, settings=settings)
        assert walk is not None
        assert walk.px == pytest.approx(254.5 / 2.5)
        assert walk.levels_consumed == 3
        assert walk.no_book is False

    def test_a_short_walks_the_bids_downward(self, settings_factory: SettingsFactory) -> None:
        """1.5 into bids of 99, 98: 1 at 99 plus 0.5 at 98 = 148 over 1.5."""
        settings = settings_factory()
        walk = walk_book(book(), side="short", size=1.5, now_ms=T0, settings=settings)
        assert walk is not None
        assert walk.px == pytest.approx(148.0 / 1.5)

    def test_a_stale_snapshot_adds_the_configured_slippage(
        self, settings_factory: SettingsFactory
    ) -> None:
        settings = settings_factory()
        late = T0 + int(settings.backtest.stale_book_s * 1000) + 1
        fresh = walk_book(book(), side="long", size=1.0, now_ms=T0, settings=settings)
        stale = walk_book(book(), side="long", size=1.0, now_ms=late, settings=settings)
        assert fresh is not None and stale is not None
        assert stale.px == pytest.approx(fresh.px * (1 + settings.backtest.slip_stale_bps / 1e4))

    def test_exhausting_the_book_fills_the_remainder_rather_than_refusing(
        self, settings_factory: SettingsFactory
    ) -> None:
        """Twenty levels is not the whole book.

        Refusing would flatter the result by dropping exactly the trades that were too big
        for what we can see, which are the ones most likely to have cost something.
        """
        settings = settings_factory()
        walk = walk_book(book(), side="long", size=10.0, now_ms=T0, settings=settings)
        assert walk is not None
        assert walk.exhausted_book is True
        assert walk.size == 10.0
        worst = 103.0 * (1 + settings.backtest.slip_stale_bps / 1e4)
        expected = (101.0 + 102.0 + 103.0 + 7.0 * worst) / 10.0
        assert walk.px == pytest.approx(expected)

    def test_no_book_at_all_uses_the_tape_and_is_counted(
        self, settings_factory: SettingsFactory
    ) -> None:
        """The normal case for the historical year, so it has to be named rather than hidden."""
        settings = settings_factory()
        tape_only = BookState(last_px=100.0, last_trade_ms=T0)
        walk = walk_book(tape_only, side="long", size=5.0, now_ms=T0, settings=settings)
        assert walk is not None
        assert walk.no_book is True
        assert walk.px == pytest.approx(100.0 * (1 + settings.backtest.slip_stale_bps / 1e4))

    def test_no_book_and_no_tape_cannot_be_priced_at_all(
        self, settings_factory: SettingsFactory
    ) -> None:
        """None means no fill, never a guessed one."""
        assert (
            walk_book(BookState(), side="long", size=1.0, now_ms=T0, settings=settings_factory())
            is None
        )

    def test_a_short_gets_the_slippage_in_the_other_direction(
        self, settings_factory: SettingsFactory
    ) -> None:
        settings = settings_factory()
        tape_only = BookState(last_px=100.0, last_trade_ms=T0)
        walk = walk_book(tape_only, side="short", size=1.0, now_ms=T0, settings=settings)
        assert walk is not None
        assert walk.px == pytest.approx(100.0 * (1 - settings.backtest.slip_stale_bps / 1e4))


class TestStops:
    def test_a_long_stop_triggers_when_mid_falls_to_it(self) -> None:
        assert stop_triggered(side="long", stop_px=95.0, mid=95.0)
        assert stop_triggered(side="long", stop_px=95.0, mid=90.0)
        assert not stop_triggered(side="long", stop_px=95.0, mid=95.01)

    def test_a_short_stop_triggers_when_mid_rises_to_it(self) -> None:
        assert stop_triggered(side="short", stop_px=105.0, mid=105.0)
        assert not stop_triggered(side="short", stop_px=105.0, mid=104.99)

    def test_a_gap_through_the_stop_fills_at_the_gapped_price(
        self, settings_factory: SettingsFactory
    ) -> None:
        """The most flattering lie a backtest can tell, and the one this refuses.

        A stop at 95 with the market gapped to 80 does not fill at 95. Filling it there
        would convert every gap into a bounded loss the market never offered, in exactly
        the tail this strategy trades.
        """
        settings = settings_factory()
        stop = order(px=95.0, side="short", kind=OrderKind.IOC, size=1.0, reduce_only=True)
        gapped = gapped_stop_order(stop, mid=80.0)
        assert gapped.px == 80.0
        assert gapped.kind is OrderKind.IOC

        gapped_book = BookState(
            bids=((80.0, 10.0),), asks=((80.1, 10.0),), snapshot_ms=T0, last_px=80.0
        )
        fill = fill_taker(gapped, gapped_book, now_ms=T0, settings=settings)
        assert fill is not None
        assert fill.px == pytest.approx(80.0)
        assert fill.px < 95.0, "the stop price was never available"

    def test_stop_slippage_is_reported_against_the_stop_price(
        self, settings_factory: SettingsFactory
    ) -> None:
        """Reported separately from entry slippage, per the spec."""
        settings = settings_factory()
        stop = order(px=95.0, side="short", kind=OrderKind.IOC, size=1.0)
        gapped_book = BookState(
            bids=((80.0, 10.0),), asks=((80.1, 10.0),), snapshot_ms=T0, last_px=80.0
        )
        fill = fill_taker(stop, gapped_book, now_ms=T0, settings=settings)
        assert fill is not None
        # A sell that got 80 when it asked for 95 is 1578.9 bps worse.
        assert fill.slippage_bps == pytest.approx(-1e4 * (80.0 - 95.0) / 95.0)
        assert fill.slippage_bps > 0, "worse than asked is positive slippage"
