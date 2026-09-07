"""The Run screen: the resolved command, the launch, the queue and the progress frame."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from liqfade.lab.app import LabApp
from liqfade.lab.configs import save_config

LAKE = Path(__file__).resolve().parent / "fixtures"


class FakeProcess:
    def __init__(self) -> None:
        self.code: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.code

    def terminate(self) -> None:
        self.terminated = True
        self.code = -15

    def kill(self) -> None:
        self.code = -9


def text_of(widget: object) -> str:
    return str(getattr(widget, "content", ""))


@pytest.fixture
def lab_dir(tmp_path: Path) -> Path:
    save_config(tmp_path / "v0.yaml", "v0 baseline", "the spec", {"backtest.latency_ms": 1000})
    return tmp_path


def app_for(lab_dir: Path) -> LabApp:
    return LabApp(config_dir=lab_dir, reports_dir=lab_dir / "reports", data_dir=LAKE)


async def test_the_run_tab_shows_the_command_it_would_launch(lab_dir: Path) -> None:
    app = app_for(lab_dir)
    async with app.run_test() as pilot:
        await pilot.press("2")
        await pilot.pause()
        shown = text_of(app.query_one("#run-command"))
        assert "liqfade backtest" in shown
        assert "--config" in shown
        assert "v0.yaml" in shown
        assert "--progress-file" in shown


async def test_a_typed_window_reaches_the_command(lab_dir: Path) -> None:
    app = app_for(lab_dir)
    async with app.run_test() as pilot:
        await pilot.press("2")
        await pilot.pause()
        pane = app.query_one("#run-pane")
        pane.query_one("#run-start").value = "2026-08-30"
        pane.query_one("#run-end").value = "2026-08-30"
        pane.query_one("#run-coins").value = "BTC"
        pane.refresh_command()
        await pilot.pause()
        shown = text_of(app.query_one("#run-command"))
        assert "--start 2026-08-30" in shown
        assert "--coins BTC" in shown


async def test_launch_queues_a_job_and_the_frame_follows_its_progress_file(
    lab_dir: Path,
) -> None:
    app = app_for(lab_dir)
    async with app.run_test() as pilot:
        await pilot.press("2")
        await pilot.pause()
        app.queue.spawn = lambda argv, log, env: FakeProcess()
        app.queue.lake_busy = lambda: False
        await pilot.press("ctrl+r")
        await pilot.pause()
        job = app.queue.jobs[0]
        assert job.state == "running"

        job.progress_path.parent.mkdir(parents=True, exist_ok=True)
        job.progress_path.write_text(
            json.dumps(
                {
                    "phase": "main",
                    "stage": "replaying",
                    "coins": ["BTC"],
                    "window_start_ms": 1756512000000,
                    "window_end_ms": 1756598400000,
                    "sim_ms": 1756555200000,
                    "trades": 4,
                    "net": 12.5,
                }
            )
        )
        app.query_one("#run-pane").tick()
        await pilot.pause()
        frame = text_of(app.query_one("#run-progress"))
        # The frame is backtest/watch.py's, so the lab and `liqfade watch` cannot disagree.
        assert "replaying" in frame
        assert "trades 4" in frame


async def test_a_busy_lake_leaves_the_job_queued_and_says_why(lab_dir: Path) -> None:
    app = app_for(lab_dir)
    async with app.run_test() as pilot:
        await pilot.press("2")
        await pilot.pause()
        app.queue.spawn = lambda argv, log, env: FakeProcess()
        app.queue.lake_busy = lambda: True
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert app.queue.jobs[0].state == "queued"
        assert "another backtest" in text_of(app.query_one("#run-status"))


async def test_cancel_sends_a_term_to_the_selected_job(lab_dir: Path) -> None:
    app = app_for(lab_dir)
    async with app.run_test() as pilot:
        await pilot.press("2")
        await pilot.pause()
        process = FakeProcess()
        app.queue.spawn = lambda argv, log, env: process
        app.queue.lake_busy = lambda: False
        await pilot.press("ctrl+r")
        await pilot.pause()
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert process.terminated


async def test_every_box_on_the_run_form_is_inside_the_screen(lab_dir: Path) -> None:
    """Textual's Input is 100% wide by default, and three of them share a row.

    `.field-input { width: 1fr }` only ever covered the edit form, so on the run form the
    end date, the latency and the lagged-ms boxes were laid out past the right edge and
    could not be seen or clicked. `docs/lab/run.svg` showed it and nothing failed.
    """
    from textual.widgets import Input

    app = LabApp(config_dir=lab_dir, reports_dir=lab_dir / "reports", data_dir=LAKE)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.press("2")
        await pilot.pause()
        boxes = list(app.query(".run-row Input").results(Input))
        assert boxes, "the run form drew no boxes, so this proves nothing"
        overflowing = {
            widget.id: (widget.region.x, widget.region.right)
            for widget in app.query(".run-row Input").results(Input)
            if widget.region.right > app.size.width
        }
        assert overflowing == {}, f"boxes laid out past the right edge: {overflowing}"


async def test_ctrl_t_cancels_the_selected_run_and_not_the_first_one(lab_dir: Path) -> None:
    """Three jobs queued, the cursor on the third: that is the one that gets the SIGTERM."""
    app = LabApp(config_dir=lab_dir, reports_dir=lab_dir / "reports", data_dir=LAKE)
    app.queue.lake_busy = lambda: True  # nothing starts, so all three stay queued
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.press("2")
        await pilot.pause()
        for _ in range(3):
            await pilot.press("ctrl+r")
            await pilot.pause()
        pane = app.query_one("#run-pane")
        app.query_one("#run-jobs").move_cursor(row=2)
        await pilot.pause()
        assert pane.selected is app.queue.jobs[2]

        await pilot.press("ctrl+t")
        await pilot.pause()
        assert [job.state for job in app.queue.jobs] == ["queued", "queued", "cancelled"]
