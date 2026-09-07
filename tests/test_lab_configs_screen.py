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


async def test_opening_the_real_v1_config_and_saving_it_changes_nothing(tmp_path: Path) -> None:
    """The round trip that has to hold for every config the owner actually runs.

    `v1_confluence.yaml` carries two deliberate nulls: `cascade.liq_z_min`, which is how it
    says the z trigger is not applied at all, and `strategy.rules.oi_flush.build_min_frac`,
    which switches off the build half of the flush. The form showed a null as an empty box
    and read an empty box back as "not in this layer", so opening the file and pressing save
    without touching anything deleted both, turned the z trigger back on, and changed which
    cascades the next run would take.

    Sixteen keys were lost, not two. Everything whose value happened to equal the resolved
    default went with them: `cascade.trigger: exact`, `intent.k_target: 0.8`,
    `backtest.latency_ms: 1000` and the rest. Those merge to the same run today and say
    nothing about what the owner chose, which is what a config file is for.
    """
    source = Path("config/backtest/v1_confluence.yaml")
    before = read_config(source)
    assert before.overrides["cascade.liq_z_min"] is None, "the fixture must carry a null"
    assert before.overrides["strategy.rules.oi_flush.build_min_frac"] is None

    target = tmp_path / "v1_confluence.yaml"
    target.write_text(source.read_text())
    app = LabApp(config_dir=tmp_path, reports_dir=tmp_path / "reports")
    async with app.run_test() as pilot:
        await pilot.press("e")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
    after = read_config(target)
    assert after.overrides == before.overrides
    assert after.overrides["cascade.liq_z_min"] is None, "the z trigger came back on"


async def test_the_screen_refuses_to_delete_a_loader_layer(tmp_path: Path) -> None:
    """`liqfade lab --config-dir config` puts mainnet.yaml one keystroke from the cursor."""
    (tmp_path / "mainnet.yaml").write_text("risk:\n  max_dd_frac: 0.1\n")
    app = LabApp(config_dir=tmp_path, reports_dir=tmp_path / "reports")
    async with app.run_test() as pilot:
        await pilot.press("x")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert (tmp_path / "mainnet.yaml").exists(), "the lab deleted a loader layer file"
        assert "loader owns" in text_of(app.query_one("#configs-status"))
