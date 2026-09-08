"""`liqfade drift` end to end, over the golden lake, through both engines.

The tracer bullet and the real assertion of this module. A `PaperSession` is driven over
the golden tape and `run_backtest` is run over the same tape written to a real lake, under
the same resolved config and the same equity. `live/paper.py` claims paper *is* the
backtest engine on a live clock; if that is true these two agree, and this is the test
that says so with a number rather than a docstring.

The clock is the thing to watch. The session is driven on the tape's own timestamps, so
its fills carry the market clock, which is the clock the replay is on and the clock the
comparison keys on. Only `submitted_ms` is the process's wall clock, and nothing here
keys on it.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import polars as pl
import pytest

from liqfade.backtest.drift import Verdict, compare, read_live, read_replay
from liqfade.backtest.engine import _resolve_meta, run_backtest
from liqfade.backtest.report import config_hash
from liqfade.backtest.source import LakeSource
from liqfade.cli import main
from liqfade.config.loader import resolve
from liqfade.config.models import Settings
from liqfade.execution.orders import OrderStore
from liqfade.live.paper import PaperSession

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import golden_lake

EQUITY = 100_000.0
TICK_MS = 1_000
# Sparse until the cascade is near, then every second. `LiveFeatureEngine.advance` closes
# every bucket up to the timestamp it is given however far it jumped, so a coarse warm-up
# leaves exactly the feature state a per-second one would; only the decisions have to be
# on the grid's own resolution, and those all happen inside the dense stretch. A per-second
# tick over the whole fixture is 12,000 ticks of sqlite and feature work for three copies
# of one cascade, which is minutes rather than the suite's seconds.
CROSS_VENUE = "bybit"
WARMUP_TICK_MS = 30_000
# One cascade is the whole comparison: the fixture's three are identical by construction,
# so the second and third would re-measure what the first already said.
DENSE_FROM_MS = golden_lake.CASCADE_STARTS[0] - 120_000
WINDOW_END_MS = golden_lake.CASCADE_STARTS[0] + golden_lake.RECOVERY_MS + 120_000


def settings_for(lake: Path, **dotted: Any) -> Settings:
    merged, _ = resolve("paper")
    merged["paths"]["data_dir"] = str(lake)
    # What `liqfade-paper.service` sets with LIQFADE_CASCADE__TRIGGER. The paper default is
    # `exact`, which reads Hyperliquid's own S3 liquidations; those arrive a day late and
    # the live feature engine refuses the trigger outright, so a paper-versus-replay
    # comparison can only be run on the trigger the loop can actually see.
    merged["cascade"]["trigger"] = "crossvenue"
    for key, value in dotted.items():
        node = merged
        parts = key.split(".")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
    return Settings.model_validate(merged)


def cross_venue(liquidations: pl.DataFrame) -> pl.DataFrame:
    """The fixture's liquidations, relabelled onto a venue the live loop can see.

    The `venue` column and not only the partition path: the replay filters the cross-venue
    liquidation set on the column, so rows written under a Bybit path while still saying
    `hyperliquid` are read by neither trigger.
    """
    return liquidations.with_columns(pl.lit(CROSS_VENUE).alias("venue"))


def events(
    trades: pl.DataFrame, liquidations: pl.DataFrame, quotes: pl.DataFrame
) -> list[tuple[int, str, dict]]:
    """Every row of the fixture in one time-ordered stream, as the session ingests them."""
    rows: list[tuple[int, str, dict]] = []
    for table, frame in (("trades", trades), ("liquidations", liquidations), ("bbo", quotes)):
        rows += [(int(row["ts_ms"]), table, row) for row in frame.to_dicts()]
    rows.sort(key=lambda item: item[0])
    return rows


def ticks(start_ms: int, end_ms: int) -> list[int]:
    """The clock the session is driven on: coarse through the warm-up, 1s at the cascade."""
    coarse = list(range(start_ms, min(DENSE_FROM_MS, end_ms), WARMUP_TICK_MS))
    dense = list(range(max(start_ms, DENSE_FROM_MS), end_ms + TICK_MS, TICK_MS))
    return coarse + dense


async def drive(
    settings: Settings,
    db: Path,
    *,
    start_ms: int,
    end_ms: int,
    trades_out: list[Any] | None = None,
) -> OrderStore:
    """Replay the golden tape through a live session on the tape's own clock."""
    trades, liquidations, tape_start_ms, _ = golden_lake.build()
    store = await OrderStore.open(db)
    session = PaperSession(
        settings,
        store=store,
        # Exactly what the replay is given: `LakeSource` carries no venue metadata for a
        # historical window, so the engine invents the most constraining defaults for the
        # run's coins. A session handed anything else would be sized and filtered
        # differently, and the diff would be measuring the fixture rather than the engines.
        meta=_resolve_meta({}, [golden_lake.COIN], settings),
        starting_equity=EQUITY,
    )
    stream = [
        row
        for row in events(
            trades,
            cross_venue(liquidations),
            golden_lake.quotes(tape_start_ms, end_ms + TICK_MS),
        )
        if row[0] <= end_ms
    ]
    cursor = 0
    for now_ms in ticks(start_ms, end_ms):
        while cursor < len(stream) and stream[cursor][0] <= now_ms:
            _, table, row = stream[cursor]
            session.ingest(table, CROSS_VENUE if table == "liquidations" else "hyperliquid", row)
            cursor += 1
        await session.tick(now_ms)
    session.close_out(end_ms)
    await session.tick(end_ms)
    if trades_out is not None:
        trades_out.extend(session.portfolio.trades)
    return store


