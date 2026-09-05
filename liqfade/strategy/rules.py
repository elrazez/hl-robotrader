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

from liqfade.config.models import MinCascadeNotionalRule, Settings, ZoneProximityRule
from liqfade.strategy.types import CascadeState, FeatureRow

__all__ = [
    "RULES",
    "ConfluenceRule",
    "MinCascadeNotional",
    "RuleVerdict",
    "ZoneProximity",
    "build_enabled_rules",
]


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


# The registry. Keys must match `StrategyRules`' field names exactly; a test pins it.
RULES: dict[str, type] = {
    ZoneProximity.name: ZoneProximity,
    MinCascadeNotional.name: MinCascadeNotional,
}


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
