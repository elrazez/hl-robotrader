"""The shared value conversions. One rule, one place: absent is null, never zero."""

from __future__ import annotations

import pytest

from liqfade.data.parse import to_float, to_int, to_ms


@pytest.mark.parametrize("absent", [None, ""])
def test_an_absent_value_is_null_not_zero(absent: object) -> None:
    """A 0.0 price and a missing price mean different things to every feature."""
    assert to_float(absent) is None
    assert to_int(absent) is None
    assert to_ms(absent) is None


def test_numbers_arrive_as_strings() -> None:
    assert to_float("78400.5") == 78400.5
    assert to_int("997296749534590") == 997296749534590


def test_epoch_milliseconds_pass_through() -> None:
    assert to_ms(1_788_609_540_000) == 1_788_609_540_000


def test_an_iso_string_becomes_milliseconds_and_a_naive_one_is_utc() -> None:
    assert to_ms("2026-09-01T12:00:00Z") == to_ms("2026-09-01T12:00:00")
    assert to_ms("2026-09-01T12:00:00+00:00") == 1_788_264_000_000


def test_the_conversion_rounds_rather_than_truncating() -> None:
    """`timestamp() * 1000` lands just under a whole millisecond as often as on one."""
    assert to_ms("2026-09-01T12:00:00.9996Z") == 1_788_264_001_000
    assert to_ms("2026-09-01T12:00:00.0004Z") == 1_788_264_000_000
