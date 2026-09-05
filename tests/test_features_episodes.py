"""The episode detector, on synthetic liquidation streams with known answers.

Every episode here is built by hand so the expected numbers come from arithmetic done on
paper rather than from the code, which is the only way a test can disagree with the
implementation. The detector is the ground truth Phase 4's online cascade detector is
later measured against, so a silent change in what counts as an episode changes every
research number downstream.
"""

from __future__ import annotations

import polars as pl

from liqfade.data.schemas import SOURCE_HL_S3, rows_to_table
from liqfade.features.episodes import group_episodes, qualify_episodes

DAY = 1_772_323_200_000  # 2026-03-01T00:00:00Z, a round millisecond to count from


def liqs(*events: tuple[int, str, float, float]) -> pl.DataFrame:
    """(offset_s, side, px, notional_usd) tuples as canonical HL liquidation rows."""
    rows = [
        {
            "venue": "hyperliquid",
            "coin": "BTC",
            "venue_symbol": "BTC",
            "ts_ms": DAY + offset_s * 1000,
            "rx_ms": 0,
            "side": side,
            "px": px,
            "sz": notional / px,
            "notional_usd": notional,
            "method": "market",
            "complete": True,
        }
        for offset_s, side, px, notional in events
    ]
    return pl.from_arrow(rows_to_table("liquidations", rows, SOURCE_HL_S3))


def bars(mids: dict[int, float], *, notional: float = 0.0) -> pl.DataFrame:
    """A 1s grid: {offset_s: mid}, carrying traded notional per second."""
    return pl.DataFrame(
        {
            "coin": ["BTC"] * len(mids),
            "ts_ms": [DAY + offset * 1000 for offset in mids],
            "mid": list(mids.values()),
            "notional_usd": [notional] * len(mids),
        }
    )


def flat(seconds: range, px: float = 100.0) -> dict[int, float]:
    return dict.fromkeys(seconds, px)


def test_events_closer_than_the_join_gap_are_one_episode() -> None:
    """Two long liquidations 30s apart are one cascade; one 90s later is a new one."""
    episodes = group_episodes(
        liqs((0, "long", 100.0, 1e6), (30, "long", 99.0, 1e6), (120, "long", 98.0, 1e6)),
        bars(flat(range(0, 200))),
        join_gap_s=60.0,
        peak_window_s=15.0,
        participation=0.25,
        participation_window_s=60.0,
    )
    assert episodes["start_ts"].to_list() == [DAY, DAY + 120_000]
    assert episodes["end_ts"].to_list() == [DAY + 30_000, DAY + 120_000]
    assert episodes["n_events"].to_list() == [2, 1]
    assert episodes["total_notional"].to_list() == [2e6, 1e6]


def test_the_extreme_is_the_worst_mid_of_the_cascade_in_the_liquidated_direction() -> None:
    """Long liquidations are forced selling, so their extreme is the low, shorts the high.

    The search runs to `end_ts + join_gap_s` because that silence is what ended the
    episode: price impact from the last event lands in the seconds after it, and a lone
    liquidation would otherwise record anchor == extreme and a zero move.
    """
    mids = flat(range(0, 30)) | flat(range(30, 60), 97.0) | flat(range(60, 200), 98.0)
    mids[150] = 90.0  # well past end_ts + join_gap_s, must not be the extreme
    episode = group_episodes(
        liqs((0, "long", 100.0, 1e6), (30, "long", 99.0, 1e6)),
        bars(mids),
        join_gap_s=60.0,
        peak_window_s=15.0,
        participation=0.25,
        participation_window_s=60.0,
    ).row(0, named=True)
    assert episode["anchor_px"] == 100.0
    assert episode["extreme_px"] == 97.0
    assert episode["extreme_ts"] == DAY + 30_000


def test_a_short_liquidation_episode_takes_the_high_as_its_extreme() -> None:
    mids = flat(range(0, 30)) | flat(range(30, 200), 103.0)
    mids[45] = 105.0
    episode = group_episodes(
        liqs((0, "short", 100.0, 1e6)),
        bars(mids),
        join_gap_s=60.0,
        peak_window_s=15.0,
        participation=0.25,
        participation_window_s=60.0,
    ).row(0, named=True)
    assert episode["anchor_px"] == 100.0
    assert episode["extreme_px"] == 105.0
    assert episode["extreme_ts"] == DAY + 45_000


