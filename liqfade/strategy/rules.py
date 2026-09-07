"""The confluence rule layer: extra entry conditions, added without touching the engine.

This exists so that trying a new entry condition is one class, one config section, one
model field and one test, rather than an edit to `engine.py` every time. The engine runs
every **enabled** rule after cascade and exhaustion have fired; all of them must pass or
the intent is dropped and the failing rule names itself as the reason. Every rule's values
reach `explain()` whether it passed or failed, so a rejected trade can still be argued
about afterwards.

With every rule disabled the engine behaves exactly as the bare spec does. That is not a
happy accident, it is the property `tests/test_strategy_engine.py` pins, because it is what
makes the golden baseline a baseline: a rule that changed behaviour while switched off
would make every historical number incomparable.

Rules are pure. They read a `FeatureRow` and a `CascadeState` and return a verdict. They
hold no state between ticks, do no I/O, and never see the clock, so the same inputs give
the same verdict in the backtest and in Phase 5's live loop.

Adding one:

1. write the class here, with a `name` and an `evaluate`, and register it in `RULES`
2. add its config model in `liqfade/config/models.py` under `StrategyRules`
3. add its section to `config/default.yaml`, `enabled: false`
4. write its test

`tests/test_strategy_rules.py` asserts `RULES` and `StrategyRules` name the same set, so
skipping step 2 or 3 fails the suite rather than shipping a rule nothing can turn on. And
because `StrategyRules` forbids extra keys, a typo in a config file's rule name is a loud
validation error rather than a rule that silently never ran.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from liqfade.config.models import (
    LevelReclaimRule,
    MinCascadeNotionalRule,
    OiFlushRule,
    Settings,
    VwapStretchRule,
    ZoneProximityRule,
)
from liqfade.strategy.types import CascadeState, FeatureRow

__all__ = [
    "LEVEL_RULES",
    "RULES",
    "ConfluenceRule",
    "LevelReclaim",
    "MinCascadeNotional",
    "OiFlush",
    "RuleVerdict",
    "VwapStretch",
    "ZoneProximity",
    "build_enabled_rules",
    "level_rules_enabled",
]

# The rules that read `FeatureRow.support_px` and `resistance_px`. Joining a level set onto
# every grid row is the most expensive thing the feature build does, so the backtest only
# does it when one of these is switched on. A rule added here and not to `RULES` is a name
# that turns on an expensive join for nothing; a level-reading rule missing from here reads
# nulls and fails on every candidate, which is the more expensive mistake because it looks
# like a result. `tests/test_strategy_rules.py` pins the membership.
LEVEL_RULES: tuple[str, ...] = ("zone_proximity", "level_reclaim")


@dataclass(frozen=True, slots=True)
class RuleVerdict:
    """One rule's answer.

    Args:
        passed: whether the intent may proceed as far as this rule is concerned.
        values: the numbers behind the answer, merged into `Intent.explain` with the
            rule's name as a prefix. A rule that returns no values is a rule whose
            decision cannot be checked later, so every rule here returns some.
    """

    passed: bool
    values: Mapping[str, float]


@runtime_checkable
class ConfluenceRule(Protocol):
    """What the engine needs from a rule. Construct with the rule's own config section."""

    name: str

    def evaluate(self, features: FeatureRow, cascade: CascadeState) -> RuleVerdict:
        """Judge one candidate entry. Pure: no state, no I/O, no clock."""
        ...


class ZoneProximity:
    """Passes when the cascade extreme sits within `zone_width_bps` of a prior level.

    **Disabled by default and expected to stay that way.** Q7 measured in-zone episodes
    reverting *worse* than out-of-zone, +36.1 bps against +146.3 at a 15 bps zone over
    1,523 in-zone episodes, which is the opposite of the effect the rule assumes. It ships
    because the mechanism it demonstrates is worth having and because deleting a tested
    negative result means someone re-derives it later.

    Distance is measured from `cascade.extreme_px`, not from the current mid: the question
    is whether the cascade ran into a level, and by the time the entry fires price has
    usually come off the extreme by more than the zone is wide.

    No level within `features.levels.max_dist_bps` means the rule fails rather than passes.
    "No level nearby" is the definition of not being in a zone.
    """

    name = "zone_proximity"

    def __init__(self, config: ZoneProximityRule) -> None:
        self.config = config

    def evaluate(self, features: FeatureRow, cascade: CascadeState) -> RuleVerdict:
        extreme = cascade.extreme_px
        if extreme <= 0:
            return RuleVerdict(False, {"extreme_px": extreme})
        distances = [
            1e4 * abs(extreme - level) / extreme
            for level in (features.support_px, features.resistance_px)
            if level is not None
        ]
        values: dict[str, float] = {"extreme_px": extreme}
        if features.support_px is not None:
            values["support_px"] = features.support_px
        if features.resistance_px is not None:
            values["resistance_px"] = features.resistance_px
        if not distances:
            return RuleVerdict(False, values)
        nearest = min(distances)
        values["nearest_level_bps"] = nearest
        return RuleVerdict(nearest <= self.config.zone_width_bps, values)


