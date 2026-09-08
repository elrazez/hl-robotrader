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
    Tier,
    Urgency,
    fade_side,
)

__all__ = ["EngineStats", "Rejection", "RuleStats", "StrategyEngine", "rejection_of"]

BTC = "BTC"


def rejection_of(
    intent: Intent,
    *,
    ts_ms: int,
    reason: str,
    stage: str,
    explain: Mapping[str, float] | None = None,
) -> Rejection:
    """The refusal of one priced intent, whichever layer refused it.

    Here rather than in each caller because `sizing` and `risk` refuse intents this engine
    already built and their rows have to be the same shape as its own; a second spelling
    of the same six fields is how the scorer ends up with two kinds of refusal.
    """
    return Rejection(
        coin=intent.coin,
        side=intent.side,
        ts_ms=ts_ms,
        reason=reason,
        stage=stage,
        entry_px=intent.entry_px,
        target_px=intent.target_px,
        stop_px=intent.stop_px,
        time_stop_s=intent.time_stop_s,
        urgency=intent.urgency,
        explain=dict(intent.explain if explain is None else explain),
    )


@dataclass(frozen=True, slots=True)
class Rejection:
    """An intent the engine built and then threw away, and why.

    Args:
        coin: the symbol.
        side: the position it would have taken.
        ts_ms: the decision tick.
        reason: the filter name or the rule name that killed it.
        stage: `rule` or `filter` from this engine, and `sizing` or `risk` from the layers
            below it, so the report can separate the confluence layer's cost from the
            pre-trade guards' and from the gates that never see this module.
        entry_px: what the intent would have rested at.
        target_px: its partial-reversion exit.
        stop_px: its stop, which is also the risk denominator sizing would have used.
        time_stop_s: seconds after which it would have been flattened regardless.
        urgency: whether it would have taken liquidity.
        explain: the same mapping the intent would have carried, including the values of
            every rule that ran. A rejected trade is worth as much for research as a taken
            one, and it is worth nothing without its numbers.

    The four prices are not optional. Rules and filters both run after `_price`, so every
    rejection this engine records was a fully priced intent, and `liqfade refusals` scores
    the counterfactual off them: a reason alone says how often a gate fired and never what
    it cost.
    """

    coin: str
    side: Side
    ts_ms: int
    reason: str
    stage: str
    entry_px: float
    target_px: float
    stop_px: float
    time_stop_s: float
    urgency: Urgency
    explain: Mapping[str, float]


@dataclass
class RuleStats:
    """How one rule performed over a run. Feeds the report's per-rule tables.

    Args:
        evaluated: candidates the rule ran on. Zero means it was switched off.
        passed: how many of those it said yes to. Over `evaluated` this is the pass rate
            the report prints for the confluence rules, which is the number that says
            whether a rule is worth requiring.
        killed: intents this rule was blamed for. Only a required rule can be blamed;
            an optional one that fails costs a vote and nothing else.
        required: whether it blocks on its own. Copied from config so a rule that never
            ran still lands in the right table.
    """

    evaluated: int = 0
    passed: int = 0
    killed: int = 0
    required: bool = True


