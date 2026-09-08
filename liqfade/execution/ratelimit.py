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

__all__ = ["HALT_REASON", "ActionBudget", "BudgetExhaustedError", "rate_limited"]

HOUR_MS = 3_600_000
# The halt both the local budget and the venue's own refusal produce. One reason string,
# because docs/RUNBOOK.md has one row for it and an owner reading Telegram has one thing
# to do about it either way: wait for the window to slide.
HALT_REASON = "rate_limit"
# docs/HYPERLIQUID.md: "Too many cumulative requests sent. Place taker orders to free up 1
# request per USDC traded". Matched on the stable half of the sentence.
VENUE_MARKER = "too many cumulative requests"


def rate_limited(reason: str) -> bool:
    """Whether a refusal means the address is out of requests, ours or the venue's."""
    lowered = reason.lower()
    return VENUE_MARKER in lowered or "action budget spent" in lowered


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

    def free_at_ms(self) -> int | None:
        """When the oldest action leaves the window, or None while nothing is counted.

        What a `rate_limit` halt expires at. The window slides, so the halt has an end and
        does not need a person to clear it: docs/RUNBOOK.md says wait, and this is the
        instant there is something to wait for.
        """
        return self._taken[0] + HOUR_MS if self._taken else None

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