def test_peak_liq_is_the_busiest_trailing_window_inside_the_episode() -> None:
    """Four events, so the answer is countable: the (t-15s, t] window at t=10s holds three."""
    episode = group_episodes(
        liqs(
            (0, "long", 100.0, 1e6),
            (5, "long", 100.0, 1e6),
            (10, "long", 100.0, 1e6),
            (20, "long", 100.0, 1e6),
        ),
        bars(flat(range(0, 200))),
        join_gap_s=60.0,
        peak_window_s=15.0,
        participation=0.25,
        participation_window_s=60.0,
    ).row(0, named=True)
    assert episode["peak_liq_15s"] == 3e6


def test_capacity_is_a_share_of_the_notional_traded_after_the_extreme() -> None:
    """1000 USD a second for the 60s after a 30s extreme, at 25%, is 15000 USD."""
    mids = flat(range(0, 30)) | flat(range(30, 200), 97.0)
    episode = group_episodes(
        liqs((0, "long", 100.0, 1e6)),
        bars(mids, notional=1000.0),
        join_gap_s=60.0,
        peak_window_s=15.0,
        participation=0.25,
        participation_window_s=60.0,
    ).row(0, named=True)
    assert episode["extreme_ts"] == DAY + 30_000
    assert episode["notional_at_25pct_participation"] == 15_000.0


def qualified(episodes: pl.DataFrame, **overrides: float) -> pl.DataFrame:
    kwargs: dict[str, float] = {
        "min_notional_usd": 250_000.0,
        "min_move_bps": 30.0,
        "p95_lookback_d": 30,
        "liq_z_min": 3.0,
    }
    return qualify_episodes(episodes, **(kwargs | overrides))  # type: ignore[arg-type]


def three_episodes() -> pl.DataFrame:
    """Small and deep, huge and shallow, then one clearing both floors."""
    mids = (
        flat(range(0, 30))
        | flat(range(30, 300), 95.0)
        | flat(range(300, 330), 95.0)
        | flat(range(330, 600), 94.9)
        | flat(range(600, 730), 94.9)
        | flat(range(730, 900), 94.0)
    )
    return group_episodes(
        liqs(
            (0, "long", 100.0, 200_000.0),
            (300, "long", 95.0, 10e6),
            (700, "long", 94.9, 1e6),
        ),
        bars(mids),
        join_gap_s=60.0,
        peak_window_s=15.0,
        participation=0.25,
        participation_window_s=60.0,
    )


def test_the_absolute_floors_drop_small_notionals_and_shallow_moves() -> None:
    """500 bps on 200k fails the notional floor; 10 bps on 10M fails the move floor."""
    floors = qualified(three_episodes())
    assert floors["total_notional"].to_list() == [200_000.0, 10e6, 1e6]
    assert [round(bps, 1) for bps in floors["move_bps"].to_list()] == [500.0, 10.5, 94.8]
    assert floors["passes_floors"].to_list() == [False, False, True]


def test_the_p95_threshold_cannot_see_the_episodes_that_come_after_it() -> None:
    """The no-lookahead rule for qualification: later episodes never move an earlier bar."""
    before = qualified(three_episodes())
    later = liqs(
        (0, "long", 100.0, 200_000.0),
        (300, "long", 95.0, 10e6),
        (700, "long", 94.9, 1e6),
        (2000, "long", 94.0, 500e6),  # a monster, long after the three above
    )
    mids = (
        flat(range(0, 30))
        | flat(range(30, 330), 95.0)
        | flat(range(330, 730), 94.9)
        | flat(range(730, 2030), 94.0)
        | flat(range(2030, 2200), 90.0)
    )
    after = qualified(
        group_episodes(
            later,
            bars(mids),
            join_gap_s=60.0,
            peak_window_s=15.0,
            participation=0.25,
            participation_window_s=60.0,
        )
    )
    assert after.height == 4
    assert before.to_dicts() == after.head(3).to_dicts()
    assert before["p95_notional"][0] is None  # nothing precedes the first episode


def test_a_high_z_qualifies_an_episode_the_p95_rule_would_have_dropped() -> None:
    """1e6 against a 9.5e6 trailing p95 is small for the coin, but z 5 says it is a cascade."""
    episodes = three_episodes().with_columns(pl.Series("max_liq_z", [0.0, 0.0, 5.0]))
    marked = qualified(episodes)
    # Trailing p95 by hand: nothing precedes the first, the second is judged against
    # {200k} so it clears, and the third against {200k, 10M} whose p95 is 9.51M.
    assert marked["passes_p95"].to_list() == [False, True, False]
    assert marked["passes_z"].to_list() == [False, False, True]
    assert marked["qualified"].to_list() == [False, False, True]
