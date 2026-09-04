"""The polars twins must equal the dict derivations row for row and in order.

That equality is the contract. `liqfade/data/ingest/hl_s3.py`'s `derive_trades` and
`derive_liquidations` are the reference implementation and the S3 parser still runs them;
the twins in `liqfade/data/derive/from_fills.py` exist only because a year of `fills_full`
through Python dicts is hours. If the two ever disagree, the dict function is right.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl
import pytest

from liqfade.data.derive.from_fills import (
    FILL_COLUMNS,
    derive_liquidations_frame,
    derive_trades_frame,
    read_fills,
)
from liqfade.data.ingest.common import read_lz4_lines
from liqfade.data.ingest.hl_s3 import derive_liquidations, derive_trades, parse_fills
from liqfade.data.schemas import SOURCE_HL_S3, rows_to_table
from liqfade.recorder.writer import write_atomic

SAMPLE_LZ4 = Path(__file__).parent / "fixtures" / "ingest" / "node_fills_by_block_sample.lz4"

VICTIM = "0x1111111111111111111111111111111111111111"
TAKER = "0x4444444444444444444444444444444444444444"
MAKER = "0x3333333333333333333333333333333333333333"


def as_frame(fills: list[dict[str, Any]]) -> pl.DataFrame:
    """Parsed fills as the `fills_full` frame a reader would get back off disk."""
    arrow = rows_to_table("fills_full", fills, SOURCE_HL_S3)
    return pl.from_arrow(arrow).select(FILL_COLUMNS)


def fill(**overrides: Any) -> dict[str, Any]:
    """One `fills_full` row. Only the columns the derivations read have to be real."""
    row = {
        "coin": "BTC",
        "ts_ms": 1_788_609_540_000,
        "rx_ms": 0,
        "block_time": 1_788_609_540_000,
        "block_number": 800_000_001,
        "local_time": 1_788_609_540_020,
        "user_address": TAKER,
        "px": 78_862.0,
        "sz": 0.5,
        "side": "A",
        "start_position": 1.5,
        "dir": "Close Long",
        "closed_pnl": 0.0,
        "hash": "0xf1a7f7bd",
        "oid": 1,
        "crossed": True,
        "fee": 5.93,
        "fee_token": "USDC",
        "tid": 111,
        "cloid": None,
        "builder": None,
        "builder_fee": None,
        "twap_id": None,
        "liq_user": None,
        "liq_mark_px": None,
        "liq_method": None,
    }
    return row | overrides


@pytest.fixture
def fixture_fills() -> list[dict[str, Any]]:
    return list(parse_fills(read_lz4_lines(SAMPLE_LZ4)))


# --- the contract -------------------------------------------------------------


def test_the_trades_twin_equals_the_dict_derivation_on_the_fixture(
    fixture_fills: list[dict[str, Any]],
) -> None:
    assert derive_trades_frame(as_frame(fixture_fills)).to_dicts() == derive_trades(fixture_fills)


def test_the_liquidations_twin_equals_the_dict_derivation_on_the_fixture(
    fixture_fills: list[dict[str, Any]],
) -> None:
    frame = derive_liquidations_frame(as_frame(fixture_fills))
    assert frame.to_dicts() == derive_liquidations(fixture_fills)


def test_both_twins_equal_the_dict_derivation_on_every_pairing_shape() -> None:
    """Maker-only groups, taker-only groups, and both halves of a liquidation.

    The fallback branches are the ones a fixture can miss: a group with no `crossed`
    record inverts the side, and a liquidation whose victim's own fill is absent inverts
    it the other way. Both are exercised here against the reference.
    """
    fills = [
        # a normal pair, taker sells
        fill(tid=1, side="A", crossed=True),
        fill(tid=1, side="B", crossed=False, user_address=MAKER),
        # maker's half only: the aggressor is the opposite of this row's side
        fill(tid=2, side="B", crossed=False, user_address=MAKER),
        # a liquidation with the victim's own record present
        fill(
            tid=3,
            side="A",
            crossed=False,
            user_address=VICTIM,
            liq_user=VICTIM,
            liq_method="market",
        ),
        fill(
            tid=3, side="B", crossed=True, user_address=TAKER, liq_user=VICTIM, liq_method="market"
        ),
        # a liquidation where only the counterparty's half is in the file
        fill(
            tid=4,
            side="B",
            crossed=True,
            user_address=TAKER,
            liq_user=VICTIM,
            liq_method="backstop",
        ),
        # a liquidation with no method: "unknown", not null
        fill(tid=5, side="A", crossed=True, user_address=VICTIM, liq_user=VICTIM),
    ]
    frame = as_frame(fills)
    assert derive_trades_frame(frame).to_dicts() == derive_trades(fills)
    assert derive_liquidations_frame(frame).to_dicts() == derive_liquidations(fills)


# --- order --------------------------------------------------------------------


def test_two_fills_in_one_millisecond_come_out_in_file_order_not_tid_order() -> None:
    """The bug 02d repairs: `tid` is a hash, so sorting on it picks an arbitrary open."""
    fills = [
        fill(tid=997296749534590, px=100.0),
        fill(tid=653061949558227, px=101.0),
    ]
    frame = derive_trades_frame(as_frame(fills))
    assert frame.get_column("tid").to_list() == [997296749534590, 653061949558227]
    assert frame.get_column("px").to_list() == [100.0, 101.0]


def test_a_millisecond_split_across_two_files_keeps_the_order_of_the_file_list(
    tmp_path: Path,
) -> None:
    """`read_fills` concatenates in the order it is given, whatever the names sort like.

    The hourly files of an un-compacted day are named `hyperliquid_07_2_ab12cd.parquet`
    by the staging mover, so `hyperliquid_07_10_x` sorts before `hyperliquid_07_2_x`.
    That is the order they are read in and the order their rows must keep.
    """
    for name, tid in (("hyperliquid_07_10_aaaaaa", 20), ("hyperliquid_07_2_bbbbbb", 10)):
        write_atomic(
            rows_to_table("fills_full", [fill(tid=tid)], SOURCE_HL_S3), tmp_path / f"{name}.parquet"
        )

    files = sorted(tmp_path.glob("*.parquet"))
    assert [path.stem for path in files] == ["hyperliquid_07_10_aaaaaa", "hyperliquid_07_2_bbbbbb"]
    assert derive_trades_frame(read_fills(files)).get_column("tid").to_list() == [20, 10]


def test_the_stable_sort_moves_a_late_millisecond_without_disturbing_its_neighbours() -> None:
    """Sorting is on `ts_ms` alone; rows sharing one keep the order they arrived in."""
    fills = [
        fill(tid=1, ts_ms=2_000, block_time=2_000),
        fill(tid=2, ts_ms=1_000, block_time=1_000),
        fill(tid=3, ts_ms=2_000, block_time=2_000),
    ]
    frame = derive_trades_frame(as_frame(fills))
    assert frame.get_column("tid").to_list() == [2, 1, 3]


# --- edges --------------------------------------------------------------------


def test_a_day_with_no_liquidations_derives_an_empty_frame_with_the_right_columns() -> None:
    frame = derive_liquidations_frame(as_frame([fill(tid=1)]))
    assert frame.height == 0
    assert (
        frame.columns
        == derive_liquidations_frame(
            as_frame([fill(tid=1, liq_user=VICTIM, user_address=VICTIM)])
        ).columns
    )