class MinCascadeNotional:
    """Passes when the cascade has liquidated more than `min_notional_usd`.

    Trivial on purpose. It is the rule that proves the mechanism with something anyone can
    check by hand, so a failure in the rule layer is distinguishable from a failure in a
    rule's own logic.
    """

    name = "min_cascade_notional"

    def __init__(self, config: MinCascadeNotionalRule) -> None:
        self.config = config

    def evaluate(self, features: FeatureRow, cascade: CascadeState) -> RuleVerdict:
        total = cascade.total_liq_usd
        return RuleVerdict(
            total >= self.config.min_notional_usd,
            {"cascade_notional_usd": total, "floor_usd": self.config.min_notional_usd},
        )


class OiFlush:
    """Passes when open interest was flushed, and optionally when it had built up first.

    Two questions, one rule, because they are two halves of the same observation a trader
    makes by eye: positions were crowded, and then they were forced out. `oi_delta_rv` over
    the rv window is the flush, and it must be at least `drop_min_frac` **down**, so the
    comparison is against a negative number. `oi_delta_build` over the longest configured OI
    window is the crowding, and it is measured to now rather than to the start of the
    cascade, so it is already net of the flush: requiring it to be positive is requiring
    open interest to still be above where it was an hour ago even after the liquidations.

    A null delta fails. It means the window is not full yet or the coin has no context
    samples, and "we cannot see the open interest" is not evidence that it was flushed.
    """

    name = "oi_flush"

    def __init__(self, config: OiFlushRule) -> None:
        self.config = config

    def evaluate(self, features: FeatureRow, cascade: CascadeState) -> RuleVerdict:
        drop, build = features.oi_delta_rv, features.oi_delta_build
        values: dict[str, float] = {"drop_min_frac": self.config.drop_min_frac}
        for key, value in (
            ("oi_delta_rv", drop),
            ("oi_delta_build", build),
            ("open_interest", features.open_interest),
        ):
            if value is not None:
                values[key] = value
        if drop is None or drop > -self.config.drop_min_frac:
            return RuleVerdict(False, values)
        floor = self.config.build_min_frac
        if floor is not None:
            values["build_min_frac"] = floor
            if build is None or build < floor:
                return RuleVerdict(False, values)
        return RuleVerdict(True, values)


class VwapStretch:
    """Passes when mid is at least `min_disp_bps` past the VWAP, the way the cascade pushed.

    Fading a long liquidation means price fell, so the displacement must be **negative**:
    we buy when price is stretched below the reference everyone else paid. Fading a short
    liquidation mirrors it. There is deliberately no mode for the opposite sign, which
    would be a momentum rule and is out of scope for v0.

    `reference: day` is the session VWAP accumulated from 00:00 UTC, which is what a
    discretionary trader watches; `anchor` is the fixed pre-cascade window
    (`features.vwap_anchor_s`), which cannot be dragged along by the cascade but is also
    blind to where the day has actually traded. A null reference fails: no VWAP is not the
    same as no stretch, and early in a UTC day the session VWAP does not exist yet.
    """

    name = "vwap_stretch"

    def __init__(self, config: VwapStretchRule) -> None:
        self.config = config

    def evaluate(self, features: FeatureRow, cascade: CascadeState) -> RuleVerdict:
        disp = features.disp_day_bps if self.config.reference == "day" else features.disp_anchor_bps
        values: dict[str, float] = {"min_disp_bps": self.config.min_disp_bps}
        if disp is None:
            return RuleVerdict(False, values)
        values["disp_bps"] = disp
        # `cascade.side` is the liquidated side: `long` means longs were closed, so price
        # fell and the fade is a buy into a negative displacement.
        passed = (
            disp <= -self.config.min_disp_bps
            if cascade.side == "long"
            else disp >= self.config.min_disp_bps
        )
        return RuleVerdict(passed, values)


