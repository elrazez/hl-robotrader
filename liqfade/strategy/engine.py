"""The strategy: features in, intents out. No I/O, no wall clock, no randomness.

`StrategyEngine.on_tick(now_ms, features_by_coin)` is the whole surface. It owns one
`CascadeDetector` per coin, turns a signal into an `Intent`, runs the enabled confluence
rules and then the pre-trade filters, and returns whatever survived.

Determinism is the contract. Given the same rows in the same order it returns the same
intents, byte for byte, which is what lets the backtest and Phase 5's live loop share this
module with no branch between them. Nothing here may read the clock: `now_ms` arrives as an
argument precisely so that a replay and a live tick are indistinguishable from inside.

Rejections are drained rather than accumulated. `take_rejections()` hands back what this
tick threw away and forgets it, so the backtest can log every near miss into its report
while a live loop that runs for weeks does not grow a list forever.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from liqfade.config.models import Settings
from liqfade.strategy.cascade import CascadeDetector, CascadeSignal
from liqfade.strategy.filters import check_filters
from liqfade.strategy.rules import ConfluenceRule, build_enabled_rules
from liqfade.strategy.types import (
    AssetMeta,
    CascadeState,
    FeatureRow,
    Intent,
    LiqSide,
    Side,
    Urgency,
    fade_side,
)

__all__ = ["EngineStats", "Rejection", "RuleStats", "StrategyEngine"]

BTC = "BTC"


@dataclass(frozen=True, slots=True)
class Rejection:
    """An intent the engine built and then threw away, and why.

    Args:
        coin: the symbol.
        side: the position it would have taken.
        ts_ms: the decision tick.
        reason: the filter name or the rule name that killed it.
        stage: `rule` or `filter`, so the report can separate the confluence layer's cost
            from the pre-trade guards'.
        explain: the same mapping the intent would have carried, including the values of
            every rule that ran. A rejected trade is worth as much for research as a taken
            one, and it is worth nothing without its numbers.
    """

    coin: str
    side: Side
    ts_ms: int
    reason: str
    stage: str
    explain: Mapping[str, float]


@dataclass
class RuleStats:
    """How one rule performed over a run. Feeds the report's per-rule table."""

    evaluated: int = 0
    passed: int = 0
    killed: int = 0


@dataclass
class EngineStats:
    """Counters the backtest report reads. Not used for any decision."""

    signals: int = 0
    intents: int = 0
    rules: dict[str, RuleStats] = field(default_factory=dict)
    filters: dict[str, int] = field(default_factory=dict)
    unpriceable: int = 0