def replay(settings: Settings, lake: Path, start_ms: int, end_ms: int) -> Any:
    return run_backtest(
        LakeSource(
            lake,
            settings=settings,
            coins=[golden_lake.COIN],
            start_ms=start_ms,
            end_ms=end_ms,
            starting_equity=EQUITY,
            cache=False,
        ),
        settings,
    )


@pytest.fixture(scope="module")
def lake(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, int, int]:
    """The golden tape, with its liquidations also written as a cross-venue feed.

    `write_lake` writes them under Hyperliquid, which is what `exact` reads. The live loop
    cannot run `exact`, so the same prints are written again under Bybit and the session
    is fed them from there: both sides then see one cross-venue liquidation tape and the
    comparison is between two engines rather than between two trigger definitions.
    """
    from liqfade.data.schemas import SOURCE_RECORDER
    from liqfade.recorder.writer import write_rows

    root = tmp_path_factory.mktemp("drift_lake")
    day_start, _ = golden_lake.write_lake(root, 1, bbo=True)
    _, liquidations, _, _ = golden_lake.build()
    day = datetime.fromtimestamp(day_start / 1000, UTC).strftime("%Y-%m-%d")
    write_rows(
        root / "parquet",
        ("liquidations", CROSS_VENUE, day, 0),
        cross_venue(liquidations).to_dicts(),
        source=SOURCE_RECORDER,
    )
    return root, day_start, WINDOW_END_MS


@pytest.fixture(scope="module")
def live_side(lake: tuple[Path, int, int], tmp_path_factory: pytest.TempPathFactory) -> Any:
    """One drive of the paper session, shared by every test in this module.

    Module scoped on purpose: the drive is the expensive half, and every test below asks
    a different question of the same run rather than a question about a different run.
    """
    root, start_ms, end_ms = lake
    db = tmp_path_factory.mktemp("drift_db") / "orders.sqlite"
    settings = settings_for(root)

    async def go() -> Any:
        store = await drive(settings, db, start_ms=start_ms, end_ms=end_ms)
        try:
            return await read_live(
                store, start_ms, end_ms + TICK_MS, config_hash=config_hash(settings)
            )
        finally:
            await store.close()

    return asyncio.run(go())


@pytest.fixture(scope="module")
def live_trades(lake: tuple[Path, int, int], tmp_path_factory: pytest.TempPathFactory) -> Any:
    """The session's own portfolio from one drive, for the engine-to-engine comparison."""
    root, start_ms, end_ms = lake
    db = tmp_path_factory.mktemp("drift_trades") / "orders.sqlite"

    async def go() -> Any:
        session_trades: list[Any] = []
        store = await drive(
            settings_for(root), db, start_ms=start_ms, end_ms=end_ms, trades_out=session_trades
        )
        await store.close()
        return session_trades

    return asyncio.run(go())


def test_the_two_engines_produce_the_same_trade_from_the_same_tape(
    lake: tuple[Path, int, int], live_trades: Any
) -> None:
    """`live/paper.py`'s claim, checked against the engine it claims to be.

    Not through the diff: straight off the two portfolios. If these ever disagree, every
    verdict below is measuring the engines rather than the plumbing between them, and this
    is the test that says which.
    """
    root, start_ms, end_ms = lake
    replayed = replay(settings_for(root), root, start_ms, end_ms).trades

    assert live_trades, "the session must produce a trade or this proves nothing"
    assert len(live_trades) == len(replayed)
    for live, other in zip(live_trades, replayed, strict=True):
        assert (live.entry_ms, live.exit_ms, live.exit_reason) == (
            other.entry_ms,
            other.exit_ms,
            other.exit_reason,
        )
        assert live.entry_px == pytest.approx(other.entry_px)
        assert live.exit_px == pytest.approx(other.exit_px)
        assert live.net_pnl == pytest.approx(other.net_pnl)


