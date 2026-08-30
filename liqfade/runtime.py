"""Process bootstrap shared by every long running command.

Installs uvloop, wires SIGINT and SIGTERM to a cooperative shutdown event, and runs a
main coroutine to completion. Nothing here knows about trading; the kill switch lives in
risk/ and control/ and does not depend on this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from liqfade.log import get_logger

log = get_logger(__name__)

# Set by run_main so any task can await shutdown without being handed the event.
_shutdown: asyncio.Event | None = None


def shutdown_event() -> asyncio.Event:
    """The process-wide shutdown event. Await it in long lived tasks."""
    if _shutdown is None:
        raise RuntimeError("shutdown_event() is only available inside run_main")
    return _shutdown


def uvloop_factory() -> Callable[[], asyncio.AbstractEventLoop] | None:
    """uvloop's loop factory, or None when uvloop is unavailable.

    Passed to asyncio.Runner rather than calling uvloop.install(), which is deprecated
    on 3.12 and mutates global policy that tests then have to undo.
    """
    try:
        import uvloop
    except ImportError:  # pragma: no cover - uvloop is a hard dependency
        return None
    return uvloop.new_event_loop


async def _run[T](
    main: Callable[[], Awaitable[T]] | Coroutine[Any, Any, T],
    grace_s: float,
) -> T:
    global _shutdown
    _shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()

    stop = _shutdown

    def _on_signal(name: str) -> None:
        log.info("shutdown_signal", signal=name)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # not available on Windows
            loop.add_signal_handler(sig, _on_signal, sig.name)

    coro = main() if callable(main) else main
    task = asyncio.ensure_future(coro)
    waiter = asyncio.ensure_future(_shutdown.wait())

    done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    waiter.cancel()

    if task in done:
        return task.result()

    # A signal arrived first: give the main coroutine grace_s to unwind cleanly.
    log.info("shutdown_begin", grace_s=grace_s)
    try:
        return await asyncio.wait_for(task, timeout=grace_s)
    except TimeoutError:
        log.warning("shutdown_forced", grace_s=grace_s)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        raise
    finally:
        _shutdown = None
        log.info("shutdown_done")


def run_main[T](
    main: Callable[[], Awaitable[T]] | Coroutine[Any, Any, T],
    *,
    grace_s: float = 10.0,
) -> T:
    """Run `main` under uvloop with signal handling. Used by every long running command.

    `main` may be a coroutine or a zero argument callable returning one. On SIGINT or
    SIGTERM the shutdown event is set and `main` gets `grace_s` seconds to return before
    it is cancelled.
    """
    with asyncio.Runner(loop_factory=uvloop_factory()) as runner:
        return runner.run(_run(main, grace_s))
