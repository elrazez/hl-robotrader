"""The paper process: the control backend, the kill wiring and the refusals.

The streams are never started here. What is tested is everything the loop owns around
them: what `/status` reports, what `inject_intent` refuses, that the kill switch reaches
the session, and that a kill flag already on disk stops the loop before it trades.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from liqfade.backtest.report import config_hash
from liqfade.config import resolve
from liqfade.config.models import Settings
from liqfade.control import ControlClient, ControlServer
from liqfade.control.protocol import InjectResult
from liqfade.execution.reconcile import Reconciler
from liqfade.live.loop import PaperBackend, PaperLoop

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


async def reconciles(reconciler: Reconciler, n: int, timeout_s: float = 5.0) -> None:
    """Wait for the reconciler's own signal that it has finished `n` comparisons."""
    try:
        async with asyncio.timeout(timeout_s):
            while reconciler.runs < n:
                await reconciler.ran.wait()
                reconciler.ran.clear()
    except TimeoutError:
        raise AssertionError(
            f"the reconciler ran {reconciler.runs} times in {timeout_s}s, wanted {n}"
        ) from None


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
    await eventually("a tick", lambda: loop.ticks > 0)
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
        await eventually("the stop to fire", lambda: COIN not in paper.session.portfolio.positions)
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
        await reconciles(loop.reconciler, 1)
        assert loop.reconciler.runs == 1  # startup

        loop.streams[0].on_reconnect("hyperliquid")  # what WSStream.run calls on the way back
        await reconciles(loop.reconciler, 2)

        assert loop.reconciler.runs == 2
    finally:
        stop.set()
        await task


async def test_the_heartbeat_file_is_written(loop: PaperLoop) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(loop.heartbeat_loop(stop))
    await eventually("the first beat", loop.settings.heartbeat_path.exists)
    stop.set()
    await task

    assert loop.settings.heartbeat_path.exists()
    assert int(loop.settings.heartbeat_path.read_text()) > 0


async def test_the_counters_are_what_the_daily_report_reads(loop: PaperLoop) -> None:
    counters = loop.counters()
    assert set(counters) >= {"ticks", "intents", "fills", "trades", "reconcile_differences"}


async def test_a_risk_block_records_the_coin_it_blocked(loop: PaperLoop) -> None:
    """A count keyed on the reason alone cannot say which coin was refused."""
    a_market(loop)
    await loop.session.tick(START + 1_000)
    # Six seconds after the last HL row, past risk.stale_coin_s of five.
    loop.session.inject_intent(coin=COIN, side="long", now_ms=START + 7_000)

    assert loop.session.result.risk_blocks == {"stale_coin_data": 1}
    assert [
        (coin, gate, reason) for _ts, coin, gate, reason in loop.session.result.recent_blocks
    ] == [(COIN, "risk", "stale_coin_data")]


