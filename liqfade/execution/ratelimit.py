"""The address action budget: how many venue calls this account may make in an hour.

docs/HYPERLIQUID.md: Hyperliquid meters actions per address, cumulatively, across orders,
cancels and modifies alike. `execution.action_budget_per_hour` is our own ceiling under
that, and it exists for one reason: the entry policy can reprice, and a loop that reprices
freely on twenty coins will spend an allowance that the kill path needs later.

**Reducing risk is counted and never refused.** A cancel, a flatten and every leg of the
kill sweep pass whatever the budget says. CLAUDE.md rule 5 promises five seconds with no
excuses, and a limiter able to block a cancel is a limiter able to hold a position open,
which is the opposite of what it is for. They still count, so the number the panel renders
is the truth about what has been spent rather than a flattering subset of it.

The window slides. A fixed window would let the whole budget be spent at 59 minutes and
again at 61, and the venue would be the thing that noticed.
"""

from __future__ import annotations

from collections import deque

__all__ = ["ActionBudget", "BudgetExhaustedError"]

HOUR_MS = 3_600_000


class BudgetExhaustedError(RuntimeError):
    """This address has spent its hourly allowance. Not a venue error; ours, before one."""


class ActionBudget:
    """A sliding one hour count of venue actions, and the ceiling on it."""

    def __init__(self, per_hour: int) -> None:
        """
        Args:
            per_hour: `execution.action_budget_per_hour`. Actions allowed in any 60
                minute window.
        """
        self.per_hour = per_hour
        self._taken: deque[int] = deque()

    def _prune(self, now_ms: int) -> None:
        cutoff = now_ms - HOUR_MS
        while self._taken and self._taken[0] <= cutoff:
            self._taken.popleft()

    def remaining(self, *, now_ms: int) -> int:
        """Actions left in the current window. What `RiskState.actions_remaining` shows."""
        self._prune(now_ms)
        return max(self.per_hour - len(self._taken), 0)

    def take(self, *, now_ms: int, reducing: bool = False) -> None:
        """Spend one action, or refuse.

        Args:
            now_ms: wall clock.
            reducing: True for a cancel, a flatten or a kill leg. Counted, never refused.

        Raises:
            BudgetExhaustedError: the window is full and this action would add risk.
        """
        self._prune(now_ms)
        if not reducing and len(self._taken) >= self.per_hour:
            raise BudgetExhaustedError(
                f"action budget spent: {len(self._taken)} actions in the last hour, "
                f"limit {self.per_hour}"
            )
        self._taken.append(now_ms)
