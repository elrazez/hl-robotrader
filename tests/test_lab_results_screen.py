"""The Results screen: the list, the report, the side by side and promote."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from liqfade.lab.app import LabApp
from liqfade.lab.configs import save_config
from liqfade.lab.runs import THE_YEAR
from tests.test_lab_results import write_run

pytestmark = pytest.mark.asyncio
LAKE = Path(__file__).resolve().parent / "fixtures"


def text_of(widget: object) -> str:
    return str(getattr(widget, "content", ""))


@pytest.fixture
def lab_dir(tmp_path: Path) -> Path:
    save_config(tmp_path / "v0.yaml", "v0 baseline", "the spec", {"backtest.latency_ms": 1000})
    reports = tmp_path / "reports"
    first = write_run(reports, "20260906T120000Z_aaaa1111")
    second = write_run(reports, "20260906T130000Z_bbbb2222")
    first.joinpath("lab.json").write_text(
        json.dumps(
            {
                "config_name": "v0 baseline",
                "config_path": str(tmp_path / "v0.yaml"),
                "overrides": {"intent.k_stop": 1.0},
            }
        )
    )
    second.joinpath("lab.json").write_text(
        json.dumps(
            {
                "config_name": "wider stop",
                "config_path": str(tmp_path / "v0.yaml"),
                "overrides": {"intent.k_stop": 1.5},
            }
        )
    )
    return tmp_path


def app_for(lab_dir: Path) -> LabApp:
    return LabApp(config_dir=lab_dir, reports_dir=lab_dir / "reports", data_dir=LAKE)


async def test_the_results_tab_lists_the_runs(lab_dir: Path) -> None:
    app = app_for(lab_dir)
    async with app.run_test() as pilot:
        await pilot.press("3")
        await pilot.pause()
        table = app.query_one("#results-table")
        assert table.row_count == 2
        configs = {str(table.get_row_at(row)[1]) for row in range(table.row_count)}
        assert configs == {"v0 baseline", "wider stop"}


async def test_open_renders_the_report_and_the_funnel_per_rule(lab_dir: Path) -> None:
    app = app_for(lab_dir)
    async with app.run_test() as pilot:
        await pilot.press("3")
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        shown = app.screen.query_one("#report-body").source
        # Newest first, which is what the list is sorted by.
        assert "Backtest 20260906T130000Z_bbbb2222" in shown
        funnel = text_of(app.screen.query_one("#report-funnel"))
        assert "vwap_stretch" in funnel
        assert "83" in funnel


async def test_marking_two_runs_shows_them_side_by_side_with_the_config_diff(
    lab_dir: Path,
) -> None:
    app = app_for(lab_dir)
    async with app.run_test() as pilot:
        await pilot.press("3")
        await pilot.pause()
        await pilot.press("space")
        await pilot.press("down")
        await pilot.press("space")
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()
        shown = text_of(app.screen.query_one("#compare-body"))
        assert "net" in shown
        assert "intent.k_stop" in shown
        assert "1.5" in shown


async def test_promote_queues_the_same_config_over_the_year(lab_dir: Path) -> None:
    app = app_for(lab_dir)
    async with app.run_test() as pilot:
        await pilot.press("3")
        await pilot.pause()
        app.queue.lake_busy = lambda: True  # queued, not launched: this is a real year run
        await pilot.press("p")
        await pilot.pause()
        assert len(app.queue.jobs) == 1
        spec = app.queue.jobs[0].spec
        assert (spec.start, spec.end) == THE_YEAR
        assert spec.coins == ("BTC", "ETH")


async def test_promote_says_so_when_it_does_not_know_the_config(tmp_path: Path) -> None:
    save_config(tmp_path / "v0.yaml", "v0", "", {"backtest.latency_ms": 1000})
    write_run(tmp_path / "reports", "20260906T120000Z_aaaa1111")
    app = LabApp(config_dir=tmp_path, reports_dir=tmp_path / "reports", data_dir=LAKE)
    async with app.run_test() as pilot:
        await pilot.press("3")
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        assert app.queue.jobs == []
        assert "which config" in text_of(app.query_one("#results-status"))