@dataclass
class EngineStats:
    """Counters the backtest report reads. Not used for any decision."""

    signals: int = 0
    intents: int = 0
    rules: dict[str, RuleStats] = field(default_factory=dict)
    filters: dict[str, int] = field(default_factory=dict)
    # Intents dropped because too few optional rules agreed, which is not any one rule's
    # doing and would be invisible in the per-rule table.
    confluence_blocks: int = 0
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
        self.stats = EngineStats(
            rules={
                name: RuleStats(required=getattr(settings.strategy.rules, name).required)
                for name in known_rules
            }
        )
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
        """This coin's detector, built on first sight and carrying its tier.

        The tier is what `cascade.liq_usd_min` is looked up by, the same way
        `filters.max_spread_bps` is. A coin with no metadata gets `AssetMeta`'s own default
        of `minor`; it cannot be traded anyway, because `_consider` drops a signal on a coin
        it cannot price.
        """
        detector = self._detectors.get(coin)
        if detector is None:
            meta = self.meta.get(coin)
            tier: Tier = meta.tier if meta is not None else "minor"
            detector = CascadeDetector(coin, self.settings, tier=tier)
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
            self._reject(intent, now_ms, killed_by, "rule", explain)
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
            self._reject(intent, now_ms, reason, "filter", explain)
            return None

        return replace(intent, explain=explain)

    def _run_rules(
        self, row: FeatureRow, state: CascadeState, explain: dict[str, float]
    ) -> str | None:
        """Run every enabled rule, merge its values into `explain`, and judge the set.

        Two kinds of rule, because the setup has two kinds of condition. A **required** rule
        is a precondition and blocks on its own: the liquidation being big enough is the
        owner's, and without it there is no trade to have. An **optional** rule is
        confluence, one voice among several, and the question is how many agree rather than
        whether all do. `strategy.min_confluence` is how many must; at 0 they are evaluated
        and reported and never block, which is how a new rule earns its place before it is
        allowed to cost a trade.

        Every rule runs even after one has failed, so `explain` carries the full picture of
        a rejected intent rather than stopping at the first objection, and every enabled
        rule's verdict reaches it as a 1 or a 0 alongside its values. A required failure is
        blamed on the first such rule in name order, which `build_enabled_rules` fixes so a
        rerun blames the same one.

        Returns the reason the intent was dropped, or None. A confluence failure has no one
        rule to name, so it reports as `confluence n/m`.
        """
        sections = self.settings.strategy.rules
        failed_required: str | None = None
        optional_total = 0
        optional_passed = 0
        for rule in self._rules:
            verdict = rule.evaluate(row, state)
            required: bool = getattr(sections, rule.name).required
            stats = self.stats.rules.setdefault(rule.name, RuleStats(required=required))
            stats.evaluated += 1
            for key, value in verdict.values.items():
                explain[f"{rule.name}.{key}"] = value
            explain[f"{rule.name}.passed"] = 1.0 if verdict.passed else 0.0
            if verdict.passed:
                stats.passed += 1
            if not required:
                optional_total += 1
                optional_passed += 1 if verdict.passed else 0
            elif not verdict.passed and failed_required is None:
                failed_required = rule.name

        needed = self.settings.strategy.min_confluence
        explain["confluence_passed"] = float(optional_passed)
        explain["confluence_optional"] = float(optional_total)
        explain["confluence_needed"] = float(needed)

        if failed_required is not None:
            self.stats.rules[failed_required].killed += 1
            return failed_required
        if optional_passed < needed:
            self.stats.confluence_blocks += 1
            return f"confluence {optional_passed}/{optional_total}"
        return None

    def _reject(
        self,
        intent: Intent,
        ts_ms: int,
        reason: str,
        stage: str,
        explain: Mapping[str, float],
    ) -> None:
        self._rejections.append(
            rejection_of(intent, ts_ms=ts_ms, reason=reason, stage=stage, explain=explain)
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
        #
        # `target_from` decides what that fraction is a fraction *of*. `extreme` is the
        # spec's formula, a fixed price on the way back from the low, and it assumes the
        # entry is near that low: true under `exhaustion.mode: fixed_delay`, false under the
        # rule mode and false again with `level_reclaim`, both of which wait for the bounce
        # to be visible before entering. From a late entry that fixed price is already
        # behind us. `entry` measures the same fraction of the distance that is left, so the
        # target is always ahead of the fill and the edge is `k_target` times the remaining
        # reversion. The stop is unaffected and stays beyond the extreme in both modes: it
        # belongs below the wick that swept the level, wherever we happened to get in. A
        # late entry therefore has a nearer target and a wider stop, and sizing follows the
        # stop, so the same risk fraction buys a smaller position.
        anchor_from = state.extreme_px if intent_cfg.target_from == "extreme" else entry_px
        target_px = anchor_from + intent_cfg.k_target * (state.anchor_px - anchor_from)
        # Beyond the extreme, scaled by volatility, never inside it. `direction` is -1 for
        # a long liquidation, which puts the stop below the low it is protecting.
        stop_px = state.extreme_px + state.direction * intent_cfg.k_stop * row.rv * row.mid
        if stop_px <= 0:
            return None

        # The cascade's peak z, not this tick's: see `CascadeState.peak_liq_z`. A null
        # threshold means the z-scale is not trusted to say anything here, so nothing is
        # urgent; see `intent.urgency_liq_z` in `config/models.py` for why that is an
        # option at all.
        peak_z = state.peak_liq_z
        threshold = intent_cfg.urgency_liq_z
        urgency: Urgency = (
            "high"
            if threshold is not None and peak_z is not None and peak_z >= threshold
            else "normal"
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
            "open_interest": row.open_interest,
            f"oi_delta_{row.window_s}s": row.oi_delta_w,
            f"oi_delta_{row.rv_window_s}s": row.oi_delta_rv,
            f"oi_delta_{row.oi_build_window_s}s": row.oi_delta_build,
            f"disp_{row.vwap_anchor_s}s_bps": row.disp_anchor_bps,
            "disp_day_bps": row.disp_day_bps,
        }
        values.update({key: value for key, value in optional.items() if value is not None})
        # Which entry rule fired, as a number because explain is a float mapping. The
        # backtest report turns it back into a word.
        values["entry_fixed_delay"] = 1.0 if signal.reason == "fixed_delay" else 0.0
        return values
