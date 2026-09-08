"""The refusal scorer: one refused intent, one window of tape, one outcome.

Pure and hand-built. The prices here are round numbers rather than a fixture's, because
the whole value of this module is that a reader can check its arithmetic without running
anything, and because it decides whether a filter looks like it is earning its keep.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from liqfade.backtest.shadow import score_refusal
from liqfade.config.models import Settings
from liqfade.strategy.engine import Rejection

SettingsFactory = Callable[..., Settings]

START = 1_700_000_000_000
# A long fade: in at 100, out at 101, stopped at 99. One dollar each way on a round number.
REFUSAL = Rejection(
    coin="ETH",
    side="long",
    ts_ms=START,
    reason="spread_too_wide",
    stage="filter",
    entry_px=100.0,
    target_px=101.0,
    stop_px=99.0,
    time_stop_s=60.0,
    urgency="normal",
    explain={},
)


@pytest.fixture
def settings(settings_factory: SettingsFactory) -> Settings:
    return settings_factory(**{"backtest.latency_ms": 1000})


def score(prints: list[tuple[int, float]], settings: Settings, **over: object) -> object:
    from dataclasses import replace

    refusal = replace(REFUSAL, **over)  # type: ignore[arg-type]
    return score_refusal(refusal, prints, settings, equity=10_000.0)


def test_the_target_is_hit_and_the_pnl_is_net_of_both_taker_fees(settings: Settings) -> None:
    """1% of notional gross, minus two taker legs. Nothing here reports a gross number."""
    outcome = score([(START + 1000, 100.0), (START + 5000, 101.0)], settings)

    assert outcome.reason == "target"  # type: ignore[attr-defined]
    assert outcome.entry_px == 100.0  # type: ignore[attr-defined]
    assert outcome.exit_px == 101.0  # type: ignore[attr-defined]
    # risk_frac * equity / |entry - stop| * entry: the risk-budget rule before any cap.
    expected = 10_000.0 * settings.sizing.risk_frac / 1.0 * 100.0
    assert outcome.notional_usd == pytest.approx(expected)  # type: ignore[attr-defined]
    assert outcome.gross_pnl == pytest.approx(expected * 0.01)  # type: ignore[attr-defined]
    assert outcome.fees > 0 and outcome.net_pnl < outcome.gross_pnl  # type: ignore[attr-defined]


def test_the_stop_is_hit_and_the_loss_is_reported(settings: Settings) -> None:
    outcome = score([(START + 1000, 100.0), (START + 4000, 98.5)], settings)

    assert outcome.reason == "stop"  # type: ignore[attr-defined]
    assert outcome.net_pnl < 0  # type: ignore[attr-defined]


def test_both_touched_in_one_second_resolves_as_the_stop(settings: Settings) -> None:
    """The grid convention is one second, one tick, and inside it the stop is first.

    A counterfactual that took the target here would flatter every refusal on a volatile
    second, which is exactly the argument for deleting a filter that was earning its keep.
    """
    prints = [(START + 1000, 100.0), (START + 4100, 101.2), (START + 4900, 98.0)]

    assert score(prints, settings).reason == "stop"  # type: ignore[attr-defined]


def test_a_target_and_a_stop_in_different_seconds_take_whichever_came_first(
    settings: Settings,
) -> None:
    """The other half, so the rule above is about the second and not about the stop."""
    prints = [(START + 1000, 100.0), (START + 4100, 101.2), (START + 5900, 98.0)]

    assert score(prints, settings).reason == "target"  # type: ignore[attr-defined]


def test_the_time_stop_exits_at_the_first_print_past_it(settings: Settings) -> None:
    prints = [(START + 1000, 100.0), (START + 30_000, 100.4), (START + 62_000, 100.6)]
    outcome = score(prints, settings)

    assert outcome.reason == "timeout"  # type: ignore[attr-defined]
    assert outcome.exit_px == 100.6  # type: ignore[attr-defined]


def test_a_print_before_the_fill_time_cannot_fill(settings: Settings) -> None:
    """No lookahead: the order is not at the venue until the latency has elapsed.

    The 100.0 print at +999ms is invisible; the fill is the 103.0 print that follows it,
    which is a worse entry, which is the honest one.
    """
    outcome = score([(START + 999, 100.0), (START + 1500, 103.0)], settings)

    assert outcome.entry_ms == START + 1500  # type: ignore[attr-defined]
    assert outcome.entry_px == 103.0  # type: ignore[attr-defined]


def test_a_window_with_no_prints_is_no_tape_rather_than_a_zero(settings: Settings) -> None:
    """A refusal the lake cannot answer for is not a refusal that would have broken even."""
    assert score([], settings).reason == "no_tape"  # type: ignore[attr-defined]
    assert score([(START - 5000, 100.0)], settings).reason == "no_tape"  # type: ignore[attr-defined]


def test_a_window_that_ends_before_an_exit_is_no_tape_too(settings: Settings) -> None:
    """It filled and then the tape stopped. Reporting the last price would invent an exit."""
    prints = [(START + 1000, 100.0), (START + 20_000, 100.2)]

    assert score(prints, settings).reason == "no_tape"  # type: ignore[attr-defined]


def test_a_short_reads_its_target_below_the_entry(settings: Settings) -> None:
    """Sign errors here would report every short backwards."""
    prints = [(START + 1000, 100.0), (START + 5000, 99.0)]
    outcome = score(prints, settings, side="short", target_px=99.0, stop_px=101.0)

    assert outcome.reason == "target"  # type: ignore[attr-defined]
    assert outcome.net_pnl > 0  # type: ignore[attr-defined]
