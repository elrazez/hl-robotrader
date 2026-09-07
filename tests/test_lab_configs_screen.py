"""The configs screen, driven by Textual's Pilot.

Only what is cheap to drive from a keyboard is tested here; the reading, writing and
validating all live in `lab/configs.py` and `lab/form.py` and are tested there.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from liqfade.lab.app import LabApp
from liqfade.lab.configs import read_config, save_config


def text_of(widget: object) -> str:
    """Whatever a Static is currently showing, as plain text."""
    return str(getattr(widget, "content", ""))


@pytest.fixture
def lab_dir(tmp_path: Path) -> Path:
    save_config(
        tmp_path / "v0.yaml", "v0 baseline", "the spec's rules", {"backtest.latency_ms": 1000}
    )
    save_config(tmp_path / "tight.yaml", "Tight stops", "k_stop halved", {"intent.k_stop": 0.5})
    return tmp_path


async def test_the_lab_opens_on_configs_and_lists_them(lab_dir: Path) -> None:
    app = LabApp(config_dir=lab_dir, reports_dir=lab_dir / "reports")
    async with app.run_test() as pilot:
        assert app.query_one("#tabs").active == "tab-configs"
        table = app.query_one("#configs-table")
        names = {str(table.get_row_at(row)[0]) for row in range(table.row_count)}
        assert names == {"Tight stops", "v0 baseline"}
        await pilot.pause()


async def test_view_shows_each_override_with_the_models_description(lab_dir: Path) -> None:
    app = LabApp(config_dir=lab_dir, reports_dir=lab_dir / "reports")
    async with app.run_test() as pilot:
        await pilot.press("v")
        await pilot.pause()
        text = text_of(app.screen.query_one("#config-view"))
        assert "intent.k_stop" in text
        assert "multiple of rv_5m * mid placed beyond the extreme" in text
        assert "0.5" in text


async def test_an_invalid_edit_shows_the_validators_message_and_saves_nothing(
    lab_dir: Path,
) -> None:
    app = LabApp(config_dir=lab_dir, reports_dir=lab_dir / "reports")
    async with app.run_test() as pilot:
        await pilot.press("e")
        await pilot.pause()
        app.screen.field("sizing.risk_frac").value = "2.0"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert "less than or equal to 1" in text_of(app.screen.query_one("#edit-errors"))
        assert read_config(lab_dir / "tight.yaml").overrides == {"intent.k_stop": 0.5}


async def test_a_valid_edit_writes_only_the_keys_that_changed(lab_dir: Path) -> None:
    app = LabApp(config_dir=lab_dir, reports_dir=lab_dir / "reports")
    async with app.run_test() as pilot:
        await pilot.press("e")
        await pilot.pause()
        app.screen.field("intent.k_target").value = "0.9"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert read_config(lab_dir / "tight.yaml").overrides == {
            "intent.k_stop": 0.5,
            "intent.k_target": 0.9,
        }


async def test_delete_asks_twice_and_the_first_no_keeps_the_file(lab_dir: Path) -> None:
    app = LabApp(config_dir=lab_dir, reports_dir=lab_dir / "reports")
    async with app.run_test() as pilot:
        await pilot.press("x")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        assert (lab_dir / "tight.yaml").exists()

        await pilot.press("x")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert (lab_dir / "tight.yaml").exists(), "one yes is not enough"
        await pilot.press("y")
        await pilot.pause()
        assert not (lab_dir / "tight.yaml").exists()
