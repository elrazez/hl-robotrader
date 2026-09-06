"""The level join: which support and resistance price a grid row is handed.

The rules that read these are existential questions about one price ("did the cascade
trade through a level and reclaim it"), and they are asked of a single number, so which
level ends up in that number is the whole answer. Picking the most recently *formed* level
rather than the nearest one to price is a rule that fires almost at random, which is why
this is tested directly rather than through a run.
"""

from __future__ import annotations

from collections.abc import Callable

import polars as pl
import pytest

from liqfade.backtest.features import _with_levels
from liqfade.config.models import Settings

SettingsFactory = Callable[..., Settings]

DAY = 1_772_323_200_000  # 2026-03-01T00:00:00Z, a UTC midnight
HOUR = 3_600_000


def grid(mids: list[float], *, first_ms: int = DAY) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "coin": ["BTC"] * len(mids),
            "ts_ms": [first_ms + i * 1000 for i in range(len(mids))],
            "mid": mids,
        }
    )


def levels(*rows: tuple[float, str, int]) -> pl.DataFrame:
    """`(px, kind, known_from)` triples as a level frame."""
    return pl.DataFrame(
        {
            "coin": ["BTC"] * len(rows),
            "ts_ms": [known for _px, _kind, known in rows],
            "px": [px for px, _kind, _known in rows],
            "kind": [kind for _px, kind, _known in rows],
            "known_from": [known for _px, _kind, known in rows],
            "source": ["swing"] * len(rows),
        }
    )


def enabled(factory: SettingsFactory) -> Settings:
    return factory(**{"strategy.rules.zone_proximity.enabled": True})


def test_support_is_the_nearest_level_below_mid_not_the_newest_one(
    settings_factory: SettingsFactory,
) -> None:
    """96 was drawn yesterday and 99 the day before; 99 is the one under this price.

    An as-of join on `known_from` would answer 96 here, and a sweep-and-reclaim rule asked
    about 96 while price is at 100 is asking about the wrong level entirely.
    """
    joined = _with_levels(
        grid([100.0]),
        levels((96.0, "support", DAY - HOUR), (99.0, "support", DAY - 48 * HOUR)),
        enabled(settings_factory),
    )
    assert joined["support_px"][0] == 99.0


def test_resistance_is_the_nearest_level_above_mid(settings_factory: SettingsFactory) -> None:
    joined = _with_levels(
        grid([100.0]),
        levels((101.0, "resistance", DAY - HOUR), (104.0, "resistance", DAY - HOUR)),
        enabled(settings_factory),
    )
    assert joined["resistance_px"][0] == 101.0


def test_a_level_further_than_max_dist_is_absent_rather_than_far_away(
    settings_factory: SettingsFactory,
) -> None:
    """`features.levels.max_dist_bps` is 200, so 2.0 either side of a mid of 100."""
    joined = _with_levels(
        grid([100.0]), levels((97.0, "support", DAY - HOUR)), enabled(settings_factory)
    )
    assert joined["support_px"][0] is None


def test_a_level_not_knowable_at_the_days_start_is_not_used(
    settings_factory: SettingsFactory,
) -> None:
    """A fractal confirmed at 01:00 is a level from tomorrow, never from 02:00 today.

    Deliberately conservative rather than lookahead: the restriction is what buys one
    as-of join per day instead of a lookup over both price and time.
    """
    late = levels((99.0, "support", DAY + HOUR))
    joined = _with_levels(grid([100.0], first_ms=DAY + 2 * HOUR), late, enabled(settings_factory))
    assert joined["support_px"][0] is None

    tomorrow = _with_levels(
        grid([100.0], first_ms=DAY + 25 * HOUR), late, enabled(settings_factory)
    )
    assert tomorrow["support_px"][0] == 99.0


def test_each_day_of_a_multi_day_grid_uses_its_own_level_set(
    settings_factory: SettingsFactory,
) -> None:
    """The streaming engine builds a day with the previous one in front of it."""
    both = pl.concat([grid([100.0], first_ms=DAY), grid([100.0], first_ms=DAY + 24 * HOUR)])
    joined = _with_levels(both, levels((99.0, "support", DAY + HOUR)), enabled(settings_factory))
    assert joined.sort("ts_ms")["support_px"].to_list() == [None, 99.0]


def test_a_row_with_no_mid_gets_no_levels(settings_factory: SettingsFactory) -> None:
    """`join_asof` on price has no key to join on, and absence is the honest answer."""
    frame = grid([100.0, 100.0]).with_columns(
        pl.when(pl.col("ts_ms") == DAY).then(None).otherwise(pl.col("mid")).alias("mid")
    )
    joined = _with_levels(frame, levels((99.0, "support", DAY - HOUR)), enabled(settings_factory))
    assert joined.sort("ts_ms")["support_px"].to_list() == [None, 99.0]


def test_nothing_is_joined_when_no_level_rule_is_enabled(
    settings_factory: SettingsFactory,
) -> None:
    """The shipped default, and the reason a normal run never reads a candle."""
    joined = _with_levels(grid([100.0]), levels((99.0, "support", DAY - HOUR)), settings_factory())
    assert joined["support_px"][0] is None
    assert joined["resistance_px"][0] is None


@pytest.mark.parametrize("frame", [None, pl.DataFrame()])
def test_no_levels_at_all_still_produces_the_columns(
    settings_factory: SettingsFactory, frame: pl.DataFrame | None
) -> None:
    """Every downstream reader expects the columns to exist, empty or not."""
    joined = _with_levels(grid([100.0]), frame, enabled(settings_factory))
    assert joined["support_px"][0] is None
    assert joined["resistance_px"][0] is None
