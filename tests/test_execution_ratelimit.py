"""The action budget: how many venue calls this address may make in an hour.

docs/HYPERLIQUID.md: the address limit is cumulative and shared by every action an
account sends, so a loop that reprices freely can spend the budget that the kill path
needs. `execution.action_budget_per_hour` has been in config since Phase 0 and read by
nothing, and `RiskState.actions_remaining` has been rendered by the panel as a dash.
"""

from __future__ import annotations

import pytest

from liqfade.execution.ratelimit import ActionBudget, BudgetExhaustedError


def test_a_fresh_budget_has_all_of_it() -> None:
    budget = ActionBudget(per_hour=10)
    assert budget.remaining(now_ms=0) == 10


def test_each_action_spends_one() -> None:
    budget = ActionBudget(per_hour=10)
    for expected in (9, 8, 7):
        budget.take(now_ms=1_000)
        assert budget.remaining(now_ms=1_000) == expected


def test_the_window_slides_rather_than_resetting() -> None:
    """A fixed window lets a burst of 2x the budget straddle the boundary.

    The venue's limit is not a fixed window, so ours must not be either: spending the
    whole budget at 59 minutes and again at 61 would be 2x the allowance in two minutes,
    and the venue would be the thing that noticed.
    """
    budget = ActionBudget(per_hour=3)
    for _ in range(3):
        budget.take(now_ms=0)
    assert budget.remaining(now_ms=3_599_000) == 0
    # The first action ages out exactly an hour after it was taken, not before.
    assert budget.remaining(now_ms=3_600_001) == 3


def test_an_exhausted_budget_refuses() -> None:
    budget = ActionBudget(per_hour=1)
    budget.take(now_ms=0)
    with pytest.raises(BudgetExhaustedError, match="action budget"):
        budget.take(now_ms=1_000)


def test_reducing_risk_is_never_refused() -> None:
    """A cancel and a flatten must go out even with the budget spent.

    CLAUDE.md rule 5 gives the kill switch five seconds and no excuses. A budget that can
    block a cancel is a budget that can hold a position open, which inverts what the limit
    is for: it exists to stop the loop spending the allowance on new risk.
    """
    budget = ActionBudget(per_hour=1)
    budget.take(now_ms=0)
    budget.take(now_ms=1_000, reducing=True)
    assert budget.remaining(now_ms=1_000) == 0, "still counted, so the panel tells the truth"


def test_the_count_is_what_the_panel_renders() -> None:
    """`RiskState.actions_remaining` is this number and nothing else."""
    budget = ActionBudget(per_hour=600)
    for _ in range(17):
        budget.take(now_ms=5_000)
    assert budget.remaining(now_ms=5_000) == 583
