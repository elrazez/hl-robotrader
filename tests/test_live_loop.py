"""The paper process: the control backend, the kill wiring and the refusals.

The streams are never started here. What is tested is everything the loop owns around
them: what `/status` reports, what `inject_intent` refuses, that the kill switch reaches
the session, and that a kill flag already on disk stops the loop before it trades.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from liqfade.config import resolve
from liqfade.config.models import Settings
from liqfade.control import ControlClient, ControlServer
from liqfade.control.protocol import InjectResult
from liqfade.live.loop import PaperLoop

COIN = "ETH"
START = 1_700_000_000_000


def paper_settings(tmp_path: Path, **dotted: Any) -> Settings:
    merged, _ = resolve("paper")
    merged["paths"]["data_dir"] = str(tmp_path)
    # What the unit sets with LIQFADE_CASCADE__TRIGGER. config/paper.yaml leaves the
    # trigger alone because it is also the backtest's base.
    merged["cascade"]["trigger"] = "crossvenue"
    for key, value in dotted.items():
        node = merged
        parts = key.split(".")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
    return Settings.model_validate(merged)


@pytest.fixture
async def loop(tmp_path: Path) -> AsyncIterator[PaperLoop]:
    paper = PaperLoop(paper_settings(tmp_path), starting_equity=5_000.0)
    await paper.setup()
    try:
        yield paper
    finally:
        await paper.aclose()


def a_market(loop: PaperLoop, px: float = 3000.0, at: int = START) -> None:
    loop.sink(
        "bbo",
        "hyperliquid",
        {
            "coin": COIN,
            "ts_ms": at,
            "rx_ms": at,
            "bid_px": px - 0.5,
            "bid_sz": 50.0,
            "ask_px": px + 0.5,
            "ask_sz": 50.0,
        },
    )
    loop.sink(
        "trades",
        "hyperliquid",
        {
            "coin": COIN,
            "ts_ms": at,
            "rx_ms": at,
            "px": px,
            "sz": 5.0,
            "side": "sell",
            "tid": 1,
        },
    )


async def test_status_reports_paper_and_never_live_trading(loop: PaperLoop) -> None:
    from liqfade.live.loop import PaperBackend

    state = await PaperBackend(loop).status()

    assert state.proc == "paper"
    assert state.env == "paper"
    assert state.live_trading is False
    assert state.risk.equity == pytest.approx(5_000.0)


async def test_pause_drops_new_intents_and_still_ticks(loop: PaperLoop) -> None:
    """Pause is about new risk. The tick itself has to keep running: it is the only path
    to the exit checks, so a loop that stops ticking stops honouring its own stops."""
    from liqfade.control.protocol import now_ms
    from liqfade.live.loop import PaperBackend

    backend = PaperBackend(loop)
    # Stamped on the wall clock, because `tick_loop` reads it. The feature engine buckets
    # on rx_ms, so a fixture stamped years ago would have one tick walk every second in
    # between.
    a_market(loop, at=now_ms())
    await backend.pause(reason="test")

    stop = asyncio.Event()
    task = asyncio.create_task(loop.tick_loop(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await task

    assert loop.ticks > 0
    assert loop.session.intents == []
    # The tick reached the engine rather than being skipped, which is what keeps the
    # exit checks alive. `_on_tick` samples equity, so a sample is the evidence.
    assert loop.session.result.equity_curve


async def test_pause_does_not_abandon_the_stop_on_a_position_already_open(
    tmp_path: Path,
) -> None:
    """The defect this replaces: a paused loop with a position stopped checking its stop.

    `_on_tick` is the only path to `_check_exits` and `_expire_entries`, so skipping the
    tick while paused leaves an open position with no stop and no time stop, which is the
    opposite of what a pause is for.
    """
    from liqfade.control.protocol import now_ms
    from liqfade.live.loop import PaperBackend

    paper = PaperLoop(
        paper_settings(tmp_path, **{"execution.entry_tif": "Ioc"}), starting_equity=5_000.0
    )
    await paper.setup()
    try:
        at = now_ms()
        a_market(paper, 3000.0, at=at)
        await paper.session.tick(at + 1_000)
        paper.session.inject_intent(coin=COIN, side="long", now_ms=at + 1_000, stop_bps=30.0)
        # The entry is visible one latency later, and the book has to be fresh when it
        # crosses: a stale book is a guess, and the fill model refuses to fill on one.
        a_market(paper, 3000.0, at=at + 2_900)
        await paper.session.tick(at + 3_000)
        assert COIN in paper.session.portfolio.positions

        await PaperBackend(paper).pause(reason="drill")
        # Through the stop at 3000 * (1 - 0.003) = 2991.
        a_market(paper, 2_900.0, at=at + 3_500)

        stop = asyncio.Event()
        task = asyncio.create_task(paper.tick_loop(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await task

        assert COIN not in paper.session.portfolio.positions
        assert [t.exit_reason for t in paper.session.portfolio.trades] == ["stop"]
    finally:
        await paper.aclose()


async def test_inject_intent_over_the_control_socket(loop: PaperLoop, sock_dir: Path) -> None:
    """The drill, driven exactly as the owner would drive it."""
    from liqfade.live.loop import PaperBackend

    a_market(loop)
    await loop.session.tick(START + 1_000)

    server = ControlServer(PaperBackend(loop), sock_dir / "control.sock")
    await server.start()
    try:
        client = ControlClient(server.socket_path, connect_timeout_s=2.0, request_timeout_s=5.0)
        result = await client.send("inject_intent", coin=COIN, side="long")
    finally:
        await server.stop()

    assert isinstance(result, InjectResult)
    assert result.injected is True
    assert result.coin == COIN
    assert result.entry_px == pytest.approx(3000.0)


async def test_inject_intent_needs_a_side_it_understands(loop: PaperLoop) -> None:
    from liqfade.live.loop import PaperBackend

    result = await PaperBackend(loop).inject_intent(coin=COIN, side="sideways")
    assert result.injected is False
    assert "long|short" in (result.reason or "")


async def test_kill_stops_the_loop_as_well_as_flattening_it(tmp_path: Path) -> None:
    """CLAUDE.md rule 5 says stop the loop. A process that keeps ticking was not killed."""
    paper = PaperLoop(paper_settings(tmp_path))
    stop = asyncio.Event()
    await paper.setup(stop)
    try:
        await paper.kill.fire(reason="test")
        assert stop.is_set()
    finally:
        await paper.aclose()


async def test_kill_over_the_backend_writes_the_flag_and_flattens(loop: PaperLoop) -> None:
    from liqfade.live.loop import PaperBackend

    a_market(loop)
    await loop.session.tick(START + 1_000)
    loop.session.inject_intent(coin=COIN, side="long", now_ms=START + 1_000)
    await loop.session.tick(START + 1_250)
    assert await loop.session.open_order_cloids()

    result = await PaperBackend(loop).kill(reason="test drill")

    assert result.killed is True
    assert loop.settings.kill_file.exists()
    assert await loop.session.open_order_cloids() == []


async def test_the_loop_refuses_to_start_while_the_kill_flag_exists(tmp_path: Path) -> None:
    """The unit's ExecStartPre catches this too. A manual start bypasses the unit."""
    (tmp_path / "KILL").write_text("killed_at: earlier\n")
    paper = PaperLoop(paper_settings(tmp_path))
    stop = asyncio.Event()

    await paper.run(stop)

    assert paper.ticks == 0


