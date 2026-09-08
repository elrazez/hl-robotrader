"""Live versus replay: matching one side's entry attempts against the other's.

Hand-built and pure. Timestamps are round offsets from a fixed start so a reader can
check which pair the matcher should prefer without running anything, which matters here
because a wrong pairing does not fail loudly: it reports drift that is not there.
"""

from __future__ import annotations

from liqfade.backtest.drift import (
    Dimension,
    DriftReport,
    Hole,
    IntentRef,
    RoundTrip,
    Side,
    Verdict,
    compare,
    match_intents,
)
from liqfade.config.loader import resolve
from liqfade.config.models import Settings

SETTINGS = Settings.model_validate(resolve("paper")[0])

START = 1_700_000_000_000
TOL = 2_000


def ref(coin: str = "ETH", side: str = "long", offset_ms: int = 0, **over: object) -> IntentRef:
    return IntentRef(coin=coin, side=side, ts_ms=START + offset_ms, **over)  # type: ignore[arg-type]


def test_the_same_intent_on_both_sides_is_one_matched_pair() -> None:
    live, replay = [ref()], [ref()]
    result = match_intents(live, replay, tol_ms=TOL)
    assert result.matched == [(live[0], replay[0])]
    assert result.live_only == []
    assert result.replay_only == []


def test_a_pair_inside_the_tolerance_matches_and_one_outside_it_does_not() -> None:
    inside = match_intents([ref()], [ref(offset_ms=TOL - 1)], tol_ms=TOL)
    assert len(inside.matched) == 1

    outside = match_intents([ref()], [ref(offset_ms=TOL + 1)], tol_ms=TOL)
    assert outside.matched == []
    assert len(outside.live_only) == 1
    assert len(outside.replay_only) == 1


def test_the_tolerance_is_inclusive_at_its_edge() -> None:
    edge = match_intents([ref()], [ref(offset_ms=TOL)], tol_ms=TOL)
    assert len(edge.matched) == 1


def test_a_pair_matches_across_the_tolerance_in_either_direction() -> None:
    early = match_intents([ref()], [ref(offset_ms=-TOL)], tol_ms=TOL)
    assert len(early.matched) == 1


def test_two_intents_at_the_same_moment_on_different_coins_never_match() -> None:
    result = match_intents([ref(coin="ETH")], [ref(coin="BTC")], tol_ms=TOL)
    assert result.matched == []
    assert len(result.live_only) == 1
    assert len(result.replay_only) == 1


def test_two_intents_at_the_same_moment_on_opposite_sides_never_match() -> None:
    result = match_intents([ref(side="long")], [ref(side="short")], tol_ms=TOL)
    assert result.matched == []


def test_the_closer_of_two_candidates_wins_rather_than_the_earlier_one() -> None:
    """Greedy in timestamp order would pair live[0] with the only replay intent.

    Live fires at +0 and +1900, replay once at +1800. Both live intents are inside the
    tolerance of it, so a matcher that walked live in order would take the first and
    report the second as live-only. The nearer pair is the true one.
    """
    live = [ref(offset_ms=0), ref(offset_ms=1_900)]
    replay = [ref(offset_ms=1_800)]
    result = match_intents(live, replay, tol_ms=TOL)
    assert result.matched == [(live[1], replay[0])]
    assert result.live_only == [live[0]]


def test_one_replay_intent_is_never_matched_to_two_live_intents() -> None:
    live = [ref(offset_ms=0), ref(offset_ms=100)]
    replay = [ref(offset_ms=50)]
    result = match_intents(live, replay, tol_ms=TOL)
    assert len(result.matched) == 1
    assert len(result.live_only) == 1
    assert result.replay_only == []


def test_the_leftovers_come_back_in_timestamp_order() -> None:
    live = [ref(offset_ms=9_000), ref(offset_ms=1_000)]
    result = match_intents(live, [], tol_ms=TOL)
    assert [item.ts_ms for item in result.live_only] == [START + 1_000, START + 9_000]


def test_two_empty_sides_match_nothing_and_raise_nothing() -> None:
    result = match_intents([], [], tol_ms=TOL)
    assert result.matched == []
    assert result.live_only == []
    assert result.replay_only == []


def test_matching_is_deterministic_when_two_candidates_are_equally_close() -> None:
    """A tie has to resolve the same way twice, or a rerun reports different drift."""
    live = [ref(offset_ms=0)]
    replay = [ref(offset_ms=-500), ref(offset_ms=500)]
    first = match_intents(live, replay, tol_ms=TOL)
    second = match_intents(live, replay, tol_ms=TOL)
    assert first.matched == second.matched
    assert first.replay_only == second.replay_only


