"""Live versus replay: matching one side's entry attempts against the other's.

Hand-built and pure. Timestamps are round offsets from a fixed start so a reader can
check which pair the matcher should prefer without running anything, which matters here
because a wrong pairing does not fail loudly: it reports drift that is not there.
"""

from __future__ import annotations

from liqfade.backtest.drift import IntentRef, match_intents

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
