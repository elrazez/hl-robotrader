"""The rule registry's contract with config.

The engine's behaviour with rules on and off is tested through the engine seam in
`tests/test_strategy_engine.py`. What is left here is the wiring that makes a new rule
impossible to half-add: a class with no config section, or a config section with no class,
or a typo in a config file's rule name.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from liqfade.config import ConfigError, load_settings
from liqfade.config.models import Settings, StrategyRules
from liqfade.strategy.rules import RULES, MinCascadeNotional, ZoneProximity, build_enabled_rules
from liqfade.strategy.types import CascadeState, FeatureRow

SettingsFactory = Callable[..., Settings]


def cascade(**over: float) -> CascadeState:
    values: dict[str, float] = {"extreme_px": 98.0, "total_liq_usd": 1_000_000.0}
    values.update(over)
    return CascadeState(
        coin="BTC",
        side="long",
        start_ts=0,
        anchor_px=100.0,
        extreme_ts=0,
        **values,  # type: ignore[arg-type]
    )


def test_registry_and_config_name_the_same_rules() -> None:
    """A rule with no config section cannot be enabled; a section with no class does nothing.

    Either half alone is a rule that silently never runs, which is worse than one that
    fails: the owner reads a report for a condition that was never evaluated.
    """
    assert set(RULES) == set(StrategyRules.model_fields)


def test_every_rule_ships_disabled() -> None:
    """Q7 said no to zones and nothing has said yes to the other. Both default off.

    Reads the real `config/default.yaml` rather than a fixture, because the thing being
    asserted is what the owner gets on a clean checkout.
    """
    rules = load_settings("paper").strategy.rules
    assert not rules.zone_proximity.enabled
    assert not rules.min_cascade_notional.enabled


def test_an_unknown_rule_name_fails_validation_loudly(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A misspelled rule must be an error, never a rule that quietly never ran."""
    import shutil
    from pathlib import Path

    source = Path("config")
    shutil.copytree(source, tmp_path / "config")
    (tmp_path / "config" / "overrides.yaml").write_text(
        "strategy:\n  rules:\n    zone_proximty:\n      enabled: true\n"
    )
    monkeypatch.setenv("LIQFADE_CONFIG_DIR", str(tmp_path / "config"))
    with pytest.raises(ConfigError, match="zone_proximty"):
        load_settings("paper")


def test_build_enabled_returns_only_enabled_rules_in_name_order(
    settings_factory: SettingsFactory,
) -> None:
    """Name order fixes which rule gets blamed when two would both have failed."""
    assert build_enabled_rules(settings_factory()) == []

    both = settings_factory(
        **{
            "strategy.rules.zone_proximity.enabled": True,
            "strategy.rules.min_cascade_notional.enabled": True,
        }
    )
    assert [type(rule) for rule in build_enabled_rules(both)] == [
        MinCascadeNotional,
        ZoneProximity,
    ]


class TestZoneProximityUnit:
    """The rule in isolation, against a constructed level set."""

    def rule(self, width_bps: float = 15.0) -> ZoneProximity:
        from liqfade.config.models import ZoneProximityRule

        return ZoneProximity(ZoneProximityRule(enabled=True, zone_width_bps=width_bps))

    def test_measures_from_the_extreme_not_the_current_mid(self) -> None:
        """The question is whether the cascade ran into a level.

        Mid here is 99.5, which is 153 bps from the level; the extreme is 98.0, which is
        1.02 bps from it. Measuring from mid would answer the wrong question by the width
        of the cascade, so the rule must pass.
        """
        row = FeatureRow(coin="BTC", ts_ms=0, mid=99.5, support_px=97.99)
        verdict = self.rule().evaluate(row, cascade())
        assert verdict.passed
        assert verdict.values["nearest_level_bps"] == pytest.approx(1e4 * 0.01 / 98.0)

    def test_takes_the_nearer_of_support_and_resistance(self) -> None:
        row = FeatureRow(coin="BTC", ts_ms=0, mid=98.0, support_px=90.0, resistance_px=98.05)
        verdict = self.rule().evaluate(row, cascade())
        assert verdict.passed
        assert verdict.values["nearest_level_bps"] == pytest.approx(1e4 * 0.05 / 98.0)

    def test_no_level_fails_rather_than_passes(self) -> None:
        """No level nearby is the definition of not being in a zone."""
        row = FeatureRow(coin="BTC", ts_ms=0, mid=98.0)
        verdict = self.rule().evaluate(row, cascade())
        assert not verdict.passed
        assert "nearest_level_bps" not in verdict.values

    def test_a_far_level_fails(self) -> None:
        row = FeatureRow(coin="BTC", ts_ms=0, mid=98.0, support_px=97.0)
        assert not self.rule().evaluate(row, cascade()).passed


class TestMinCascadeNotionalUnit:
    def rule(self, floor: float) -> MinCascadeNotional:
        from liqfade.config.models import MinCascadeNotionalRule

        return MinCascadeNotional(MinCascadeNotionalRule(enabled=True, min_notional_usd=floor))

    def test_passes_at_or_above_the_floor(self) -> None:
        row = FeatureRow(coin="BTC", ts_ms=0, mid=98.0)
        assert self.rule(1_000_000.0).evaluate(row, cascade()).passed

    def test_fails_below_the_floor(self) -> None:
        row = FeatureRow(coin="BTC", ts_ms=0, mid=98.0)
        verdict = self.rule(1_000_001.0).evaluate(row, cascade())
        assert not verdict.passed
        assert verdict.values["cascade_notional_usd"] == 1_000_000.0
