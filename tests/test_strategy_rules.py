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
from liqfade.strategy.rules import (
    LEVEL_RULES,
    RULES,
    LevelReclaim,
    MinCascadeNotional,
    OiFlush,
    VwapStretch,
    ZoneProximity,
    build_enabled_rules,
    level_rules_enabled,
)
from liqfade.strategy.types import CascadeState, FeatureRow

SettingsFactory = Callable[..., Settings]


def cascade(**over: float | str) -> CascadeState:
    values: dict[str, float | str] = {
        "side": "long",
        "anchor_px": 100.0,
        "extreme_px": 98.0,
        "total_liq_usd": 1_000_000.0,
    }
    values.update(over)
    return CascadeState(
        coin="BTC",
        start_ts=0,
        extreme_ts=0,
        **values,  # type: ignore[arg-type]
    )


def test_registry_and_config_name_the_same_rules() -> None:
    """A rule with no config section cannot be enabled; a section with no class does nothing.

    Either half alone is a rule that silently never runs, which is worse than one that
    fails: the owner reads a report for a condition that was never evaluated.
    """
    assert set(RULES) == set(StrategyRules.model_fields)


def test_every_level_rule_is_a_real_rule_and_switches_the_join_on() -> None:
    """A name in `LEVEL_RULES` that is not a rule turns on an expensive join for nothing.

    The other direction is the one that costs a day: a rule that reads `support_px` and is
    missing from this tuple sees nulls on every row and fails every candidate, which reads
    like a measurement rather than like a bug.
    """
    assert set(LEVEL_RULES) <= set(RULES)
    assert not level_rules_enabled(load_settings("paper"))


def test_every_rule_ships_disabled() -> None:
    """Q7 said no to zones and nothing has said yes to the other. Both default off.

    Reads the real `config/default.yaml` rather than a fixture, because the thing being
    asserted is what the owner gets on a clean checkout.
    """
    rules = load_settings("paper").strategy.rules
    for name in RULES:
        assert not getattr(rules, name).enabled, f"{name} ships enabled"


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


def test_the_registry_holds_exactly_the_rules_this_phase_ships() -> None:
    """Pinned by name so adding a rule is a deliberate edit here as well as there.

    Every one of these is a switch the owner turns on in a backtest config, and a report
    naming a rule the reader cannot find in this list is a report about something else.
    """
    assert set(RULES) == {
        "zone_proximity",
        "min_cascade_notional",
        "oi_flush",
        "vwap_stretch",
        "level_reclaim",
    }


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


class TestOiFlushUnit:
    """Open interest fell over the rv window, and optionally had built up over the hour."""

    def rule(self, drop: float = 0.01, build: float | None = None) -> OiFlush:
        from liqfade.config.models import OiFlushRule

        return OiFlush(OiFlushRule(enabled=True, drop_min_frac=drop, build_min_frac=build))

    def row(self, **over: float | None) -> FeatureRow:
        values: dict[str, float | None] = {
            "mid": 98.0,
            "open_interest": 1_000_000.0,
            "oi_delta_rv": -0.02,
            "oi_delta_build": 0.05,
        }
        values.update(over)
        return FeatureRow(coin="BTC", ts_ms=0, **values)  # type: ignore[arg-type]

    def test_a_drop_past_the_floor_passes(self) -> None:
        verdict = self.rule().evaluate(self.row(), cascade())
        assert verdict.passed
        assert verdict.values["oi_delta_rv"] == -0.02
        assert verdict.values["open_interest"] == 1_000_000.0

    def test_a_shallower_drop_fails(self) -> None:
        assert not self.rule(drop=0.05).evaluate(self.row(), cascade()).passed

    def test_open_interest_rising_fails_however_far(self) -> None:
        """The sign is the whole claim: a build is not a flush."""
        assert not self.rule().evaluate(self.row(oi_delta_rv=0.2), cascade()).passed

    def test_a_null_delta_fails_rather_than_passes(self) -> None:
        """Not seeing the open interest is not evidence that it was flushed."""
        assert not self.rule().evaluate(self.row(oi_delta_rv=None), cascade()).passed

    def test_the_build_half_is_skipped_when_it_is_null(self) -> None:
        assert self.rule(build=None).evaluate(self.row(oi_delta_build=-0.5), cascade()).passed

    def test_the_build_half_wants_open_interest_still_up_over_the_hour(self) -> None:
        assert self.rule(build=0.03).evaluate(self.row(), cascade()).passed
        assert not self.rule(build=0.06).evaluate(self.row(), cascade()).passed
        assert not self.rule(build=0.03).evaluate(self.row(oi_delta_build=None), cascade()).passed