# --- the verdicts ---------------------------------------------------------------------

TRIP_KW: dict[str, object] = {
    "entry_px": 100.0,
    "exit_px": 101.0,
    "exit_reason": "target",
    "net_pnl": 10.0,
    "notional": 1_000.0,
}


def trip(offset_ms: int = 0, exit_offset_ms: int = 60_000, **over: object) -> RoundTrip:
    kw = {**TRIP_KW, **over}
    return RoundTrip(
        coin=str(kw.pop("coin", "ETH")),
        side=str(kw.pop("side", "long")),
        ts_ms=START + offset_ms,
        exit_ms=START + offset_ms + exit_offset_ms,
        **kw,  # type: ignore[arg-type]
    )


def side(**over: object) -> Side:
    base: dict[str, object] = {
        "intents": [],
        "round_trips": [],
        "refusals": {},
        "config_hash": "abc",
    }
    return Side(**{**base, **over})  # type: ignore[arg-type]


def dimension(report: DriftReport, name: str) -> Dimension:
    return next(d for d in report.dimensions if d.name == name)


# --- config ---------------------------------------------------------------------------


def test_two_equal_config_hashes_match() -> None:
    report = compare(side(), side(), settings=SETTINGS)
    assert dimension(report, "config").verdict is Verdict.MATCH


def test_an_unknown_live_config_hash_is_not_comparable_and_is_never_a_match() -> None:
    """The running soak predates the hash on `paper_started`. Absence is not agreement."""
    report = compare(side(config_hash=None), side(), settings=SETTINGS)
    assert dimension(report, "config").verdict is Verdict.NOT_COMPARABLE


def test_unequal_config_hashes_drift_and_stop_the_report_there() -> None:
    """Nothing below config is comparable once the two sides ran different config."""
    report = compare(side(config_hash="aaa"), side(config_hash="bbb"), settings=SETTINGS)
    assert [d.name for d in report.dimensions] == ["config"]
    assert dimension(report, "config").verdict is Verdict.DRIFT
    assert report.verdict is Verdict.DRIFT


def test_an_unknown_hash_does_not_stop_the_report() -> None:
    report = compare(side(config_hash=None), side(), settings=SETTINGS)
    assert [d.name for d in report.dimensions] != ["config"]


# --- intents --------------------------------------------------------------------------


def test_intents_match_when_both_sides_are_almost_entirely_paired() -> None:
    live = [ref(offset_ms=i * 60_000) for i in range(10)]
    replay = [ref(offset_ms=i * 60_000 + 500) for i in range(10)]
    report = compare(side(intents=live), side(intents=replay), settings=SETTINGS)
    assert dimension(report, "intents").verdict is Verdict.MATCH


def test_intents_drift_when_the_live_side_fired_ones_the_replay_never_did() -> None:
    live = [ref(offset_ms=i * 60_000) for i in range(10)]
    replay = [ref(offset_ms=i * 60_000 + 500) for i in range(5)]
    report = compare(side(intents=live), side(intents=replay), settings=SETTINGS)
    assert dimension(report, "intents").verdict is Verdict.DRIFT


def test_a_drifting_intents_dimension_names_the_unmatched_with_their_timestamps() -> None:
    live = [ref(offset_ms=0), ref(offset_ms=600_000)]
    report = compare(side(intents=live), side(intents=[ref(offset_ms=0)]), settings=SETTINGS)
    detail = " ".join(dimension(report, "intents").detail)
    assert str(START + 600_000) in detail


def test_two_sides_that_both_did_nothing_are_a_match_and_say_so() -> None:
    report = compare(side(), side(), settings=SETTINGS)
    intents = dimension(report, "intents")
    assert intents.verdict is Verdict.MATCH
    assert any("no intents" in line for line in intents.detail)


# --- entries --------------------------------------------------------------------------


def test_entries_match_when_the_matched_intents_filled_at_the_same_price() -> None:
    live = [ref(offset_ms=0, entry_px=100.00), ref(offset_ms=60_000, entry_px=100.00)]
    replay = [ref(offset_ms=0, entry_px=100.01), ref(offset_ms=60_000, entry_px=100.01)]
    report = compare(side(intents=live), side(intents=replay), settings=SETTINGS)
    assert dimension(report, "entries").verdict is Verdict.MATCH


def test_entries_drift_when_the_median_price_gap_is_over_the_bar() -> None:
    live = [ref(offset_ms=i * 60_000, entry_px=100.0) for i in range(4)]
    replay = [ref(offset_ms=i * 60_000, entry_px=100.5) for i in range(4)]
    report = compare(side(intents=live), side(intents=replay), settings=SETTINGS)
    entries = dimension(report, "entries")
    assert entries.verdict is Verdict.DRIFT
    assert entries.metrics[0].value > 5.0


