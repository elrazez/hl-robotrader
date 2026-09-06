"""The progress file the backtest writes, and the frame `liqfade watch` draws from it."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from test_strategy_cascade_consistency import build_stream

from liqfade.backtest.engine import RunInputs, run_backtest
from liqfade.backtest.progress import (
    CHECK_EVERY_SIM_MS,
    ProgressState,
    ProgressWriter,
    read_progress,
)
from liqfade.backtest.watch import render
from liqfade.config.models import Settings

FIXTURE_LAKE = Path(__file__).parent / "fixtures"


def test_the_writer_is_throttled_and_the_file_is_always_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"wall": 100.0}
    monkeypatch.setattr("liqfade.backtest.progress.time.monotonic", lambda: clock["wall"])
    path = tmp_path / "p.json"
    writer = ProgressWriter(path)
    writer.set(stage="replaying", window_start_ms=0, window_end_ms=10 * CHECK_EVERY_SIM_MS)

    writer.tick(0, trades=0, net=0.0, equity=1.0, intents=0)
    first = read_progress(path)
    assert first is not None and first.sim_ms == 0
    # Same wall second, a simulated minute later: no write.
    writer.tick(CHECK_EVERY_SIM_MS, trades=1, net=2.0, equity=1.0, intents=1)
    assert read_progress(path) is not None and read_progress(path).trades == 0  # type: ignore[union-attr]
    # A wall second later: written, and no temp file left beside it.
    clock["wall"] += 1.5
    writer.tick(2 * CHECK_EVERY_SIM_MS, trades=1, net=2.0, equity=1.0, intents=1)
    state = read_progress(path)
    assert state is not None and state.trades == 1 and state.fraction == pytest.approx(0.2)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["p.json"]
    assert json.loads(path.read_text())["stage"] == "replaying"


def test_the_engine_reports_the_clock_and_the_trade_count(
    settings_factory: object, tmp_path: Path
) -> None:
    trades, liquidations, start_ms, end_ms = build_stream()
    settings = settings_factory(**{"features.z_lookback_h": 1.0})  # type: ignore[operator]
    assert isinstance(settings, Settings)
    inputs = RunInputs(
        trades=trades,
        liquidations=liquidations,
        start_ms=start_ms,
        end_ms=end_ms,
        starting_equity=100_000.0,
    )
    path = tmp_path / "p.json"
    result = run_backtest(inputs, settings, ProgressWriter(path))
    state = read_progress(path)
    assert state is not None
    assert state.stage == "replaying" and state.sim_ms == end_ms and state.fraction == 1.0
    assert state.trades == len(result.trades) and state.coins == list(result.coins)


def test_the_command_marks_the_file_done_with_the_report_dir(tmp_path: Path) -> None:
    from liqfade.cli import main

    progress = tmp_path / "progress.json"
    with redirect_stdout(io.StringIO()):
        code = main(
            [
                "backtest",
                "--data-dir",
                str(FIXTURE_LAKE),
                "--coins",
                "BTC",
                "--start",
                "2026-08-30",
                "--end",
                "2026-08-30",
                "--out",
                str(tmp_path),
                "--progress-file",
                str(progress),
            ]
        )
    assert code == 0
    state = read_progress(progress)
    assert state is not None and state.stage == "done"
    assert Path(state.report_dir).is_dir()


def test_the_frame_shows_the_bar_the_eta_and_the_numbers() -> None:
    state = ProgressState(
        phase="main",
        stage="replaying",
        coins=["BTC", "ETH"],
        window_start_ms=0,
        window_end_ms=200_000,
        sim_ms=100_000,
        starting_equity=10_000.0,
        equity=10_250.0,
        trades=7,
        net=250.0,
        intents=9,
        started_at=1000.0,
        phase_started_at=1000.0,
        updated_at=1060.0,
    )
    frame = render(state, now=1060.0, width=100, path=Path("x"))
    assert "BTC,ETH" in frame and "50.0%" in frame and "█" in frame and "░" in frame
    # Half way after a minute means a minute to go.
    assert "eta 01:00" in frame and "trades 7" in frame and "+250.00" in frame
    done = render(
        ProgressState(stage="done", report_dir="reports/run", started_at=0.0),
        now=5.0,
        path=Path("x"),
    )
    assert "100.0%" in done and "reports/run" in done
    assert "no backtest running" in render(None, now=0.0, path=Path("nowhere"))