class TestVwapStretchUnit:
    """Mid stretched past the VWAP, on the side the cascade pushed it."""

    def rule(self, reference: str = "day", min_disp_bps: float = 30.0) -> VwapStretch:
        from liqfade.config.models import VwapStretchRule

        return VwapStretch(
            VwapStretchRule(
                enabled=True,
                reference=reference,  # type: ignore[arg-type]
                min_disp_bps=min_disp_bps,
            )
        )

    def test_a_long_fade_wants_price_below_the_session_vwap(self) -> None:
        """Longs were liquidated, price fell, we buy: the displacement is negative."""
        row = FeatureRow(coin="BTC", ts_ms=0, mid=98.0, disp_day_bps=-45.0)
        verdict = self.rule().evaluate(row, cascade())
        assert verdict.passed
        assert verdict.values["disp_bps"] == -45.0

    def test_price_above_the_vwap_fails_a_long_fade(self) -> None:
        row = FeatureRow(coin="BTC", ts_ms=0, mid=98.0, disp_day_bps=45.0)
        assert not self.rule().evaluate(row, cascade()).passed

    def test_a_short_fade_wants_the_mirror(self) -> None:
        row = FeatureRow(coin="BTC", ts_ms=0, mid=102.0, disp_day_bps=45.0)
        assert self.rule().evaluate(row, cascade(side="short")).passed
        low = FeatureRow(coin="BTC", ts_ms=0, mid=102.0, disp_day_bps=-45.0)
        assert not self.rule().evaluate(low, cascade(side="short")).passed

    def test_a_stretch_short_of_the_threshold_fails(self) -> None:
        row = FeatureRow(coin="BTC", ts_ms=0, mid=98.0, disp_day_bps=-29.0)
        assert not self.rule().evaluate(row, cascade()).passed

    def test_the_anchor_reference_reads_the_other_column(self) -> None:
        row = FeatureRow(coin="BTC", ts_ms=0, mid=98.0, disp_day_bps=0.0, disp_anchor_bps=-45.0)
        assert self.rule(reference="anchor").evaluate(row, cascade()).passed
        assert not self.rule(reference="day").evaluate(row, cascade()).passed

    def test_no_vwap_fails_rather_than_passes(self) -> None:
        """Early in a UTC day the session VWAP does not exist yet."""
        row = FeatureRow(coin="BTC", ts_ms=0, mid=98.0)
        verdict = self.rule().evaluate(row, cascade())
        assert not verdict.passed
        assert "disp_bps" not in verdict.values


class TestLevelReclaimUnit:
    """Through the level, then back above it. The swing failure and the three tap."""

    def rule(self, sweep: float = 5.0, reclaim: float = 0.0) -> LevelReclaim:
        from liqfade.config.models import LevelReclaimRule

        return LevelReclaim(
            LevelReclaimRule(enabled=True, sweep_min_bps=sweep, reclaim_min_bps=reclaim)
        )

    def test_a_swept_and_reclaimed_support_passes(self) -> None:
        """Support at 99, the cascade printed 98 (101 bps through), mid is back at 99.5."""
        row = FeatureRow(coin="BTC", ts_ms=0, mid=99.5, support_px=99.0)
        verdict = self.rule().evaluate(row, cascade())
        assert verdict.passed
        assert verdict.values["sweep_bps"] == pytest.approx(1e4 * 1.0 / 99.0)
        assert verdict.values["reclaim_bps"] == pytest.approx(1e4 * 0.5 / 99.0)

    def test_a_level_the_cascade_never_reached_fails(self) -> None:
        """Support at 97 is below the extreme, so nothing was swept."""
        row = FeatureRow(coin="BTC", ts_ms=0, mid=99.5, support_px=97.0)
        assert not self.rule().evaluate(row, cascade()).passed

    def test_price_still_under_the_level_fails(self) -> None:
        """Through the low and still below it is a cascade in progress, not a reclaim."""
        row = FeatureRow(coin="BTC", ts_ms=0, mid=98.5, support_px=99.0)
        assert not self.rule().evaluate(row, cascade()).passed

    def test_a_sweep_shallower_than_the_threshold_fails(self) -> None:
        """The extreme is 1.02 bps through a level at 98.01; the floor here is 5."""
        row = FeatureRow(coin="BTC", ts_ms=0, mid=99.5, support_px=98.01)
        assert not self.rule().evaluate(row, cascade()).passed

    def test_the_reclaim_threshold_is_applied_when_it_is_set(self) -> None:
        row = FeatureRow(coin="BTC", ts_ms=0, mid=99.5, support_px=99.0)
        assert self.rule(reclaim=50.0).evaluate(row, cascade()).passed
        assert not self.rule(reclaim=51.0).evaluate(row, cascade()).passed

    def test_a_short_cascade_reads_resistance_and_mirrors_every_sign(self) -> None:
        state = cascade(side="short", extreme_px=102.0, anchor_px=100.0)
        row = FeatureRow(coin="BTC", ts_ms=0, mid=100.5, resistance_px=101.0)
        verdict = self.rule().evaluate(row, state)
        assert verdict.passed
        assert verdict.values["sweep_bps"] == pytest.approx(1e4 * 1.0 / 101.0)
        # The support level is not consulted at all on this side.
        assert (
            not self.rule()
            .evaluate(FeatureRow(coin="BTC", ts_ms=0, mid=100.5, support_px=101.0), state)
            .passed
        )

    def test_no_level_fails_rather_than_passes(self) -> None:
        """No level within features.levels.max_dist_bps means nothing was swept."""
        row = FeatureRow(coin="BTC", ts_ms=0, mid=99.5)
        verdict = self.rule().evaluate(row, cascade())
        assert not verdict.passed
        assert "level_px" not in verdict.values


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
