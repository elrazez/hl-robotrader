"""Config layering, validation and the live trading gate."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from liqfade.config import ConfigError, flatten, load_settings, resolve
from liqfade.config.loader import CONFIG_DIR


def test_every_environment_loads() -> None:
    for env in ("paper", "testnet", "mainnet"):
        settings = load_settings(env, environ={})
        assert settings.env == env


def test_unknown_environment_is_an_error() -> None:
    with pytest.raises(ConfigError, match="unknown environment"):
        load_settings("staging", environ={})


def test_env_file_overrides_default() -> None:
    default_risk = load_settings("paper", environ={}).sizing.risk_frac
    mainnet = load_settings("mainnet", environ={})
    assert mainnet.sizing.risk_frac < default_risk
    assert mainnet.risk.max_concurrent_positions == 2


def test_envvar_overrides_files_and_is_reported_as_the_source() -> None:
    environ = {"LIQFADE_SIZING__RISK_FRAC": "0.001", "LIQFADE_PANEL__PORT": "9999"}
    settings = load_settings("paper", environ=environ)
    assert settings.sizing.risk_frac == 0.001
    assert settings.panel.port == 9999

    _, sources = resolve("paper", environ=environ)
    assert sources["sizing.risk_frac"] == "envvar"
    assert sources["panel.port"] == "envvar"
    assert sources["cascade.liq_z_min"] == "default.yaml"
    assert sources["risk.max_concurrent_positions"] == "default.yaml"


def test_an_empty_envvar_is_not_set_and_leaves_the_yaml_value_alone() -> None:
    # .env.example ships `LIQFADE_SYNC_HOST=`; copied to a box as-is it must not
    # become sync.host = None and refuse every process at startup.
    environ = {"LIQFADE_SYNC_HOST": "", "LIQFADE_PANEL__PORT": "  "}
    settings = load_settings("paper", environ=environ)
    assert settings.sync.host == ""
    assert settings.panel.port == load_settings("paper", environ={}).panel.port
    _, sources = resolve("paper", environ=environ)
    assert sources["panel.port"] == "default.yaml"


def test_env_file_is_reported_as_the_source_when_it_wins() -> None:
    _, sources = resolve("mainnet", environ={})
    assert sources["sizing.risk_frac"] == "mainnet.yaml"
    assert sources["cascade.liq_z_min"] == "default.yaml"


def test_liqfade_env_selects_the_environment_file() -> None:
    assert load_settings(environ={"LIQFADE_ENV": "testnet"}).env == "testnet"


def test_extra_config_file_layers_between_env_and_overrides(tmp_path: Path) -> None:
    extra = tmp_path / "run.yaml"
    extra.write_text("backtest:\n  latency_ms: 3000\n")
    settings = load_settings("paper", extra_files=[extra], environ={})
    assert settings.backtest.latency_ms == 3000
    _, sources = resolve("paper", extra_files=[extra], environ={})
    assert sources["backtest.latency_ms"] == "run.yaml"


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    extra = tmp_path / "typo.yaml"
    extra.write_text("sizing:\n  risk_fraq: 0.01\n")
    with pytest.raises(ConfigError, match="invalid configuration"):
        load_settings("paper", extra_files=[extra], environ={})


def test_out_of_range_value_is_rejected(tmp_path: Path) -> None:
    extra = tmp_path / "bad.yaml"
    extra.write_text("sizing:\n  risk_frac: 2.0\n")
    with pytest.raises(ConfigError, match="invalid configuration"):
        load_settings("paper", extra_files=[extra], environ={})


def test_cross_field_validators_reject_inconsistent_risk(tmp_path: Path) -> None:
    extra = tmp_path / "stale.yaml"
    extra.write_text("risk:\n  stale_coin_s: 90.0\n")
    with pytest.raises(ConfigError, match="stale thresholds"):
        load_settings("paper", extra_files=[extra], environ={})


def test_a_cascade_with_no_liquidation_condition_is_rejected(tmp_path: Path) -> None:
    """Both of `cascade.liq_z_min` and `cascade.liq_usd_min` null admits every row.

    Each is allowed to be null on its own, which is what makes a notional-only run
    expressible, so the only thing standing between the owner and a config that trades
    every second of the tape is this validator.
    """
    extra = tmp_path / "no_trigger.yaml"
    extra.write_text("cascade:\n  liq_z_min: null\n  liq_usd_min: null\n")
    with pytest.raises(ConfigError, match="liq_z_min or liq_usd_min"):
        load_settings("paper", extra_files=[extra], environ={})


def test_flatten_produces_dotted_keys() -> None:
    merged, _ = resolve("paper", environ={})
    flat = flatten(merged)
    assert flat["sizing.risk_frac"] == 0.005
    assert flat["panel.host"] == "127.0.0.1"


# --- The live trading gate: CLAUDE.md rule 3. All three conditions must hold. ---


def _mainnet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live: str | None):
    monkeypatch.delenv("LIQFADE_LIVE_TRADING", raising=False)
    if live is not None:
        monkeypatch.setenv("LIQFADE_LIVE_TRADING", live)
    return load_settings(
        "mainnet",
        environ={"LIQFADE_PATHS__DATA_DIR": str(tmp_path)},
    )


def test_live_trading_blocked_when_env_is_not_mainnet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIQFADE_LIVE_TRADING", "1")
    settings = load_settings("paper", environ={"LIQFADE_PATHS__DATA_DIR": str(tmp_path)})
    assert settings.live_trading_enabled is False
    assert settings.live_trading_block_reason() == "env is paper, not mainnet"


def test_live_trading_blocked_when_flag_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _mainnet(tmp_path, monkeypatch, live=None)
    assert settings.live_trading_enabled is False
    assert settings.live_trading_block_reason() == "LIQFADE_LIVE_TRADING is not set to 1"


def test_live_trading_blocked_when_flag_is_not_exactly_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for value in ("0", "true", "yes", "", "01"):
        settings = _mainnet(tmp_path, monkeypatch, live=value)
        assert settings.live_trading_enabled is False


def test_live_trading_blocked_when_kill_file_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _mainnet(tmp_path, monkeypatch, live="1")
    assert settings.live_trading_enabled is True  # all three conditions hold

    settings.kill_file.parent.mkdir(parents=True, exist_ok=True)
    settings.kill_file.write_text("halt")
    assert settings.live_trading_enabled is False
    assert "kill switch file present" in (settings.live_trading_block_reason() or "")

    settings.kill_file.unlink()
    assert settings.live_trading_enabled is True  # and it recovers when the file goes


def test_live_trading_blocked_when_the_kill_file_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kill switch fails closed: anything but a clean "not found" counts as present.

    Path.exists() answers False for every OSError, so an unmounted or unreadable data_dir
    used to read as "no kill file" and let live trading through. Here data_dir is a plain
    file, so stat on data_dir/KILL raises ENOTDIR rather than ENOENT.
    """
    settings = _mainnet(tmp_path, monkeypatch, live="1")
    assert settings.live_trading_enabled is True

    data_dir = settings.paths.data_dir
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.write_text("not a directory")

    assert settings.kill_switch_present is True
    assert settings.live_trading_enabled is False
    assert "kill switch file present" in (settings.live_trading_block_reason() or "")


