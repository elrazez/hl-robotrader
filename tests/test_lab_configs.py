"""Strategy configs as the lab sees them: a list, a diff against the defaults, a writer."""

from __future__ import annotations

from pathlib import Path

import pytest

from liqfade.lab.configs import (
    delete_config,
    duplicate_config,
    list_configs,
    read_config,
    render_config,
    save_config,
)

REPO_CONFIGS = Path(__file__).resolve().parent.parent / "config" / "backtest"


def test_the_repos_own_configs_list_with_the_keys_they_override() -> None:
    configs = list_configs(REPO_CONFIGS)
    by_name = {config.name: config for config in configs}
    assert "v0" in by_name
    v1 = by_name["v1_confluence"]
    assert v1.overrides["cascade.trigger"] == "exact"
    assert v1.overrides["strategy.min_confluence"] == 2
    assert v1.overrides["cascade.liq_usd_min.core"] == 500000.0
    assert v1.mtime > 0


def test_a_name_header_wins_over_the_filename(tmp_path: Path) -> None:
    path = tmp_path / "whatever.yaml"
    path.write_text(
        "# name: Tight stops\n# notes: k_stop halved, 2026-09-06\nintent:\n  k_stop: 0.5\n"
    )
    config = read_config(path)
    assert config.name == "Tight stops"
    assert config.notes == "k_stop halved, 2026-09-06"
    assert config.overrides == {"intent.k_stop": 0.5}


def test_without_a_header_the_filename_is_the_name(tmp_path: Path) -> None:
    path = tmp_path / "quiet.yaml"
    path.write_text("intent:\n  k_stop: 0.5\n")
    config = read_config(path)
    assert config.name == "quiet"
    assert config.notes == ""


def test_the_writer_emits_only_overridden_keys_each_with_the_models_description() -> None:
    text = render_config(
        "Tight stops",
        "k_stop halved",
        {"intent.k_stop": 0.5, "cascade.liq_usd_min.core": 500000.0},
    )
    assert text.startswith("# name: Tight stops\n# notes: k_stop halved\n")
    assert "# multiple of rv_5m * mid placed beyond the extreme" in text
    assert "  k_stop: 0.5" in text
    assert "    core: 500000.0" in text
    # Nothing the owner did not change.
    assert "k_target" not in text
    assert "risk_frac" not in text


def test_a_written_config_reads_back_as_what_was_written(tmp_path: Path) -> None:
    overrides = {
        "intent.k_stop": 0.5,
        "exhaustion.mode": "rule",
        "cascade.liq_z_min": None,
        "cascade.liq_usd_min.core": 500000.0,
        "cascade.liq_usd_min.major": 250000.0,
        "cascade.liq_usd_min.minor": 100000.0,
    }
    path = save_config(tmp_path / "tight.yaml", "Tight stops", "k_stop halved", overrides)
    back = read_config(path)
    assert back.name == "Tight stops"
    assert back.overrides == overrides


def test_a_written_config_is_one_the_loader_accepts(tmp_path: Path) -> None:
    """A file the lab saves has to be a file `liqfade backtest --config` can load."""
    from liqfade.config import load_settings

    path = save_config(tmp_path / "tight.yaml", "Tight stops", "", {"intent.k_stop": 0.5})
    settings = load_settings("paper", extra_files=[path])
    assert settings.intent.k_stop == 0.5


def test_duplicate_copies_the_overrides_under_a_new_name(tmp_path: Path) -> None:
    source = save_config(tmp_path / "base.yaml", "Base", "the first one", {"intent.k_stop": 0.5})
    copy = duplicate_config(source, "Base wider stop")
    assert copy != source
    assert copy.exists()
    duplicated = read_config(copy)
    assert duplicated.name == "Base wider stop"
    assert duplicated.overrides == {"intent.k_stop": 0.5}
    assert "copied from base.yaml" in duplicated.notes


def test_duplicate_refuses_to_land_on_a_file_that_exists(tmp_path: Path) -> None:
    save_config(tmp_path / "base.yaml", "Base", "", {"intent.k_stop": 0.5})
    save_config(tmp_path / "taken.yaml", "Taken", "", {"intent.k_stop": 0.6})
    with pytest.raises(FileExistsError):
        duplicate_config(tmp_path / "base.yaml", "taken")


def test_delete_removes_the_file(tmp_path: Path) -> None:
    path = save_config(tmp_path / "gone.yaml", "Gone", "", {"intent.k_stop": 0.5})
    delete_config(path)
    assert not path.exists()


def test_the_lab_refuses_to_write_outside_its_own_directory(tmp_path: Path) -> None:
    """CLAUDE.md: nothing in the lab may touch config/mainnet.yaml."""
    from liqfade.lab.configs import LabConfigError

    with pytest.raises(LabConfigError):
        save_config(tmp_path / ".." / "mainnet.yaml", "Sneaky", "", {"intent.k_stop": 0.5})
