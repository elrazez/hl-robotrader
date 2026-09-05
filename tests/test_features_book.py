"""Spread, depth and book imbalance from L2 snapshots, and the nulls when there are none.

The historical year has no bbo at all and only hourly L2 snapshots on four coins, so for
almost every research window these columns are null. That is the point of the tests: null
has to mean absent, never zero, or a caller reads "no depth" as "no liquidity" and blocks a
trade for a reason that does not exist.
"""

from __future__ import annotations

import polars as pl
import pytest

from liqfade.data.schemas import SOURCE_HL_ARCHIVE, rows_to_table
from liqfade.features.book import book_features

DAY = 1_772_323_200_000
STEP = 1000


def snapshot(
    ts_ms: int, levels: list[tuple[float, float, float, float]]
) -> list[dict[str, object]]:
    return [
        {
            "coin": "BTC",
            "ts_ms": ts_ms,
            "rx_ms": 0,
            "level": index,
            "bid_px": bid_px,
            "bid_sz": bid_sz,
            "ask_px": ask_px,
            "ask_sz": ask_sz,
        }
        for index, (bid_px, bid_sz, ask_px, ask_sz) in enumerate(levels)
    ]


def l2(*snapshots: list[dict[str, object]]) -> pl.DataFrame:
    rows = [row for snap in snapshots for row in snap]
    return pl.from_arrow(rows_to_table("l2", rows, SOURCE_HL_ARCHIVE))


def grid(seconds: int) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "coin": ["BTC"] * seconds,
            "ts_ms": [DAY + i * STEP for i in range(seconds)],
            "mid": [100.0] * seconds,
        }
    )


def at(frame: pl.DataFrame, second: int, column: str) -> object:
    return frame.filter(pl.col("ts_ms") == DAY + second * STEP)[column][0]


def test_the_spread_comes_from_the_touch_of_the_last_snapshot_before_t() -> None:
    book = book_features(
        grid(20),
        l2(snapshot(DAY + 5000, [(99.95, 1.0, 100.05, 1.0)])),
        depth_bps=10.0,
    )
    assert at(book, 5, "spread_bps") is None  # the snapshot stamped at t is not yet knowable
    assert at(book, 6, "spread_bps") == pytest.approx(10.0)


def test_depth_counts_only_the_levels_inside_the_band_and_imbalance_stays_in_one() -> None:
    """10 bps of 100 is a penny either way, so the third level on each side is outside it."""
    levels = [
        (99.99, 2.0, 100.01, 1.0),
        (99.95, 3.0, 100.05, 1.0),
        (99.80, 100.0, 100.20, 100.0),
    ]
    book = book_features(grid(20), l2(snapshot(DAY + 5000, levels)), depth_bps=10.0)
    assert at(book, 6, "depth_bid") == pytest.approx(99.99 * 2 + 99.95 * 3)
    assert at(book, 6, "depth_ask") == pytest.approx(100.01 + 100.05)
    imbalance = at(book, 6, "book_imbalance")
    assert isinstance(imbalance, float) and 0.0 < imbalance <= 1.0


def test_a_window_with_no_l2_gets_nulls_and_not_zeros() -> None:
    """The year of history, and every coin outside the four the archive snapshots."""
    book = book_features(grid(10), pl.DataFrame(), depth_bps=10.0)
    for column in ("spread_bps", "depth_bid", "depth_ask", "book_imbalance"):
        assert book[column].null_count() == book.height, column


def test_a_snapshot_taken_later_cannot_change_an_earlier_row() -> None:
    early = l2(snapshot(DAY + 2000, [(99.95, 1.0, 100.05, 1.0)]))
    late = pl.concat([early, l2(snapshot(DAY + 8000, [(90.0, 500.0, 110.0, 500.0)]))])
    cut = pl.col("ts_ms") <= DAY + 8000
    assert (
        book_features(grid(20), early, depth_bps=10.0).filter(cut).to_dicts()
        == book_features(grid(20), late, depth_bps=10.0).filter(cut).to_dicts()
    )
