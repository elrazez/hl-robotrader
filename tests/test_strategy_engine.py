"""The engine seam: signal to intent, the rule layer, the filters and no-lookahead.

Drives `StrategyEngine.on_tick` with hand-built rows so the intent's arithmetic can be
checked against `docs/STRATEGY_AND_RISK.md` section 3 by hand. The target and stop
expectations are worked out from the config values rather than by calling the engine twice.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

import pytest
from strategy_rows import START_MS, TICK_MS, falling_row, quiet_row

from liqfade.config.models import Settings
from liqfade.strategy.engine import StrategyEngine
from liqfade.strategy.types import AssetMeta, Intent

SettingsFactory = Callable[..., Settings]

BTC_META = AssetMeta(coin="BTC", sz_decimals=5, max_leverage=40.0, tier="core", listing_days=900.0)
META: Mapping[str, AssetMeta] = {"BTC": BTC_META}


def run_to_signal(
    engine: StrategyEngine, *, mid_after: float = 98.0, ticks: int = 600
) -> list[Intent]:
    """Start a long cascade at 100, drop to `mid_after`, then wait out the fixed delay."""
    engine.on_tick(START_MS, {"BTC": falling_row(START_MS, 100.0)})
    intents: list[Intent] = []
    for step in range(1, ticks):
        ts = START_MS + step * TICK_MS
        intents.extend(engine.on_tick(ts, {"BTC": quiet_row(ts, mid_after)}))
    return intents


class TestIntent:
    def test_a_cascade_becomes_one_intent(self, settings_factory: SettingsFactory) -> None:
        engine = StrategyEngine(settings_factory(), META)
        intents = run_to_signal(engine)
        assert len(intents) == 1

    def test_target_and_stop_come_from_config(self, settings_factory: SettingsFactory) -> None:
        settings = settings_factory()
        engine = StrategyEngine(settings, META)
        (intent,) = run_to_signal(engine)

        anchor, extreme, rv, mid = 100.0, 98.0, 0.002, 98.0
        # target = extreme + k_target * (anchor - extreme), k_target 0.8 from Q8.
        assert intent.target_px == pytest.approx(
            extreme + settings.intent.k_target * (anchor - extreme)
        )
        # stop sits k_stop * rv * mid BELOW the low a long fade is protecting.
        assert intent.stop_px == pytest.approx(extreme - settings.intent.k_stop * rv * mid)
        assert intent.stop_px < extreme, "a stop inside the extreme is not a stop"
        assert intent.time_stop_s == settings.intent.time_stop_s

    def test_fading_a_long_liquidation_goes_long(self, settings_factory: SettingsFactory) -> None:
        engine = StrategyEngine(settings_factory(), META)
        (intent,) = run_to_signal(engine)
        assert intent.side == "long"
        assert intent.target_px > intent.entry_px, "a long's target is above its entry"

    def test_fading_a_short_liquidation_goes_short(self, settings_factory: SettingsFactory) -> None:
        settings = settings_factory()
        engine = StrategyEngine(settings, META)
        rising = falling_row(
            START_MS,
            100.0,
            liq_z_long_w=0.0,
            liq_z_short_w=4.0,
            liq_imbalance_w=-0.8,
            ret_w=0.01,
            liq_short_usd_peak=1_000_000.0,
        )
        engine.on_tick(START_MS, {"BTC": rising})
        intents: list[Intent] = []
        for step in range(1, 600):
            ts = START_MS + step * TICK_MS
            intents.extend(engine.on_tick(ts, {"BTC": quiet_row(ts, 102.0)}))
        (intent,) = intents
        assert intent.side == "short"
        assert intent.target_px < intent.entry_px
        assert intent.stop_px > 102.0, "a short fade's stop sits above the high"

    def test_urgency_is_high_for_a_large_cascade(self, settings_factory: SettingsFactory) -> None:
        settings = settings_factory()
        engine = StrategyEngine(settings, META)
        engine.on_tick(
            START_MS,
            {"BTC": falling_row(START_MS, 100.0, liq_z_long_w=settings.intent.urgency_liq_z)},
        )
        intents: list[Intent] = []
        for step in range(1, 600):
            ts = START_MS + step * TICK_MS
            intents.extend(engine.on_tick(ts, {"BTC": quiet_row(ts, 98.0)}))
        assert intents[0].urgency == "high"

    def test_explain_names_real_windows(self, settings_factory: SettingsFactory) -> None:
        """Telegram posts this mapping, so a reader needs to know which 60 seconds."""
        engine = StrategyEngine(settings_factory(), META)
        (intent,) = run_to_signal(engine)
        assert "liq_z_60s" in intent.explain
        assert "rv_300s" in intent.explain
        assert intent.explain["anchor_px"] == 100.0
        assert intent.explain["extreme_px"] == 98.0
        assert intent.explain["move_bps"] == pytest.approx(200.0)

    def test_no_volatility_reference_produces_no_intent(
        self, settings_factory: SettingsFactory
    ) -> None:
        """A stop guessed from nothing is worse than no trade."""
        engine = StrategyEngine(settings_factory(), META)
        engine.on_tick(START_MS, {"BTC": falling_row(START_MS, 100.0)})
        intents: list[Intent] = []
        for step in range(1, 600):
            ts = START_MS + step * TICK_MS
            intents.extend(engine.on_tick(ts, {"BTC": quiet_row(ts, 98.0, rv=None)}))
        assert intents == []
        assert engine.stats.unpriceable == 1

    def test_an_unknown_coin_is_never_traded(self, settings_factory: SettingsFactory) -> None:
        engine = StrategyEngine(settings_factory(), {})
        assert run_to_signal(engine) == []


class TestRuleLayer:
    def test_all_rules_disabled_is_the_bare_spec_behaviour(
        self, settings_factory: SettingsFactory
    ) -> None:
        """The golden baseline. A disabled rule must not change a single value."""
        bare = StrategyEngine(settings_factory(), META)
        (baseline,) = run_to_signal(bare)

        explicit = settings_factory(
            **{
                "strategy.rules.zone_proximity.enabled": False,
                "strategy.rules.min_cascade_notional.enabled": False,
            }
        )
        (other,) = run_to_signal(StrategyEngine(explicit, META))
        assert other == baseline

    def test_a_failing_rule_kills_the_intent_and_names_itself(
        self, settings_factory: SettingsFactory
    ) -> None:
        settings = settings_factory(
            **{
                "strategy.rules.min_cascade_notional.enabled": True,
                # The fixture cascade liquidates 4,000,000, so a floor above it must fail.
                "strategy.rules.min_cascade_notional.min_notional_usd": 9_000_000.0,
            }
        )
        engine = StrategyEngine(settings, META)
        assert run_to_signal(engine) == []

        (rejection,) = engine.take_rejections()
        assert rejection.reason == "min_cascade_notional"
        assert rejection.stage == "rule"
        assert rejection.explain["min_cascade_notional.cascade_notional_usd"] == 4_000_000.0

    def test_a_passing_rule_leaves_the_intent_and_its_values_in_explain(
        self, settings_factory: SettingsFactory
    ) -> None:
        settings = settings_factory(
            **{
                "strategy.rules.min_cascade_notional.enabled": True,
                "strategy.rules.min_cascade_notional.min_notional_usd": 1_000.0,
            }
        )
        engine = StrategyEngine(settings, META)
        (intent,) = run_to_signal(engine)
        assert intent.explain["min_cascade_notional.cascade_notional_usd"] == 4_000_000.0
        assert intent.explain["min_cascade_notional.floor_usd"] == 1_000.0

    def test_the_per_rule_table_counts_evaluated_passed_and_killed(
        self, settings_factory: SettingsFactory
    ) -> None:
        settings = settings_factory(
            **{
                "strategy.rules.min_cascade_notional.enabled": True,
                "strategy.rules.min_cascade_notional.min_notional_usd": 9_000_000.0,
            }
        )
        engine = StrategyEngine(settings, META)
        run_to_signal(engine)
        stats = engine.stats.rules["min_cascade_notional"]
        assert (stats.evaluated, stats.passed, stats.killed) == (1, 0, 1)
        assert engine.stats.rules["zone_proximity"].evaluated == 0, "a disabled rule never runs"

    def test_zone_proximity_passes_inside_the_zone(self, settings_factory: SettingsFactory) -> None:
        settings = settings_factory(
            **{
                "strategy.rules.zone_proximity.enabled": True,
                "strategy.rules.zone_proximity.zone_width_bps": 15.0,
            }
        )
        engine = StrategyEngine(settings, META)
        # The extreme is 98.0; a support at 97.99 is 1.02 bps away, inside 15.
        engine.on_tick(START_MS, {"BTC": falling_row(START_MS, 100.0)})
        intents: list[Intent] = []
        for step in range(1, 600):
            ts = START_MS + step * TICK_MS
            intents.extend(engine.on_tick(ts, {"BTC": quiet_row(ts, 98.0, support_px=97.99)}))
        (intent,) = intents
        assert intent.explain["zone_proximity.nearest_level_bps"] == pytest.approx(
            1e4 * 0.01 / 98.0
        )

    def test_zone_proximity_fails_outside_the_zone(self, settings_factory: SettingsFactory) -> None:
        settings = settings_factory(
            **{
                "strategy.rules.zone_proximity.enabled": True,
                "strategy.rules.zone_proximity.zone_width_bps": 15.0,
            }
        )
        engine = StrategyEngine(settings, META)
        engine.on_tick(START_MS, {"BTC": falling_row(START_MS, 100.0)})
        for step in range(1, 600):
            ts = START_MS + step * TICK_MS
            engine.on_tick(ts, {"BTC": quiet_row(ts, 98.0, support_px=90.0)})
        (rejection,) = engine.take_rejections()
        assert rejection.reason == "zone_proximity"

    def test_zone_proximity_fails_when_there_is_no_level(
        self, settings_factory: SettingsFactory
    ) -> None:
        """No level nearby is the definition of not being in a zone, so it fails."""
        settings = settings_factory(**{"strategy.rules.zone_proximity.enabled": True})
        engine = StrategyEngine(settings, META)
        assert run_to_signal(engine) == []
        (rejection,) = engine.take_rejections()
        assert rejection.reason == "zone_proximity"


class TestConfluence:
    """Optional rules are votes, not vetoes, and `min_confluence` is how many are needed.

    The setup a trader runs by hand takes tens of trades a week, which an all-AND stack
    of four corroborating conditions cannot produce: any one of them being absent is normal.
    `min_cascade_notional` here is the one that is not negotiable, and the two zone rules
    stand in for the confluences, one of which passes and one of which does not.
    """

    def settings(
        self, factory: SettingsFactory, *, needed: int, floor: float = 1_000.0
    ) -> Settings:
        return factory(
            **{
                "strategy.min_confluence": needed,
                # Required: the liquidation itself.
                "strategy.rules.min_cascade_notional.enabled": True,
                "strategy.rules.min_cascade_notional.required": True,
                "strategy.rules.min_cascade_notional.min_notional_usd": floor,
                # Optional and passing: a level 1.02 bps from the extreme, inside 15.
                "strategy.rules.zone_proximity.enabled": True,
                "strategy.rules.zone_proximity.required": False,
                "strategy.rules.zone_proximity.zone_width_bps": 15.0,
                # Optional and failing: no open interest on these rows at all.
                "strategy.rules.oi_flush.enabled": True,
                "strategy.rules.oi_flush.required": False,
            }
        )

    def run(self, settings: Settings) -> tuple[StrategyEngine, list[Intent]]:
        engine = StrategyEngine(settings, META)
        engine.on_tick(START_MS, {"BTC": falling_row(START_MS, 100.0)})
        intents: list[Intent] = []
        for step in range(1, 600):
            ts = START_MS + step * TICK_MS
            intents.extend(engine.on_tick(ts, {"BTC": quiet_row(ts, 98.0, support_px=97.99)}))
        return engine, intents

    def test_one_of_two_confluences_is_enough_when_one_is_asked_for(
        self, settings_factory: SettingsFactory
    ) -> None:
        engine, intents = self.run(self.settings(settings_factory, needed=1))
        (intent,) = intents
        assert intent.explain["confluence_passed"] == 1.0
        assert intent.explain["confluence_optional"] == 2.0
        assert intent.explain["zone_proximity.passed"] == 1.0
        assert intent.explain["oi_flush.passed"] == 0.0
        assert engine.stats.confluence_blocks == 0

    def test_two_are_not_enough_when_only_one_agrees(
        self, settings_factory: SettingsFactory
    ) -> None:
        engine, intents = self.run(self.settings(settings_factory, needed=2))
        assert intents == []
        (rejection,) = engine.take_rejections()
        assert rejection.reason == "confluence 1/2"
        assert engine.stats.confluence_blocks == 1
        # No single optional rule is blamed for a count nobody failed alone.
        assert engine.stats.rules["oi_flush"].killed == 0
        assert engine.stats.rules["zone_proximity"].killed == 0

    def test_a_required_rule_blocks_however_many_confluences_agree(
        self, settings_factory: SettingsFactory
    ) -> None:
        """The liquidation is the signal. Nothing votes it back in."""
        settings = self.settings(settings_factory, needed=0, floor=9_000_000.0)
        engine, intents = self.run(settings)
        assert intents == []
        (rejection,) = engine.take_rejections()
        assert rejection.reason == "min_cascade_notional"
        assert engine.stats.rules["min_cascade_notional"].killed == 1
        assert engine.stats.confluence_blocks == 0

    def test_zero_means_the_optional_rules_are_reported_and_never_block(
        self, settings_factory: SettingsFactory
    ) -> None:
        """How a rule earns its place before it is allowed to cost a trade."""
        engine, intents = self.run(self.settings(settings_factory, needed=0))
        (intent,) = intents
        assert intent.explain["confluence_needed"] == 0.0
        assert engine.stats.rules["oi_flush"].evaluated == 1
        assert engine.stats.rules["oi_flush"].passed == 0
        assert engine.stats.rules["oi_flush"].killed == 0

    def test_a_confluence_count_no_rule_set_could_reach_is_refused(
        self, settings_factory: SettingsFactory
    ) -> None:
        """Two votes needed and one optional rule enabled would block every intent."""
        with pytest.raises(ValueError, match="min_confluence"):
            settings_factory(
                **{
                    "strategy.min_confluence": 2,
                    "strategy.rules.zone_proximity.enabled": True,
                    "strategy.rules.zone_proximity.required": False,
                }
            )


class TestFilters:
    def test_a_wide_spread_blocks_the_trade(self, settings_factory: SettingsFactory) -> None:
        settings = settings_factory()
        cap = settings.filters.max_spread_bps.core
        engine = StrategyEngine(settings, META)
        engine.on_tick(START_MS, {"BTC": falling_row(START_MS, 100.0)})
        for step in range(1, 600):
            ts = START_MS + step * TICK_MS
            engine.on_tick(ts, {"BTC": quiet_row(ts, 98.0, spread_bps=cap + 1.0)})
        (rejection,) = engine.take_rejections()
        assert rejection.reason == "spread_too_wide"

    def test_a_null_spread_does_not_block(self, settings_factory: SettingsFactory) -> None:
        """The historical year has no book at all; the cost model handles the spread."""
        engine = StrategyEngine(settings_factory(), META)
        assert len(run_to_signal(engine)) == 1

    def test_a_too_new_coin_is_skipped(self, settings_factory: SettingsFactory) -> None:
        young = AssetMeta(coin="BTC", sz_decimals=5, max_leverage=40.0, listing_days=1.0)
        engine = StrategyEngine(settings_factory(), {"BTC": young})
        assert run_to_signal(engine) == []
        assert engine.take_rejections()[0].reason == "coin_too_new"

    def test_an_undateable_coin_counts_as_too_new(self, settings_factory: SettingsFactory) -> None:
        unknown = AssetMeta(coin="BTC", sz_decimals=5, max_leverage=40.0, listing_days=None)
        engine = StrategyEngine(settings_factory(), {"BTC": unknown})
        assert engine.take_rejections() == []
        assert run_to_signal(engine) == []
        assert engine.take_rejections()[0].reason == "coin_too_new"

    def test_an_excluded_coin_is_skipped(self, settings_factory: SettingsFactory) -> None:
        engine = StrategyEngine(settings_factory(**{"universe.exclude": ["BTC"]}), META)
        assert run_to_signal(engine) == []
        assert engine.take_rejections()[0].reason == "coin_excluded"

    def test_too_small_an_edge_is_skipped(self, settings_factory: SettingsFactory) -> None:
        """A 200 bps move with k_target 0.8 clears the bar; k_target 0.001 does not."""
        settings = settings_factory(**{"intent.k_target": 0.001})
        engine = StrategyEngine(settings, META)
        assert run_to_signal(engine) == []
        assert engine.take_rejections()[0].reason == "edge_too_small"

    def test_funding_gate_is_off_by_default_and_fires_when_set(
        self, settings_factory: SettingsFactory
    ) -> None:
        # Off: null threshold, so even absurd funding passes.
        engine = StrategyEngine(settings_factory(), META)
        engine.on_tick(START_MS, {"BTC": falling_row(START_MS, 100.0)})
        got = []
        for step in range(1, 600):
            ts = START_MS + step * TICK_MS
            got.extend(engine.on_tick(ts, {"BTC": quiet_row(ts, 98.0, funding=0.01)}))
        assert len(got) == 1

        # On: 0.01/hr is 800 bps per 8h against a long, well over a 30 bps threshold.
        gated = StrategyEngine(settings_factory(**{"filters.funding_extreme_bps_8h": 30.0}), META)
        gated.on_tick(START_MS, {"BTC": falling_row(START_MS, 100.0)})
        for step in range(1, 600):
            ts = START_MS + step * TICK_MS
            gated.on_tick(ts, {"BTC": quiet_row(ts, 98.0, funding=0.01)})
        assert gated.take_rejections()[0].reason == "funding_extreme"


class TestNoLookahead:
    def test_mutating_the_future_cannot_change_the_past(
        self, settings_factory: SettingsFactory
    ) -> None:
        """Feed the same rows up to T, then two different futures, and compare up to T.

        The engine's only defence against lookahead is structural: it sees one tick at a
        time and keeps no forward buffer. This is the test that would catch that changing.
        """
        cut_step = 300
        cut_ts = START_MS + cut_step * TICK_MS

        def replay(after_mid: float) -> list[Intent]:
            engine = StrategyEngine(settings_factory(), META)
            seen: list[Intent] = []
            engine.on_tick(START_MS, {"BTC": falling_row(START_MS, 100.0)})
            for step in range(1, 900):
                ts = START_MS + step * TICK_MS
                mid = 98.0 if ts <= cut_ts else after_mid
                seen.extend(engine.on_tick(ts, {"BTC": quiet_row(ts, mid)}))
            return [intent for intent in seen if intent.ts_ms <= cut_ts]

        assert replay(98.0) == replay(1.0), "a different future changed a past intent"
