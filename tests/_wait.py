"""Waiting on a background task, rather than guessing how long it takes.

One copy for the whole suite. The same sleep-then-assert shape was in six test files,
and a helper per file is six chances for one of them to drift to a timeout too short for
a loaded machine. Imported as `from _wait import eventually`, the way `strategy_rows`
already is: pytest puts `tests/` on the path because there is no `__init__.py` in it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


async def eventually(what: str, condition: Callable[[], bool], timeout_s: float = 5.0) -> None:
    """Wait for a background loop to get there, rather than guessing how long it takes.

    A fixed sleep is a bet on the scheduler: on a loaded machine the task under test has
    not run yet when the assertion fires, and the test fails on a run that was only late.
    Waiting on the condition itself is slow only when the code is wrong.
    """
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout_s}s waiting for {what}")
        await asyncio.sleep(0.001)
