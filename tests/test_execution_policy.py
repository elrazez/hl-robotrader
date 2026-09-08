"""The entry policy: one decision table, used by the backtest and by the live manager.

docs/STRATEGY_AND_RISK.md section 3 describes `Alo` at the touch with a TTL, a bounded
number of reprices and an `Ioc` fallback. `backtest/engine.py` has implemented that since
Phase 4 and the live path has not, so the risk this file exists to remove is the two
drifting apart: a maker fill rate the backtest reports and the bot never achieves.
"""

from __future__ import annotations

import pytest

from liqfade.execution.policy import EntryAction, entry_action, touch_price


def _settings(ttl_s: float = 5.0, max_reprices: int = 2):
    import pathlib
    import tempfile

    from tests.test_live_loop import paper_settings

    return paper_settings(
        pathlib.Path(tempfile.mkdtemp()),
        **{"execution.entry_ttl_s": ttl_s, "execution.max_reprices": max_reprices},
    )


def test_a_young_entry_is_held() -> None:
    settings = _settings(ttl_s=5.0)
    assert (
        entry_action(now_ms=4_999, visible_ms=0, reprices=0, settings=settings) is EntryAction.HOLD
    )


def test_an_entry_at_exactly_its_ttl_is_repriced() -> None:
    """The backtest's boundary is `now < visible + ttl` holds, so `>=` acts. Same here."""
    settings = _settings(ttl_s=5.0)
    assert (
        entry_action(now_ms=5_000, visible_ms=0, reprices=0, settings=settings)
        is EntryAction.REPRICE
    )


def test_the_last_allowed_reprice_still_reprices() -> None:
    settings = _settings(ttl_s=5.0, max_reprices=2)
    assert (
        entry_action(now_ms=9_000, visible_ms=0, reprices=1, settings=settings)
        is EntryAction.REPRICE
    )


def test_past_max_reprices_the_entry_is_abandoned() -> None:
    """Not cancelled and replaced again: the cascade it was chasing is over."""
    settings = _settings(ttl_s=5.0, max_reprices=2)
    assert (
        entry_action(now_ms=9_000, visible_ms=0, reprices=2, settings=settings)
        is EntryAction.ABANDON
    )


def test_max_reprices_zero_abandons_at_the_first_expiry() -> None:
    settings = _settings(ttl_s=5.0, max_reprices=0)
    assert (
        entry_action(now_ms=5_000, visible_ms=0, reprices=0, settings=settings)
        is EntryAction.ABANDON
    )


@pytest.mark.parametrize(
    ("is_buy", "bid", "ask", "expected"),
    [(True, 100.0, 101.0, 100.0), (False, 100.0, 101.0, 101.0)],
)
def test_the_touch_is_the_side_that_rests(is_buy, bid, ask, expected) -> None:
    """A buy rests on the bid and a sell on the offer. Crossing is what Alo refuses."""
    assert touch_price(is_buy=is_buy, bid=bid, ask=ask) == expected


def test_a_missing_side_has_no_touch() -> None:
    """No book, no reprice. The backtest holds the order rather than inventing a price."""
    assert touch_price(is_buy=True, bid=None, ask=101.0) is None


def test_the_backtest_engine_runs_this_exact_policy() -> None:
    """The pin CLAUDE.md's first design constraint asks for, asserted rather than assumed.

    `backtest/engine.py._expire_entries` and `OrderManager.reprice` are the two callers of
    this table. They share the function, so they cannot disagree, and this test is what
    would notice if somebody re-inlined the rule into one of them: it drives the real
    engine over a clock and asserts the transitions match what `entry_action` says.
    """
    from liqfade.backtest.engine import _CoinState, _expire_entries
    from liqfade.backtest.fills import OrderKind, SimOrder
    from liqfade.backtest.portfolio import Portfolio

    settings = _settings(ttl_s=2.0, max_reprices=2)
    portfolio = Portfolio(settings, starting_equity=10_000.0)
    coin_state = _CoinState()
    coin_state.entry = SimOrder(
        cloid="0xabc",
        coin="ETH",
        side="long",
        kind=OrderKind.ALO,
        px=100.0,
        size=1.0,
        visible_ms=0,
    )
    state = {"ETH": coin_state}

    seen = []
    for now in (1_000, 2_000, 4_500, 7_000, 9_500):
        expected = entry_action(
            now_ms=now,
            visible_ms=coin_state.entry.visible_ms if coin_state.entry else 0,
            reprices=coin_state.reprices,
            settings=settings,
        )
        _expire_entries(now, state, portfolio, None, settings)
        seen.append((expected, coin_state.reprices, coin_state.entry is None))

    actions = [action for action, _, _ in seen]
    assert actions == [
        EntryAction.HOLD,  # 1.0s in, under the 2.0s ttl
        EntryAction.REPRICE,  # exactly at the ttl
        EntryAction.REPRICE,  # second and last allowed move
        EntryAction.ABANDON,  # two moves spent
        EntryAction.ABANDON,
    ]
    assert seen[2][1] == 2, "both reprices should have been counted by the engine"
    assert seen[3][2] is True, "the engine should have dropped the entry on ABANDON"
    assert portfolio.abandoned_entries == 1