def test_an_intent_that_became_an_order_on_only_one_side_is_not_priced_against_a_default() -> None:
    """A None entry price is not a zero. Diffing against one invents a 10,000 bps gap."""
    live = [ref(offset_ms=0, entry_px=100.0)]
    replay = [ref(offset_ms=0, entry_px=None)]
    report = compare(side(intents=live), side(intents=replay), settings=SETTINGS)
    assert dimension(report, "entries").verdict is Verdict.NOT_COMPARABLE


# --- exits ----------------------------------------------------------------------------


def test_exits_match_when_the_reasons_agree_and_the_prices_are_close() -> None:
    trips = [trip(offset_ms=i * 600_000) for i in range(10)]
    report = compare(side(round_trips=trips), side(round_trips=trips), settings=SETTINGS)
    assert dimension(report, "exits").verdict is Verdict.MATCH


def test_exits_drift_when_the_live_side_stopped_out_where_the_replay_took_target() -> None:
    live = [trip(offset_ms=i * 600_000, exit_reason="stop") for i in range(10)]
    replay = [trip(offset_ms=i * 600_000) for i in range(10)]
    report = compare(side(round_trips=live), side(round_trips=replay), settings=SETTINGS)
    assert dimension(report, "exits").verdict is Verdict.DRIFT


def test_exits_drift_on_price_even_when_every_reason_agrees() -> None:
    live = [trip(offset_ms=i * 600_000, exit_px=101.0) for i in range(10)]
    replay = [trip(offset_ms=i * 600_000, exit_px=102.0) for i in range(10)]
    report = compare(side(round_trips=live), side(round_trips=replay), settings=SETTINGS)
    assert dimension(report, "exits").verdict is Verdict.DRIFT


def test_exits_are_not_comparable_when_neither_side_completed_a_round_trip() -> None:
    report = compare(side(), side(), settings=SETTINGS)
    assert dimension(report, "exits").verdict is Verdict.NOT_COMPARABLE


# --- pnl ------------------------------------------------------------------------------


def test_pnl_matches_when_the_two_totals_are_inside_a_round_trip_of_costs() -> None:
    live = [trip(offset_ms=i * 600_000, net_pnl=10.0) for i in range(10)]
    replay = [trip(offset_ms=i * 600_000, net_pnl=10.05) for i in range(10)]
    report = compare(side(round_trips=live), side(round_trips=replay), settings=SETTINGS)
    assert dimension(report, "pnl").verdict is Verdict.MATCH


def test_pnl_drifts_when_the_gap_is_far_wider_than_costs_and_noise() -> None:
    live = [trip(offset_ms=i * 600_000, net_pnl=10.0) for i in range(10)]
    replay = [trip(offset_ms=i * 600_000, net_pnl=-500.0) for i in range(10)]
    report = compare(side(round_trips=live), side(round_trips=replay), settings=SETTINGS)
    assert dimension(report, "pnl").verdict is Verdict.DRIFT


def test_pnl_is_not_comparable_without_replay_trades_to_interval() -> None:
    report = compare(side(round_trips=[trip()]), side(), settings=SETTINGS)
    assert dimension(report, "pnl").verdict is Verdict.NOT_COMPARABLE


# --- refusals -------------------------------------------------------------------------


def test_refusals_match_when_every_reason_lands_within_its_allowance() -> None:
    live = {("filter", "spread_too_wide"): 100, ("risk", "daily_loss"): 3}
    replay = {("filter", "spread_too_wide"): 105, ("risk", "daily_loss"): 4}
    report = compare(side(refusals=live), side(refusals=replay), settings=SETTINGS)
    assert dimension(report, "refusals").verdict is Verdict.MATCH


def test_refusals_drift_when_one_reason_is_far_off_even_if_the_rest_agree() -> None:
    live = {("filter", "spread_too_wide"): 100, ("risk", "daily_loss"): 3}
    replay = {("filter", "spread_too_wide"): 105, ("risk", "daily_loss"): 40}
    report = compare(side(refusals=live), side(refusals=replay), settings=SETTINGS)
    assert dimension(report, "refusals").verdict is Verdict.DRIFT


def test_a_small_count_is_allowed_to_differ_by_two_rather_than_by_ten_percent() -> None:
    """Ten percent of three is nothing. Without the floor every rare gate reads as drift."""
    report = compare(
        side(refusals={("risk", "cooldown"): 1}),
        side(refusals={("risk", "cooldown"): 3}),
        settings=SETTINGS,
    )
    assert dimension(report, "refusals").verdict is Verdict.MATCH