class StrategyEngine:
    """One engine per run. Holds a detector per coin and nothing else that persists."""

    def __init__(self, settings: Settings, meta: Mapping[str, AssetMeta]) -> None:
        """
        Args:
            settings: resolved config. Read on every tick rather than snapshotted, so a
                Phase 5 `reload_config` at a safe point takes effect without a restart.
            meta: per-coin venue and universe metadata. A coin absent from here is not
                traded: sizing cannot round its size and the filters cannot date it.
        """
        self.settings = settings
        self.meta = dict(meta)
        known_rules = type(settings.strategy.rules).model_fields
        self.stats = EngineStats(rules={name: RuleStats() for name in known_rules})
        self._detectors: dict[str, CascadeDetector] = {}
        self._rules: list[ConfluenceRule] = build_enabled_rules(settings)
        self._rejections: list[Rejection] = []

    def take_rejections(self) -> list[Rejection]:
        """Hand back the rejections since the last call and forget them."""
        drained, self._rejections = self._rejections, []
        return drained

    def on_tick(self, now_ms: int, features_by_coin: Mapping[str, FeatureRow]) -> list[Intent]:
        """Advance every coin's detector one tick and return the intents that survive.

        Args:
            now_ms: the decision timestamp. Every row must be stamped with it.
            features_by_coin: one row per coin under consideration this tick.

        Returns:
            Intents in coin order, so a run is reproducible regardless of how the caller
            built the mapping.
        """
        signals: list[tuple[str, CascadeSignal]] = []
        for coin in sorted(features_by_coin):
            row = features_by_coin[coin]
            signal = self._detector(coin).on_tick(row)
            if signal is not None:
                signals.append((coin, signal))
        self.stats.signals += len(signals)

        btc_side = self._btc_cascade_side()
        intents = []
        for coin, signal in signals:
            intent = self._consider(now_ms, signal, features_by_coin[coin], btc_side)
            if intent is not None:
                intents.append(intent)
        self.stats.intents += len(intents)
        return intents

    # --- internals ------------------------------------------------------------

    def _detector(self, coin: str) -> CascadeDetector:
        detector = self._detectors.get(coin)
        if detector is None:
            detector = CascadeDetector(coin, self.settings)
            self._detectors[coin] = detector
        return detector

    def _btc_cascade_side(self) -> LiqSide | None:
        """Which side BTC is cascading on, for the market-wide filter.

        Read after every detector has ticked, so it reflects this tick rather than the
        last one. Only the engine can answer this: a filter sees one coin.
        """
        detector = self._detectors.get(BTC)
        if detector is None:
            return None
        sides: tuple[LiqSide, ...] = ("long", "short")
        for side in sides:
            if detector.active(side) is not None:
                return side
        return None

    def _consider(
        self,
        now_ms: int,
        signal: CascadeSignal,
        row: FeatureRow,
        btc_side: LiqSide | None,
    ) -> Intent | None:
        """Turn one signal into an intent, or record why it did not become one."""
        state = signal.state
        meta = self.meta.get(state.coin)
        if meta is None:
            self.stats.unpriceable += 1
            return None

        priced = self._price(now_ms, signal, row)
        if priced is None:
            # No mid or no volatility reference, so the stop has no width and the target
            # has no anchor. There is no safe default for either; a stop guessed from
            # nothing is worse than no trade.
            self.stats.unpriceable += 1
            return None
        intent = priced

        explain = dict(intent.explain)
        killed_by = self._run_rules(row, state, explain)
        side = intent.side
        if killed_by is not None:
            self._reject(state.coin, side, now_ms, killed_by, "rule", explain)
            return None

        reason = check_filters(
            row,
            side=side,
            edge_frac=intent.edge_frac,
            meta=meta,
            settings=self.settings,
            btc_cascade_side=btc_side,
        )
        if reason is not None:
            self.stats.filters[reason] = self.stats.filters.get(reason, 0) + 1
            self._reject(state.coin, side, now_ms, reason, "filter", explain)
            return None

        return replace(intent, explain=explain)

    def _run_rules(
        self, row: FeatureRow, state: CascadeState, explain: dict[str, float]
    ) -> str | None:
        """Run every enabled rule, merge its values into `explain`, return the first fail.

        Every rule runs even after one has failed, so `explain` carries the full picture of
        a rejected intent rather than stopping at the first objection. The name returned is
        the first failure in name order, which `build_enabled_rules` fixes so a rerun
        blames the same rule.
        """
        failed: str | None = None
        for rule in self._rules:
            verdict = rule.evaluate(row, state)
            stats = self.stats.rules.setdefault(rule.name, RuleStats())
            stats.evaluated += 1
            for key, value in verdict.values.items():
                explain[f"{rule.name}.{key}"] = value
            if verdict.passed:
                stats.passed += 1
            else:
                stats.killed += 1
                if failed is None:
                    failed = rule.name
        return failed

    def _reject(
        self,
        coin: str,
        side: Side,
        ts_ms: int,
        reason: str,
        stage: str,
        explain: Mapping[str, float],
    ) -> None:
        self._rejections.append(
            Rejection(
                coin=coin, side=side, ts_ms=ts_ms, reason=reason, stage=stage, explain=dict(explain)
            )
        )

    def _price(self, now_ms: int, signal: CascadeSignal, row: FeatureRow) -> Intent | None:
        """Build the entry, target and stop from spec section 3. None when unpriceable."""
        state = signal.state
        if row.mid is None or row.mid <= 0 or row.rv is None or row.rv <= 0:
            return None

        side = fade_side(state.side)
        entry_px = self._entry_price(row, side)
        if entry_px <= 0:
            return None

        intent_cfg = self.settings.intent
        # Partial reversion back towards the pre-cascade anchor. k_target 0.8 is Q8's
        # winning cell over 2,722 episodes, taken because every neighbour was positive.
        target_px = state.extreme_px + intent_cfg.k_target * (state.anchor_px - state.extreme_px)
        # Beyond the extreme, scaled by volatility, never inside it. `direction` is -1 for
        # a long liquidation, which puts the stop below the low it is protecting.
        stop_px = state.extreme_px + state.direction * intent_cfg.k_stop * row.rv * row.mid
        if stop_px <= 0:
            return None

        # The cascade's peak z, not this tick's: see `CascadeState.peak_liq_z`.
        peak_z = state.peak_liq_z
        urgency: Urgency = (
            "high" if peak_z is not None and peak_z >= intent_cfg.urgency_liq_z else "normal"
        )

        return Intent(
            coin=state.coin,
            side=side,
            entry_px=entry_px,
            target_px=target_px,
            stop_px=stop_px,
            time_stop_s=intent_cfg.time_stop_s,
            urgency=urgency,
            ts_ms=now_ms,
            explain=self._explain(signal, row),
        )

    def _entry_price(self, row: FeatureRow, side: Side) -> float:
        """The touch we would rest at: best bid to go long, best ask to go short.

        The historical year records no book, so `spread_bps` is null and this falls back to
        mid. That is not the entry pretending to be free: the backtest's cost model
        subtracts an estimated effective spread built from Hyperliquid's own impact prices,
        which is the honest treatment of a quote nobody wrote down. Where a real spread
        exists, live or on a recorded day, the touch is derived from it and the same code
        path serves both.
        """
        assert row.mid is not None  # guarded by _price
        if row.spread_bps is None:
            return row.mid
        half = row.spread_bps / 2e4
        return row.mid * (1.0 - half) if side == "long" else row.mid * (1.0 + half)

    def _explain(self, signal: CascadeSignal, row: FeatureRow) -> dict[str, float]:
        """Every feature value that crossed a threshold, keyed by its real window.

        The keys spell the window out (`liq_z_60s`, not `liq_z_w`) because this mapping is
        what Telegram posts and what the trade log keeps, and a reader a month later needs
        to know which 60 seconds were meant.
        """
        state = signal.state
        values: dict[str, float] = {
            "cascade_start_ts": float(state.start_ts),
            "cascade_age_s": state.age_s(signal.ts_ms),
            "anchor_px": state.anchor_px,
            "extreme_px": state.extreme_px,
            "move_bps": state.move_bps(),
            f"peak_liq_{row.peak_window_s}s": state.peak_liq_15s,
            "cascade_notional_usd": state.total_liq_usd,
            f"liq_imbalance_{row.window_s}s": row.liq_imbalance_w,
            f"liq_usd_{row.window_s}s": row.liq_usd_w(state.side),
        }
        optional = {
            # The peak the cascade reached, which is what urgency was decided on. The
            # value at this tick is `_now`, and under a fixed delay entry it is usually
            # zero: reporting only that would make every alert look like a small cascade.
            f"liq_z_{row.window_s}s": state.peak_liq_z,
            f"liq_z_{row.window_s}s_now": row.liq_z(state.side),
            f"proxy_{row.window_s}s": row.proxy(state.side),
            f"ret_{row.window_s}s": row.ret_w,
            f"rv_{row.rv_window_s}s": row.rv,
            f"cvd_slope_{row.peak_window_s}s": row.cvd_slope,
            "spread_bps": row.spread_bps,
            "book_imbalance": row.book_imbalance,
            "funding": row.funding,
            "mark_mid_bps": row.mark_mid_bps,
        }
        values.update({key: value for key, value in optional.items() if value is not None})
        # Which entry rule fired, as a number because explain is a float mapping. The
        # backtest report turns it back into a word.
        values["entry_fixed_delay"] = 1.0 if signal.reason == "fixed_delay" else 0.0
        return values
