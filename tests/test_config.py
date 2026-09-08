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


def test_the_shipped_backtest_configs_load_and_say_what_they_switch_on() -> None:
    """Every run config in `config/backtest/` must validate against the real models.

    A run config is the only place the owner turns rules on, and a typo in one is a rule
    that silently never ran. `extra="forbid"` makes an unknown key an error; this makes a
    known key with the wrong value visible, by naming the exact rule set v1 claims.
    """
    for path in sorted(Path("config/backtest").glob("*.yaml")):
        load_settings("paper", extra_files=[path], environ={})

    v1 = load_settings(
        "paper", extra_files=[Path("config/backtest/v1_confluence.yaml")], environ={}
    )
    rules = v1.strategy.rules
    names = type(rules).model_fields
    assert {name for name in names if getattr(rules, name).enabled} == {
        "min_cascade_notional",
        "oi_flush",
        "vwap_stretch",
        "level_reclaim",
        "zone_proximity",
    }
    # The liquidation is the only thing that blocks on its own; the rest are votes.
    assert {
        name for name in names if getattr(rules, name).enabled and getattr(rules, name).required
    } == {"min_cascade_notional"}
    assert v1.strategy.min_confluence == 2
    assert v1.cascade.liq_z_min is None
    assert v1.cascade.liq_usd_min is not None
    assert v1.cascade.liq_usd_min.core == 500_000.0
    assert v1.exhaustion.mode == "rule"
    # The exhaustion rule and level_reclaim both enter after the bounce has started, so the
    # target has to be measured from the fill rather than from the low.
    assert v1.intent.target_from == "entry"
    # The default is what the robust z fix restored; overriding it back would hide that.
    assert v1.features.z_robust is True

    # Tuning runs only: the account halts are off in v1 so the whole tape replays, and the
    # live limits are untouched. A run config that quietly relaxed the shipped ones, or a
    # default that drifted to match, would both be invisible without this pair.
    assert (v1.risk.max_dd_frac, v1.risk.max_daily_loss_frac) == (1.0, 1.0)
    assert v1.risk.cooldown_after_streak_min == 0
    # The gates that shape which trades are taken, rather than cutting the tape short, stay.
    base = load_settings("paper", environ={})
    assert v1.risk.max_cluster_exposure_frac == base.risk.max_cluster_exposure_frac
    assert v1.risk.coin_cooldown_after_loss_min == base.risk.coin_cooldown_after_loss_min
    assert (base.risk.max_dd_frac, base.risk.max_daily_loss_frac) == (0.10, 0.03)


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

    Written under data_dir, which is where this layer lives; see
    TestTheOverridesLayerIsUnderDataDir below for why.
    """
    shutil.copytree(CONFIG_DIR, tmp_path / "config")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "overrides.yaml").write_text("env: mainnet\n")
    monkeypatch.setenv("LIQFADE_CONFIG_DIR", str(tmp_path / "config"))
    with pytest.raises(ConfigError, match="Only --env and LIQFADE_ENV"):
        load_settings(environ={"LIQFADE_PATHS__DATA_DIR": str(tmp_path / "data")})


def test_an_explicit_env_argument_still_beats_liqfade_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutil.copytree(CONFIG_DIR, tmp_path / "config")
    monkeypatch.setenv("LIQFADE_CONFIG_DIR", str(tmp_path / "config"))
    settings = load_settings("paper", environ={"LIQFADE_ENV": "testnet"})
    assert settings.env == "paper"


def test_the_info_and_exchange_urls_default_to_the_one_api_url() -> None:
    """Splitting them costs nothing while they are the same host, and buys the guard."""
    from liqfade.config import load_settings

    hl = load_settings("paper").venues.hyperliquid

    assert hl.info_base == hl.api_url
    assert hl.exchange_base == hl.api_url


def test_the_exchange_url_can_be_pointed_apart_from_the_info_url() -> None:
    """Reading is public and can come from anywhere. Signing must go where we meant.

    They are one host today. They are two fields because the decision about where an
    order is sent is not the same decision as where a price is read, and one of them
    costs money when it is wrong.
    """
    from liqfade.config import resolve
    from liqfade.config.models import Settings

    merged, _ = resolve("paper")
    merged["venues"]["hyperliquid"]["info_url"] = "https://a-node.example"

    hl = Settings.model_validate(merged).venues.hyperliquid

    assert hl.info_base == "https://a-node.example"
    assert hl.exchange_base == hl.api_url


def test_the_layers_are_reported_in_merge_order_with_what_each_one_set() -> None:
    """The panel has to show what a layer said, not only which layer won.

    `flatten(resolve(...))` gives the winning value and its source label. A settings page
    that offers to change a number has to say what it is changing it from and what the
    layer underneath will go back to saying if the override is removed.
    """
    from liqfade.config import config_layers

    found = config_layers("paper")

    assert [layer.name for layer in found] == [
        "default.yaml",
        "paper.yaml",
        "overrides.yaml",
        "envvar",
    ]
    assert found[0].exists is True
    assert "risk.kill_deadline_s" in found[0].values
    # A layer reports only what it sets, not the merged result.
    assert "risk.kill_deadline_s" not in found[1].values


def test_a_layer_that_is_not_there_is_reported_as_empty_not_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """overrides.yaml legitimately does not exist until the panel writes one."""
    from liqfade.config import config_layers

    monkeypatch.setenv("LIQFADE_SIZING__RISK_FRAC", "0.004")
    layers = {layer.name: layer for layer in config_layers("paper")}

    assert layers["overrides.yaml"].values == {}
    assert layers["envvar"].values["sizing.risk_frac"] == 0.004


# --- the overrides layer lives under data_dir --------------------------------------


class TestTheOverridesLayerIsUnderDataDir:
    """`overrides.yaml` is the one config layer written at runtime, so it lives with the
    data rather than with the code.

    Three processes share one data dir on the box and each runs from its own checkout:
    the recorder and the panel from `/srv/liqfade/app`, the paper loop from
    `/srv/liqfade/paper`. A file under `config/` would be unwritable by the panel, whose
    unit is `ProtectSystem=strict` with only the data dir writable, and invisible to the
    other checkout even if it were written. See docs/DECISIONS.md, 2026-09-08.
    """

    @pytest.fixture
    def config_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        shutil.copytree(CONFIG_DIR, tmp_path / "config")
        monkeypatch.setenv("LIQFADE_CONFIG_DIR", str(tmp_path / "config"))
        return tmp_path / "config"

    def test_it_is_read_from_data_dir(self, config_dir: Path, tmp_path: Path) -> None:
        data = tmp_path / "data"
        data.mkdir()
        (data / "overrides.yaml").write_text("sizing:\n  risk_frac: 0.004\n")

        settings = load_settings("paper", environ={"LIQFADE_PATHS__DATA_DIR": str(data)})

        assert settings.sizing.risk_frac == 0.004

    def test_a_file_left_in_the_config_directory_is_not_read(
        self, config_dir: Path, tmp_path: Path
    ) -> None:
        """There is one overrides file, not two, so nothing can quietly win from code."""
        (config_dir / "overrides.yaml").write_text("sizing:\n  risk_frac: 0.004\n")
        data = tmp_path / "data"
        data.mkdir()

        settings = load_settings("paper", environ={"LIQFADE_PATHS__DATA_DIR": str(data)})

        assert settings.sizing.risk_frac == 0.005  # default.yaml's, untouched

    def test_an_env_var_moving_data_dir_moves_the_overrides_file(
        self, config_dir: Path, tmp_path: Path
    ) -> None:
        """The env layer merges last and so wins, and it can name the data dir.

        The file has to be found before it can be merged, so data_dir is resolved from
        every other layer first. A box that sets LIQFADE_DATA_DIR in its env file, which
        is how this one is configured, must not have its overrides looked for elsewhere.
        """
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "overrides.yaml").write_text("sizing:\n  risk_frac: 0.003\n")

        settings = load_settings("paper", environ={"LIQFADE_DATA_DIR": str(elsewhere)})

        assert settings.sizing.risk_frac == 0.003

    def test_config_layers_names_the_path_it_looked_at(
        self, config_dir: Path, tmp_path: Path
    ) -> None:
        """The panel shows the owner where the file it writes actually lives."""
        from liqfade.config import config_layers

        data = tmp_path / "data"
        data.mkdir()
        layers = {
            layer.name: layer
            for layer in config_layers("paper", environ={"LIQFADE_PATHS__DATA_DIR": str(data)})
        }

        assert layers["overrides.yaml"].path == str(data / "overrides.yaml")
        assert layers["overrides.yaml"].exists is False
        assert layers["overrides.yaml"].values == {}