class LevelReclaim:
    """The swing failure and the three tap: through a level, and back on the right side.

    For a long liquidation the level is `support_px`, the nearest one below the current
    mid. The cascade extreme must be at least `sweep_min_bps` below it, which is the
    "traded through a significant swing low" half, and mid must now be at least
    `reclaim_min_bps` above it, which is the "failed to close below" half. A short
    liquidation mirrors it against `resistance_px`.

    The two conditions together require the level to sit **between the extreme and the
    current mid**, and the nearest level below mid is the highest that can be in that band.
    It is therefore the *shallowest* sweep available, not the deepest: the rule measures the
    extreme against the level closest to price and never against a deeper one. Where a
    shallow level sits between mid and a level the cascade genuinely tore through, the shallow
    one is what gets judged, and the deeper level is invisible to the rule. At v1's
    `sweep_min_bps` of 5 that matters, because a 5 bps sweep of the near level passes the
    condition on a cascade whose real sweep was much deeper, and a shallow level too close to
    mid to have been swept by 5 bps fails it while a deeper qualifying level goes unasked.
    See `_with_levels` in `backtest/features.py` for how the level is chosen and for the
    deliberate restriction to levels knowable at the start of the day.

    A null level fails. `support_px` is null when no level of that kind is within
    `features.levels.max_dist_bps`, and no level nearby means nothing was swept.
    """

    name = "level_reclaim"

    def __init__(self, config: LevelReclaimRule) -> None:
        self.config = config

    def evaluate(self, features: FeatureRow, cascade: CascadeState) -> RuleVerdict:
        below = cascade.side == "long"
        level = features.support_px if below else features.resistance_px
        mid, extreme = features.mid, cascade.extreme_px
        values: dict[str, float] = {
            "sweep_min_bps": self.config.sweep_min_bps,
            "reclaim_min_bps": self.config.reclaim_min_bps,
            "extreme_px": extreme,
        }
        if level is not None:
            values["level_px"] = level
        if mid is not None:
            values["mid"] = mid
        if level is None or level <= 0 or mid is None or mid <= 0 or extreme <= 0:
            return RuleVerdict(False, values)

        # Both positive exactly when the geometry holds: the extreme beyond the level, mid
        # back on the near side of it.
        sign = 1.0 if below else -1.0
        sweep_bps = 1e4 * sign * (level - extreme) / level
        reclaim_bps = 1e4 * sign * (mid - level) / level
        values["sweep_bps"] = sweep_bps
        values["reclaim_bps"] = reclaim_bps
        passed = (
            sweep_bps >= self.config.sweep_min_bps and reclaim_bps >= self.config.reclaim_min_bps
        )
        return RuleVerdict(passed, values)


# The registry. Keys must match `StrategyRules`' field names exactly; a test pins it.
RULES: dict[str, type] = {
    ZoneProximity.name: ZoneProximity,
    MinCascadeNotional.name: MinCascadeNotional,
    OiFlush.name: OiFlush,
    VwapStretch.name: VwapStretch,
    LevelReclaim.name: LevelReclaim,
}


def level_rules_enabled(settings: Settings) -> bool:
    """Whether any enabled rule needs `support_px` and `resistance_px` built."""
    sections = settings.strategy.rules
    return any(getattr(sections, name).enabled for name in LEVEL_RULES)


def build_enabled_rules(settings: Settings) -> list[ConfluenceRule]:
    """Instantiate every rule whose config section has `enabled: true`.

    Sorted by name so the order rules run in, and therefore which one gets the credit for
    killing an intent when two would have, does not depend on dict insertion order. A
    backtest that reported a different blocking rule on a rerun would be unreadable.
    """
    sections = settings.strategy.rules
    enabled: list[ConfluenceRule] = []
    for name in sorted(RULES):
        config = getattr(sections, name)
        if config.enabled:
            enabled.append(RULES[name](config))
    return enabled