async def test_a_risk_block_is_logged_and_posted_once_per_reason(
    loop: PaperLoop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A silent refusal in the live process is a strategy that looks like it stopped."""
    import structlog

    posted: list[str] = []

    async def fake_post(text: str) -> bool:
        posted.append(text)
        return True

    monkeypatch.setattr(loop, "post", fake_post)
    a_market(loop)
    await loop.session.tick(START + 1_000)
    for n in range(3):
        loop.session.inject_intent(coin=COIN, side="long", now_ms=START + 7_000 + n)

    with structlog.testing.capture_logs() as logs:
        await loop.report_risk_blocks()

    events = [entry for entry in logs if entry["event"] == "risk_block"]
    assert len(events) == 3  # every one is logged
    assert events[0]["reason"] == "stale_coin_data"
    assert events[0]["coin"] == COIN
    # And posted once, because three identical refusals in one second are one thing to say.
    assert len(posted) == 1
    assert "stale_coin_data" in posted[0]
    assert COIN in posted[0]
    # The counter the report reads is untouched.
    assert loop.session.result.risk_blocks["stale_coin_data"] == 3


async def test_the_same_risk_reason_is_not_posted_again_inside_its_window(
    loop: PaperLoop, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted: list[str] = []

    async def fake_post(text: str) -> bool:
        posted.append(text)
        return True

    monkeypatch.setattr(loop, "post", fake_post)
    a_market(loop)
    await loop.session.tick(START + 1_000)

    loop.session.inject_intent(coin=COIN, side="long", now_ms=START + 7_000)
    await loop.report_risk_blocks()
    loop.session.inject_intent(coin=COIN, side="long", now_ms=START + 8_000)
    await loop.report_risk_blocks()

    assert len(posted) == 1


async def test_two_identical_simulated_fills_are_both_posted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`loop.post` used to send every line through the ten minute dedupe.

    Two identical fills inside ten minutes are two trades, and the second one being
    swallowed loses it from the only record the owner watches in real time.
    """
    import liqfade.live.loop as loop_module

    sent: list[tuple[str, bool]] = []

    async def fake_send(text: str, *, timeout_s: float = 10.0, dedupe: bool = True) -> bool:
        sent.append((text, dedupe))
        return True

    monkeypatch.setattr(loop_module, "send_alert", fake_send)
    paper = PaperLoop(paper_settings(tmp_path, **{"telegram.enabled": True}))
    await paper.setup()
    try:
        paper.session.fills.append((START, COIN, "entry", 3000.0, 0.1))
        paper.session.fills.append((START + 1, COIN, "entry", 3000.0, 0.1))

        posted = await paper._post_fills(0)

        assert posted == 2
        assert len(sent) == 2
        assert sent[0][0] == sent[1][0]  # identical text, both sent
        assert all(dedupe is False for _text, dedupe in sent)
    finally:
        await paper.aclose()


async def test_a_recorder_style_condition_alert_keeps_its_dedupe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Everything that is not an event still repeats the same words every tick it holds."""
    import liqfade.live.loop as loop_module

    sent: list[tuple[str, bool]] = []

    async def fake_send(text: str, *, timeout_s: float = 10.0, dedupe: bool = True) -> bool:
        sent.append((text, dedupe))
        return True

    monkeypatch.setattr(loop_module, "send_alert", fake_send)
    paper = PaperLoop(paper_settings(tmp_path, **{"telegram.enabled": True}))
    await paper.setup()
    try:
        await paper.post("[paper] RECONCILE interval: missing_order")

        assert sent == [("[paper] RECONCILE interval: missing_order", True)]
    finally:
        await paper.aclose()


def _recorder(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool]]:
    """Capture what the loop would have posted to Telegram."""
    import liqfade.live.loop as loop_module

    sent: list[tuple[str, bool]] = []

    async def fake_send(text: str, *, timeout_s: float = 10.0, dedupe: bool = True) -> bool:
        sent.append((text, dedupe))
        return True

    monkeypatch.setattr(loop_module, "send_alert", fake_send)
    return sent


async def test_a_stream_reconnect_is_posted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLAUDE.md section 5 lists reconnect among the things that go to Telegram."""
    sent = _recorder(monkeypatch)
    paper = PaperLoop(paper_settings(tmp_path, **{"telegram.enabled": True}))
    await paper.setup()
    try:
        paper._on_reconnect("hyperliquid")
        await eventually(
            "the reconnect post",
            lambda: any("RECONNECT" in text and "hyperliquid" in text for text, _ in sent),
        )

        assert any("RECONNECT" in text and "hyperliquid" in text for text, _ in sent)
    finally:
        await paper.aclose()


async def test_a_tick_that_raises_is_posted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error the loop swallowed into a log line is an error nobody sees."""
    sent = _recorder(monkeypatch)
    paper = PaperLoop(paper_settings(tmp_path, **{"telegram.enabled": True}))
    await paper.setup()
    try:

        async def boom(*_args: object, **_kwargs: object) -> list[Any]:
            raise RuntimeError("the book fell over")

        monkeypatch.setattr(paper.session, "tick", boom)
        stop = asyncio.Event()
        task = asyncio.create_task(paper.tick_loop(stop))
        await eventually(
            "the error post",
            lambda: any("ERROR" in text and "the book fell over" in text for text, _ in sent),
        )
        stop.set()
        await task

        assert any("ERROR" in text and "the book fell over" in text for text, _ in sent)
    finally:
        await paper.aclose()


async def test_a_halt_is_posted_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _recorder(monkeypatch)
    paper = PaperLoop(paper_settings(tmp_path, **{"telegram.enabled": True}))
    await paper.setup()
    try:
        paper.session.risk.state.halt_reason = "drawdown"
        await paper.report_halt()
        await paper.report_halt()

        halts = [text for text, _ in sent if "HALT" in text]
        assert len(halts) == 1
        assert "drawdown" in halts[0]
    finally:
        await paper.aclose()


async def test_inject_intent_is_refused_when_live_trading_is_armed(
    loop: PaperLoop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`env != mainnet` is not the whole gate. An armed box is not a drill box.

    The drill opens a position with no signal behind it. Doing that on a process that has
    been told live trading is on is exactly the mistake the three gates exist to stop,
    whatever the env file happens to say.
    """
    monkeypatch.setenv("LIQFADE_LIVE_TRADING", "1")
    a_market(loop)
    await loop.session.tick(START + 1_000)

    from liqfade.live.loop import PaperBackend

    result = await PaperBackend(loop).inject_intent(coin=COIN, side="long")

    assert result.injected is False
    assert "LIQFADE_LIVE_TRADING" in (result.reason or "")


async def test_a_paper_fill_is_written_to_the_fills_table(loop: PaperLoop) -> None:
    """Paper writes the registry a testnet run writes, or it is drilling something else.

    The bare `OrderStore.upsert` wrote the orders table and nothing else, so `data/
    orders.sqlite` had no fills in it at all and the panel's fills history, which is one of
    the things Phase 5 is supposed to be exercising, had nothing to show.
    """
    from liqfade.control.protocol import now_ms

    assert loop.store is not None
    from liqfade.live.loop import PaperBackend

    at = now_ms()
    a_market(loop, 3000.0, at=at)
    await loop.session.tick(at + 1_000)
    # Driven the way the owner drives it, because the backend is what syncs the registry
    # after an order placed outside a tick.
    assert (await PaperBackend(loop).inject_intent(coin=COIN, side="long")).injected
    a_market(loop, 3000.0, at=at + 2_900)
    await loop.session.tick(at + 3_000)

    fills = await loop.store.fills()

    assert fills, "a simulated fill left no row in the fills table"
    assert fills[0]["size"] > 0
    assert fills[0]["px"] > 0


async def test_a_paper_order_walks_the_real_state_machine(loop: PaperLoop) -> None:
    """SUBMITTED then ACKED, through `OrderManager`, not a status computed on the side."""
    from liqfade.execution.orders import OrderStatus

    assert loop.store is not None
    a_market(loop)
    await loop.session.tick(START + 1_000)
    loop.session.inject_intent(coin=COIN, side="long", now_ms=START + 1_000)
    await loop.session.tick(START + 1_250)

    rows = await loop.store.all_orders()

    assert [row.status for row in rows] == [OrderStatus.ACKED]
    assert rows[0].oid is not None  # the manager records what the transport answered


async def test_a_sizing_refusal_is_reported_like_a_risk_one(
    loop: PaperLoop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found by the box drill of 2026-09-07, not by reading the code.

    An injected entry produced `intent_injected` and then nothing at all: no order, no
    refusal, no line. Sizing returns before the risk gate is ever reached, so covering
    only `risk_blocks` left half the refusals as silent as they were before. To an owner
    watching Telegram the two are the same event, which is a drill that reported success
    and rested no order.
    """
    import structlog

    posted: list[str] = []

    async def fake_post(text: str) -> bool:
        posted.append(text)
        return True

    monkeypatch.setattr(loop, "post", fake_post)
    # A coin that is quoted and has never traded. Sizing caps on the traded volume in the
    # window, so it refuses before the risk gate is consulted, which is the whole point.
    loop.sink(
        "bbo",
        "hyperliquid",
        {
            "coin": "BTC",
            "ts_ms": START,
            "rx_ms": START,
            "bid_px": 60_000.0,
            "bid_sz": 5.0,
            "ask_px": 60_001.0,
            "ask_sz": 5.0,
        },
    )
    await loop.session.tick(START + 1_000)
    loop.session.inject_intent(coin="BTC", side="long", now_ms=START + 1_100)

    assert loop.session.result.sizing_blocks, "expected sizing to refuse this one"
    with structlog.testing.capture_logs() as logs:
        await loop.report_risk_blocks()

    events = [entry for entry in logs if entry["event"] == "risk_block"]
    assert len(events) == 1
    assert events[0]["coin"] == "BTC"
    assert events[0]["gate"] == "sizing"
    assert posted and "RISK BLOCK" in posted[0]


async def test_the_heartbeat_keeps_beating_through_a_long_tick(tmp_path: Path) -> None:
    """A tick that takes a fill, a report and a reconcile must not stop the heartbeat.

    The heartbeat used to be one more coroutine on the loop's own event loop, so anything
    that held that loop for longer than `risk.watchdog_stale_s` also stopped the file the
    watchdog reads, and the watchdog would kill a process that was merely busy. It runs on
    its own thread now, and the thread's interval is `risk.heartbeat_s` rather than a
    constant beside a validated field of the same name that nothing read.
    """
    import time as wall

    from liqfade.execution.watchdog import read_heartbeat

    paper = PaperLoop(
        paper_settings(tmp_path, **{"risk.heartbeat_s": 0.02, "risk.watchdog_stale_s": 0.5}),
        starting_equity=5_000.0,
    )
    await paper.setup()
    stop = asyncio.Event()
    task = asyncio.create_task(paper.heartbeat_loop(stop))
    try:
        await eventually(
            "the first beat", lambda: read_heartbeat(paper.settings.heartbeat_path) is not None
        )
        before = read_heartbeat(paper.settings.heartbeat_path)
        # Blocking the event loop is the whole point: this is what a long tick does.
        wall.sleep(0.3)  # noqa: ASYNC251 - fifteen heartbeat intervals of a busy loop
        after = read_heartbeat(paper.settings.heartbeat_path)
    finally:
        stop.set()
        await task
        await paper.aclose()

    assert before is not None and after is not None
    assert after - before >= 200, "the heartbeat stopped while the event loop was busy"


async def test_pause_resume_and_flat_reach_telegram(loop: PaperLoop) -> None:
    """A pause from the panel must look like a pause from Telegram to whoever is watching.

    These commands arrive from Telegram, the panel and the CLI alike, and they only wrote
    a log line. A state change nobody is told about is how two people end up disagreeing
    about whether the bot is trading.
    """
    posted: list[str] = []

    async def capture(text: str, **_: Any) -> None:
        posted.append(text)

    loop.post = capture  # type: ignore[assignment,method-assign]
    backend = PaperBackend(loop)

    await backend.pause("panel")
    await backend.resume()
    await backend.flat()

    assert any("PAUSED" in p and "panel" in p for p in posted)
    assert any("RESUMED" in p for p in posted)
    assert any("FLAT" in p for p in posted)


async def _reload_env(loop: PaperLoop, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `load_settings` resolve the same base the fixture built in memory.

    The unit does this with `Environment=` lines, so production is already consistent;
    the fixture constructs settings directly and has to say the same thing out loud.
    """
    monkeypatch.setenv("LIQFADE_PATHS__DATA_DIR", str(loop.settings.paths.data_dir))
    monkeypatch.setenv("LIQFADE_CASCADE__TRIGGER", loop.settings.cascade.trigger)


async def test_reload_applies_a_risk_limit_and_says_what_moved(
    loop: PaperLoop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The panel writes overrides.yaml and calls this; the loop has to actually apply it."""
    await _reload_env(loop, monkeypatch)
    posted: list[str] = []

    async def capture(text: str, **_: Any) -> None:
        posted.append(text)

    loop.post = capture  # type: ignore[assignment,method-assign]
    before = loop.settings.sizing.risk_frac
    overrides = Path(loop.settings.paths.data_dir) / "overrides.yaml"
    overrides.write_text(f"sizing:\n  risk_frac: {before / 2}\n")

    result = await PaperBackend(loop).reload_config()

    assert result.error is None
    assert "sizing.risk_frac" in result.changed
    assert loop.settings.sizing.risk_frac == before / 2
    assert loop.session.settings.sizing.risk_frac == before / 2, "the session reads it too"
    assert any("CONFIG RELOADED" in p for p in posted)


async def test_reload_refuses_what_the_feature_buffers_were_built_around(
    loop: PaperLoop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-reloaded engine has windows that no longer describe the data in its buffers.

    05a refused every reload for this reason. Naming the keys is the answer, not refusing
    the command: the panel's settings page changes sizing and risk limits, which are read
    fresh on the tick that uses them.
    """
    await _reload_env(loop, monkeypatch)
    before = loop.settings.features.z_lookback_h
    overrides = Path(loop.settings.paths.data_dir) / "overrides.yaml"
    overrides.write_text("features:\n  z_lookback_h: 999\n")

    result = await PaperBackend(loop).reload_config()

    assert result.changed == []
    assert result.error is not None
    assert "features.z_lookback_h" in result.error
    assert "restart" in result.error
    assert loop.settings.features.z_lookback_h == before


async def test_paper_started_names_the_config_hash_the_run_is_using(tmp_path: Path) -> None:
    """`liqfade drift` compares the soak's config against the replay's by this hash.

    Without it the config dimension can only report NOT_COMPARABLE, and every number
    below it would be a diff between two runs nobody has shown were the same strategy.
    """
    paper = PaperLoop(paper_settings(tmp_path), starting_equity=5_000.0)
    await paper.setup()
    try:
        fields = paper.started_fields()
    finally:
        await paper.aclose()

    assert fields["config_hash"] == config_hash(paper.settings)
    # The fields the line already carried are still on it.
    assert fields["env"] == paper.settings.env
    assert fields["trigger"] == "crossvenue"
    assert fields["equity"] == 5_000.0


async def test_the_config_hash_moves_when_a_setting_the_replay_reads_moves(
    tmp_path: Path,
) -> None:
    """A hash that ignored an override would report MATCH across two different runs."""
    one = PaperLoop(paper_settings(tmp_path), starting_equity=5_000.0)
    other = PaperLoop(
        paper_settings(tmp_path, **{"execution.max_reprices": 99}), starting_equity=5_000.0
    )
    await one.setup()
    await other.setup()
    try:
        assert one.started_fields()["config_hash"] != other.started_fields()["config_hash"]
    finally:
        await one.aclose()
        await other.aclose()


async def test_reload_reaches_the_strategy_that_decides_the_next_tick(
    loop: PaperLoop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The panel's writable sections are mostly the strategy's own.

    A reload that swaps the loop's settings and leaves the engine on the old object
    announces a change on Telegram and applies none of it, which is the worst of both:
    the owner believes the box is trading under the numbers the page shows.
    """
    from liqfade.control.protocol import now_ms

    await _reload_env(loop, monkeypatch)
    at = now_ms()
    a_market(loop, 3000.0, at=at)
    await loop.session.tick(at + 1_000)  # ETH gets a detector, built under the old settings
    engine = loop.session.strategy
    assert engine._detectors, "a detector to be left behind by the reload"

    overrides = Path(loop.settings.paths.data_dir) / "overrides.yaml"
    overrides.write_text(
        "strategy:\n"
        "  min_confluence: 1\n"
        "  rules:\n"
        "    min_cascade_notional:\n"
        "      enabled: true\n"
        "      required: false\n"
    )

    result = await PaperBackend(loop).reload_config()

    assert result.error is None
    assert "strategy.min_confluence" in result.changed
    assert engine.settings is loop.settings, "the engine reads its settings on every tick"
    assert engine.settings.strategy.min_confluence == 1
    # The rules and the detectors are built once and would otherwise keep the config
    # sections they were constructed with.
    assert "min_cascade_notional" in [rule.name for rule in engine._rules]
    assert all(d.settings is loop.settings for d in engine._detectors.values())


async def test_reload_reaches_a_venue_session_the_same_way(tmp_path: Path) -> None:
    """Testnet and mainnet run `VenueSession` behind the same reload, and the same panel."""
    from liqfade.live.loop import apply_settings
    from tests.test_live_venue import a_session

    session, _venue = await a_session(tmp_path)
    try:
        fresh = paper_settings(
            tmp_path,
            **{
                "strategy.min_confluence": 1,
                "strategy.rules.min_cascade_notional.enabled": True,
                "strategy.rules.min_cascade_notional.required": False,
            },
        )

        apply_settings(session, fresh)

        assert session.settings is fresh
        assert session.risk.settings is fresh
        assert session.strategy.settings is fresh
        assert "min_cascade_notional" in [rule.name for rule in session.strategy._rules]
    finally:
        await session._store.close()
