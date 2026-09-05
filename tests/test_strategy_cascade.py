"""The online cascade detector: start conditions, extreme tracking and both entry modes.

Every threshold in these tests comes from `config/default.yaml` through `settings_factory`,
never from a literal here, so a config change that ought to break a test does break it. The
feature values are hand-built rather than replayed, because the point is to hold three of
the start conditions and vary the fourth.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from strategy_rows import START_MS, TICK_MS, falling_row, quiet_row

from liqfade.config.models import Settings
from liqfade.strategy.cascade import CascadeDetector
from liqfade.strategy.types import FeatureRow

SettingsFactory = Callable[..., Settings]


class TestStart:
    def test_all_conditions_start_a_cascade(self, settings_factory: SettingsFactory) -> None:
        detector = CascadeDetector("BTC", settings_factory())
        detector.on_tick(falling_row(START_MS, 100.0))
        state = detector.active("long")
        assert state is not None
        assert state.side == "long"
        assert state.start_ts == START_MS
        assert state.anchor_px == 100.0

    def test_nothing_starts_on_a_quiet_tape(self, settings_factory: SettingsFactory) -> None:
        detector = CascadeDetector("BTC", settings_factory())
        detector.on_tick(quiet_row(START_MS, 100.0))
        assert detector.active("long") is None
        assert detector.active("short") is None

    @pytest.mark.parametrize(
        ("field", "value", "why"),
        [
            ("liq_z_long_w", 2.0, "z under cascade.liq_z_min"),
            ("liq_z_long_w", None, "z undefined, which is not the same as unusual"),
            ("ret_w", -0.001, "move is 0.5 rv, under ret_over_rv_min"),
            ("ret_w", 0.01, "price rose, which is not a long liquidation"),
            ("liq_imbalance_w", 0.3, "imbalance under imbalance_min"),
            ("liq_imbalance_w", -0.8, "imbalance favours the other side"),
            ("rv", None, "no volatility reference, so the move cannot be normalised"),
        ],
    )
    def test_one_missing_condition_blocks_the_start(
        self, settings_factory: SettingsFactory, field: str, value: float | None, why: str
    ) -> None:
        detector = CascadeDetector("BTC", settings_factory())
        detector.on_tick(falling_row(START_MS, 100.0, **{field: value}))
        assert detector.active("long") is None, why

    def test_short_liquidations_start_the_other_side(
        self, settings_factory: SettingsFactory
    ) -> None:
        """Shorts liquidated is forced buying, so price rises and the imbalance is negative."""
        detector = CascadeDetector("BTC", settings_factory())
        detector.on_tick(
            falling_row(
                START_MS,
                100.0,
                liq_z_long_w=0.0,
                liq_z_short_w=4.0,
                liq_imbalance_w=-0.8,
                ret_w=0.01,
                liq_short_usd_peak=1_000_000.0,
            )
        )
        assert detector.active("long") is None
        state = detector.active("short")
        assert state is not None and state.side == "short"


class TestTrigger:
    def test_proxy_trigger_ignores_the_z_score(self, settings_factory: SettingsFactory) -> None:
        """With trigger=proxy the exact z is not consulted, because live cannot see it."""
        settings = settings_factory(**{"cascade.trigger": "proxy"})
        detector = CascadeDetector("BTC", settings)
        detector.on_tick(falling_row(START_MS, 100.0, liq_z_long_w=None, proxy_long_w=5.0))
        assert detector.active("long") is not None

    def test_proxy_trigger_needs_the_proxy_over_its_threshold(
        self, settings_factory: SettingsFactory
    ) -> None:
        settings = settings_factory(**{"cascade.trigger": "proxy"})
        detector = CascadeDetector("BTC", settings)
        detector.on_tick(falling_row(START_MS, 100.0, liq_z_long_w=9.0, proxy_long_w=1.0))
        assert detector.active("long") is None

    def test_crossvenue_trigger_reads_the_z_score(self, settings_factory: SettingsFactory) -> None:
        """Same condition as exact; what differs is which venues filled the column."""
        settings = settings_factory(**{"cascade.trigger": "crossvenue"})
        detector = CascadeDetector("BTC", settings)
        detector.on_tick(falling_row(START_MS, 100.0))
        assert detector.active("long") is not None


class TestTracking:
    def test_extreme_follows_the_lowest_mid_of_a_falling_cascade(
        self, settings_factory: SettingsFactory
    ) -> None:
        detector = CascadeDetector("BTC", settings_factory())
        detector.on_tick(falling_row(START_MS, 100.0))
        for step, mid in enumerate((99.0, 97.0, 98.0), start=1):
            detector.on_tick(quiet_row(START_MS + step * TICK_MS, mid))
        state = detector.active("long")
        assert state is not None
        assert state.extreme_px == 97.0
        assert state.extreme_ts == START_MS + 2 * TICK_MS, "a bounce does not move the extreme"

    def test_peak_liq_is_the_largest_burst_since_the_start(
        self, settings_factory: SettingsFactory
    ) -> None:
        detector = CascadeDetector("BTC", settings_factory())
        detector.on_tick(falling_row(START_MS, 100.0, liq_long_usd_peak=1_000_000.0))
        detector.on_tick(quiet_row(START_MS + TICK_MS, 99.0, liq_long_usd_peak=3_000_000.0))
        detector.on_tick(quiet_row(START_MS + 2 * TICK_MS, 98.0, liq_long_usd_peak=500_000.0))
        state = detector.active("long")
        assert state is not None and state.peak_liq_15s == 3_000_000.0

    def test_total_liquidated_notional_accumulates_new_flow_only(
        self, settings_factory: SettingsFactory
    ) -> None:
        """The window column is a rolling sum, so adding it every tick would count it 240
        times a minute. The cascade total tracks the running maximum of the window instead,
        which is the most flow the window has ever held."""
        detector = CascadeDetector("BTC", settings_factory())
        detector.on_tick(falling_row(START_MS, 100.0, liq_long_usd_w=4_000_000.0))
        detector.on_tick(quiet_row(START_MS + TICK_MS, 99.0, liq_long_usd_w=4_000_000.0))
        detector.on_tick(quiet_row(START_MS + 2 * TICK_MS, 98.0, liq_long_usd_w=6_000_000.0))
        state = detector.active("long")
        assert state is not None and state.total_liq_usd == 6_000_000.0


class TestFixedDelayMode:
    """Q2's answer: enter a flat `exhaustion.fixed_delay_s` after the cascade starts."""

    def test_signals_once_at_the_delay_and_not_before(
        self, settings_factory: SettingsFactory
    ) -> None:
        settings = settings_factory(**{"exhaustion.mode": "fixed_delay"})
        delay_ms = int(settings.exhaustion.fixed_delay_s * 1000)
        detector = CascadeDetector("BTC", settings)
        assert detector.on_tick(falling_row(START_MS, 100.0)) is None

        signals = []
        for ts in range(START_MS + TICK_MS, START_MS + delay_ms + 5 * TICK_MS, TICK_MS):
            signal = detector.on_tick(quiet_row(ts, 98.0))
            if signal is not None:
                signals.append(signal)

        assert len(signals) == 1, "one intent per cascade, not one per tick after the delay"
        assert signals[0].state.start_ts == START_MS
        assert signals[0].reason == "fixed_delay"
        fired_at = signals[0].ts_ms
        assert fired_at - START_MS >= delay_ms
        assert fired_at - START_MS < delay_ms + TICK_MS, "fires on the first tick past it"

    def test_a_delay_past_max_age_never_signals(self, settings_factory: SettingsFactory) -> None:
        """The age window applies to both modes: an old cascade is a different regime."""
        settings = settings_factory(
            **{"exhaustion.mode": "fixed_delay", "exhaustion.fixed_delay_s": 1000.0}
        )
        detector = CascadeDetector("BTC", settings)
        detector.on_tick(falling_row(START_MS, 100.0))
        signals = [
            detector.on_tick(quiet_row(START_MS + step * 1000, 98.0)) for step in range(1, 1200)
        ]
        assert not any(signals)
        assert detector.active("long") is None, "dropped once past exhaustion.max_age_s"