def test_a_reason_only_one_side_ever_recorded_is_named_in_the_detail() -> None:
    report = compare(
        side(refusals={("filter", "spread_too_wide"): 50}),
        side(refusals={}),
        settings=SETTINGS,
    )
    assert any("spread_too_wide" in line for line in dimension(report, "refusals").detail)


def test_refusals_are_not_comparable_when_the_live_table_holds_nothing() -> None:
    """The soak that predates the refusals table has an empty one, not an agreeing one."""
    report = compare(side(refusals={}), side(refusals={("risk", "x"): 5}), settings=SETTINGS)
    assert dimension(report, "refusals").verdict is Verdict.NOT_COMPARABLE


def test_the_refusals_dimension_says_day_one_risk_gates_start_from_different_state() -> None:
    report = compare(
        side(refusals={("risk", "cooldown"): 1}),
        side(refusals={("risk", "cooldown"): 1}),
        settings=SETTINGS,
    )
    assert any("RiskState" in line for line in dimension(report, "refusals").detail)


# --- tape holes -----------------------------------------------------------------------


def test_a_hole_is_listed_with_the_coin_and_the_day_it_falls_in() -> None:
    hole = Hole(coin="ETH", start_ms=START, end_ms=START + 600_000)
    report = compare(side(), side(), holes=[hole], settings=SETTINGS)
    assert any("ETH" in line for line in dimension(report, "holes").detail)


def test_an_intent_inside_a_hole_is_excluded_from_the_intents_verdict_not_counted_against_it() -> (
    None
):
    """The recorder's socket and the loop's socket are two connections with two sets of
    holes. An intent the replay could not have seen is not evidence that the two drifted."""
    hole = Hole(coin="ETH", start_ms=START - 1, end_ms=START + 600_001)
    live = [ref(offset_ms=i * 60_000) for i in range(10)]
    report = compare(side(intents=live), side(), holes=[hole], settings=SETTINGS)
    assert dimension(report, "intents").verdict is Verdict.MATCH
    assert report.excluded_by_hole == 10


def test_a_hole_on_another_coin_excludes_nothing() -> None:
    hole = Hole(coin="BTC", start_ms=START - 1, end_ms=START + 600_001)
    live = [ref(offset_ms=i * 60_000) for i in range(10)]
    report = compare(side(intents=live), side(), holes=[hole], settings=SETTINGS)
    assert dimension(report, "intents").verdict is Verdict.DRIFT
    assert report.excluded_by_hole == 0


def test_no_holes_is_a_match_rather_than_a_dimension_with_nothing_to_say() -> None:
    report = compare(side(), side(), settings=SETTINGS)
    assert dimension(report, "holes").verdict is Verdict.MATCH


# --- the report as a whole ------------------------------------------------------------


def test_every_dimension_carries_its_threshold_beside_its_number() -> None:
    live = [ref(offset_ms=i * 60_000, entry_px=100.0) for i in range(4)]
    report = compare(side(intents=live), side(intents=live), settings=SETTINGS)
    for dim in report.dimensions:
        for metric in dim.metrics:
            assert metric.units
            assert metric.threshold is not None


def test_the_overall_verdict_is_the_worst_dimension_and_not_comparable_never_hides_drift() -> None:
    live = [ref(offset_ms=i * 60_000) for i in range(10)]
    report = compare(side(intents=live, config_hash=None), side(), settings=SETTINGS)
    assert report.verdict is Verdict.DRIFT


def test_a_tape_hole_is_not_comparable_rather_than_drift() -> None:
    """A dropped recorder socket is missing input, not the two sides disagreeing.

    Calling it DRIFT would make every week containing one gap a drifting week, and a
    verdict that is always DRIFT is a verdict nobody reads.
    """
    hole = Hole(coin="ETH", start_ms=START, end_ms=START + 600_000)
    report = compare(side(), side(), holes=[hole], settings=SETTINGS)
    assert dimension(report, "holes").verdict is Verdict.NOT_COMPARABLE
    assert report.verdict is Verdict.NOT_COMPARABLE


def test_a_round_trip_inside_a_hole_is_left_out_of_the_exits_verdict_too() -> None:
    hole = Hole(coin="ETH", start_ms=START - 1, end_ms=START + 600_001)
    live = [trip(offset_ms=0, exit_reason="stop")]
    replay = [trip(offset_ms=0, exit_reason="target")]
    report = compare(
        side(round_trips=live), side(round_trips=replay), holes=[hole], settings=SETTINGS
    )
    assert dimension(report, "exits").verdict is Verdict.NOT_COMPARABLE