def test_the_diff_matches_on_everything_the_registry_records_faithfully(
    lake: tuple[Path, int, int], live_side: Any
) -> None:
    """Intents, exits, refusals and coverage agree, which is what the plumbing decides."""
    root, start_ms, end_ms = lake
    settings = settings_for(root)
    result = replay(settings, root, start_ms, end_ms)
    report = compare(live_side, read_replay(result, settings=settings), settings=settings)

    assert result.trades and live_side.round_trips
    verdicts = {d.name: d.verdict for d in report.dimensions}
    assert verdicts["config"] is Verdict.MATCH
    assert verdicts["intents"] is Verdict.MATCH
    assert verdicts["exits"] is Verdict.MATCH
    assert verdicts["holes"] is Verdict.MATCH


def test_entries_and_pnl_drift_because_paper_books_the_order_price_not_the_fill_price(
    lake: tuple[Path, int, int], live_side: Any
) -> None:
    """A real finding, pinned here so that fixing it fails this test rather than passing quietly.

    The two portfolios agree to the cent (the test above). What disagrees is the order
    registry: `PaperSession._book_fill` records `px=order.px`, the price the order was
    placed at, while the fill model fills a crossing entry at the touch and the portfolio
    books that. On this fixture the difference is the half spread, ten basis points, and it
    flows into the reconstructed PnL as an entry that looks cheaper than the one the
    portfolio paid.

    It is a paper-mode fidelity gap rather than a drift between engines: on a real venue
    `record_fill` is handed the venue's own price. The panel, `/status` and this command
    all read the registry, so all three currently show a better entry than was booked.
    Fixing it belongs in `live/paper.py`, not here.
    """
    root, start_ms, end_ms = lake
    settings = settings_for(root)
    result = replay(settings, root, start_ms, end_ms)
    report = compare(live_side, read_replay(result, settings=settings), settings=settings)

    entries = next(d for d in report.dimensions if d.name == "entries")
    assert entries.verdict is Verdict.DRIFT
    # Exactly the slippage the fill model charged and the registry did not record.
    assert entries.metrics[0].value == pytest.approx(result.trades[0].entry_slippage_bps, rel=0.02)


def test_a_replay_under_different_config_stops_the_report_at_the_config_line(
    lake: tuple[Path, int, int], live_side: Any
) -> None:
    """Nothing below a config disagreement compares the same strategy against itself."""
    root, start_ms, end_ms = lake
    moved = settings_for(root, **{"intent.k_target": 0.2})
    result = replay(moved, root, start_ms, end_ms)
    report = compare(live_side, read_replay(result, settings=moved), settings=moved)

    assert [d.name for d in report.dimensions] == ["config"]
    assert report.verdict is Verdict.DRIFT


def test_a_replay_that_exits_somewhere_else_is_caught_by_the_numbers_not_by_the_hash(
    lake: tuple[Path, int, int], live_side: Any
) -> None:
    """The real assertion: a comparator trusted precisely when it is wrong is worthless.

    The replay's target is moved and its hash is then relabelled to the live side's, so
    the config dimension cannot be what catches it and the dimensions below have to. On
    this fixture the moved target puts the exit under the entry and the replay refuses the
    intent the session traded, which is the disagreement the intents dimension exists for.
    """
    root, start_ms, end_ms = lake
    moved = settings_for(root, **{"intent.k_target": 0.2})
    replayed = read_replay(replay(moved, root, start_ms, end_ms), settings=moved)
    settings = settings_for(root)
    report = compare(
        live_side,
        replace(replayed, config_hash=config_hash(settings)),
        settings=settings,
    )

    drifting = {d.name for d in report.dimensions if d.verdict is Verdict.DRIFT}
    assert drifting, [
        (d.name, str(d.verdict), [(m.name, m.value) for m in d.metrics]) for d in report.dimensions
    ]


def test_the_command_writes_a_report_and_a_json_beside_it(
    lake: tuple[Path, int, int], tmp_path: Path
) -> None:
    """The CLI end to end, over a registry with nothing in it.

    An empty registry is not a quiet pass: the refusals table holds nothing, so that
    dimension has to come back NOT_COMPARABLE rather than agreeing with a replay that
    refused things.
    """
    root, start_ms, _ = lake

    async def seed() -> None:
        store = await OrderStore.open(tmp_path / "orders.sqlite")
        await store.close()

    asyncio.run(seed())
    day = datetime.fromtimestamp(start_ms / 1000, UTC).strftime("%Y-%m-%d")
    out = tmp_path / "reports"
    code = main(
        [
            "drift",
            "--env",
            "paper",
            "--start",
            day,
            "--end",
            day,
            "--coins",
            golden_lake.COIN,
            "--equity",
            str(EQUITY),
            "--data-dir",
            str(root),
            "--db",
            str(tmp_path / "orders.sqlite"),
            "--out",
            str(out),
        ]
    )

    assert code == 0
    report = (out / f"drift_{day}_{day}.md").read_text()
    assert "NOT_COMPARABLE" in report
    assert "refusals" in report
    payload = orjson.loads((out / f"drift_{day}_{day}.json").read_bytes())
    assert {d["name"] for d in payload["dimensions"]} >= {"config", "intents", "refusals"}