async def test_reconcile_is_clean_when_the_registry_matches_the_engine(
    loop: PaperLoop,
) -> None:
    a_market(loop)
    await loop.session.tick(START + 1_000)
    loop.session.inject_intent(coin=COIN, side="long", now_ms=START + 1_000)
    await loop.session.tick(START + 1_250)

    result = await loop.reconciler.check("test")

    assert result is not None and result.clean


async def test_reconcile_reports_a_registry_row_the_engine_does_not_have(
    loop: PaperLoop,
) -> None:
    """The mirror drifting from the book it mirrors is the one thing paper can reconcile."""
    from liqfade.execution.orders import Order, OrderIntent, OrderStatus

    assert loop.store is not None
    await loop.store.upsert(
        Order(
            intent=OrderIntent(
                cloid="0x" + "1" * 32, coin=COIN, is_buy=True, px=1.0, size=1.0, tif="Alo"
            ),
            status=OrderStatus.ACKED,
        )
    )

    result = await loop.reconciler.check("test")

    assert result is not None and [d.kind for d in result.differences] == ["missing_order"]
    assert loop.counters()["reconcile_differences"] == 1


async def test_paper_never_reports_its_own_position_as_one_the_venue_lost(
    loop: PaperLoop,
) -> None:
    """Paper has no venue account, so the position half has no second source at all.

    Comparing it against nothing would call every open position `missing_position`, which
    is a HALT, every sixty seconds. Not checked is the honest answer.
    """
    a_market(loop)
    await loop.session.tick(START + 1_000)
    loop.session.inject_intent(coin=COIN, side="long", now_ms=START + 1_000)
    await loop.session.tick(START + 1_250)

    result = await loop.reconciler.check("test")

    assert result is not None
    assert result.checked_positions == 0
    assert not [d for d in result.differences if "position" in d.kind]


async def test_a_stream_reconnect_reconciles_without_waiting_for_the_interval(
    loop: PaperLoop,
) -> None:
    """CLAUDE.md rule 4: reconciliation runs on every WebSocket reconnect.

    The loop counted reconnects and triggered nothing, while the class that implements
    the trigger was used by nothing but its own tests.
    """
    assert loop.streams and all(stream.on_reconnect is not None for stream in loop.streams)

    stop = asyncio.Event()
    task = asyncio.create_task(loop.reconciler.run(stop, reconnected=loop.reconnected))
    try:
        await asyncio.sleep(0.02)
        assert loop.reconciler.runs == 1  # startup

        loop.streams[0].on_reconnect()  # what WSStream.run calls when it comes back
        await asyncio.sleep(0.02)

        assert loop.reconciler.runs == 2
    finally:
        stop.set()
        await task


async def test_the_heartbeat_file_is_written(loop: PaperLoop) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(loop.heartbeat_loop(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await task

    assert loop.settings.heartbeat_path.exists()
    assert int(loop.settings.heartbeat_path.read_text()) > 0


async def test_the_counters_are_what_the_daily_report_reads(loop: PaperLoop) -> None:
    counters = loop.counters()
    assert set(counters) >= {"ticks", "intents", "fills", "trades", "reconcile_differences"}