def test_default_environment_is_paper() -> None:
    assert load_settings(environ={}).env == "paper"


def test_secret_env_vars_are_never_merged_into_the_config() -> None:
    """A secret must not end up in the config tree, where `config show` would print it."""
    merged, _ = resolve(
        "paper",
        environ={
            "LIQFADE_PANEL_PASSWORD_HASH": "$2b$12$deadbeef",
            "LIQFADE_PANEL_SESSION_SECRET": "super-secret",
            "LIQFADE_LIVE_TRADING": "1",
        },
    )
    rendered = repr(flatten(merged))
    assert "deadbeef" not in rendered
    assert "super-secret" not in rendered
    assert "live_trading" not in merged


def test_an_overrides_file_may_not_choose_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """overrides.yaml is written by the panel, and the panel may not enable live trading.

    The environment file is picked before this layer merges, so `env: mainnet` here used
    to give env == "mainnet" (the gate open) while mainnet.yaml was never loaded and
    paper's looser sizing and risk limits stayed in place.
    """
    shutil.copytree(CONFIG_DIR, tmp_path / "config")
    (tmp_path / "config" / "overrides.yaml").write_text("env: mainnet\n")
    monkeypatch.setenv("LIQFADE_CONFIG_DIR", str(tmp_path / "config"))
    with pytest.raises(ConfigError, match="Only --env and LIQFADE_ENV"):
        load_settings(environ={})


def test_an_explicit_env_argument_still_beats_liqfade_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutil.copytree(CONFIG_DIR, tmp_path / "config")
    monkeypatch.setenv("LIQFADE_CONFIG_DIR", str(tmp_path / "config"))
    settings = load_settings("paper", environ={"LIQFADE_ENV": "testnet"})
    assert settings.env == "paper"