class TestExhaustionRuleMode:
    """The spec's rule, kept as code behind `exhaustion.mode: rule` although Q2 said no."""

    def started(self, settings: Settings) -> CascadeDetector:
        """A detector with a long cascade running whose extreme is already set at 98.

        The extreme has to be established and then left alone, because `no_new_extreme_s`
        measures from the last new extreme: a row at a fresh low restarts that clock, and
        every exhaustion row after this one sits at 98 rather than below it.
        """
        detector = CascadeDetector("BTC", settings)
        detector.on_tick(falling_row(START_MS, 100.0))
        detector.on_tick(quiet_row(START_MS + TICK_MS, 98.0))
        return detector

    def exhausted_row(self, ts_ms: int, mid: float = 98.0, **over: float | None) -> FeatureRow:
        """Liquidations decayed, flow flipped up, mark back under its percentile."""
        values: dict[str, float | None] = {
            "liq_long_usd_peak": 100_000.0,  # 0.1 of the 1,000,000 peak, under liq_decay_frac
            "cvd_slope": 50_000.0,  # flipped against a falling cascade
            "mark_mid_bps": 2.0,
            "mark_mid_threshold": 10.0,
        }
        values.update(over)
        return quiet_row(ts_ms, mid, **values)

    def test_needs_consecutive_ticks_of_every_condition(
        self, settings_factory: SettingsFactory
    ) -> None:
        settings = settings_factory(**{"exhaustion.mode": "rule"})
        need = settings.exhaustion.consecutive_ticks
        detector = self.started(settings)

        # Past min_age and past no_new_extreme_s, with the extreme set once and left alone.
        base = START_MS + int(settings.exhaustion.min_age_s * 1000) + 1000
        signals = [
            self._tick(detector, self.exhausted_row(base + n * TICK_MS)) for n in range(need)
        ]
        assert signals[:-1] == [None] * (need - 1), "no signal before the streak completes"
        assert signals[-1] is not None
        assert signals[-1].reason == "exhaustion_rule"

    def test_a_broken_condition_resets_the_streak(self, settings_factory: SettingsFactory) -> None:
        settings = settings_factory(**{"exhaustion.mode": "rule"})
        need = settings.exhaustion.consecutive_ticks
        detector = self.started(settings)
        base = START_MS + int(settings.exhaustion.min_age_s * 1000) + 1000

        for n in range(need - 1):
            assert self._tick(detector, self.exhausted_row(base + n * TICK_MS)) is None
        # Liquidations pick up again: the cascade was not exhausted after all.
        broken = self.exhausted_row(base + (need - 1) * TICK_MS, liq_long_usd_peak=900_000.0)
        assert self._tick(detector, broken) is None
        state = detector.active("long")
        assert state is not None and state.exhaustion_streak == 0

    def test_a_new_extreme_blocks_exhaustion(self, settings_factory: SettingsFactory) -> None:
        """`no_new_extreme_s` is the condition that says the cascade is still running."""
        settings = settings_factory(**{"exhaustion.mode": "rule"})
        detector = self.started(settings)
        base = START_MS + int(settings.exhaustion.min_age_s * 1000) + 1000
        signals = [
            self._tick(detector, self.exhausted_row(base + n * TICK_MS, 98.0 - n * 0.1))
            for n in range(settings.exhaustion.consecutive_ticks + 3)
        ]
        assert not any(signals), "price kept making new lows, so nothing was exhausted"

    def test_nothing_fires_before_min_age(self, settings_factory: SettingsFactory) -> None:
        settings = settings_factory(**{"exhaustion.mode": "rule"})
        detector = self.started(settings)
        signals = [
            self._tick(detector, self.exhausted_row(START_MS + n * TICK_MS))
            for n in range(2, int(settings.exhaustion.min_age_s * 1000 / TICK_MS))
        ]
        assert not any(signals)

    @staticmethod
    def _tick(detector: CascadeDetector, row: FeatureRow) -> object:
        return detector.on_tick(row)
