"""The order proof: broken `start_position` chains are counted, perps apart from spot.

This is the check the whole 02d repair rests on, so it has to be wrong-answer-proof in
both directions: it must count a real reordering, and it must not count the spot balance
moves that are not reorderings at all. Both are asserted here against a partition built
row by row, where the right answer is known.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import duckdb
import pytest

from liqfade.data.schemas import SOURCE_HL_S3, rows_to_table
from liqfade.recorder.writer import write_atomic

SCRIPT = Path(__file__).parents[1] / "scripts" / "research" / "fills_order_check.py"

USER = "0x1111111111111111111111111111111111111111"
OTHER = "0x2222222222222222222222222222222222222222"


def load() -> Any:
    spec = importlib.util.spec_from_file_location("fills_order_check", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = load()


def fill(**overrides: Any) -> dict[str, Any]:
    """One `fills_full` row. Only the columns the chain walks have to be real."""
    row: dict[str, Any] = {
        "coin": "BTC",
        "ts_ms": 1_000,
        "rx_ms": 0,
        "block_time": 1_000,
        "block_number": 1,
        "local_time": 1_000,
        "user_address": USER,
        "px": 78_000.0,
        "sz": 1.0,
        "side": "B",
        "start_position": 0.0,
        "dir": "Open Long",
        "closed_pnl": 0.0,
        "hash": "0xabc",
        "oid": 1,
        "crossed": True,
        "fee": 0.0,
        "fee_token": "USDC",
        "tid": 1,
        "cloid": None,
        "builder": None,
        "builder_fee": None,
        "twap_id": None,
        "liq_user": None,
        "liq_mark_px": None,
        "liq_method": None,
    }
    return row | overrides


def write(directory: Path, name: str, rows: list[dict[str, Any]]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.parquet"
    write_atomic(rows_to_table("fills_full", rows, SOURCE_HL_S3), path)
    return path


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    return duckdb.connect()


def test_a_chain_that_adds_up_has_no_violations(con: Any, tmp_path: Path) -> None:
    """Buys add to the position, sells subtract, and each fill starts where the last ended."""
    rows = [
        fill(tid=1, ts_ms=1_000, side="B", sz=2.0, start_position=0.0),
        fill(tid=2, ts_ms=2_000, side="B", sz=3.0, start_position=2.0),
        fill(tid=3, ts_ms=3_000, side="A", sz=1.5, start_position=5.0),
        fill(tid=4, ts_ms=4_000, side="B", sz=0.5, start_position=3.5),
    ]
    result = check.check_day(con, [write(tmp_path, "hyperliquid_00", rows)])
    assert result["perp"] == {
        "links": 3,
        "violations": 0,
        "violations_same_ms": 0,
        "worst_err": 0.0,
    }


def test_a_break_inside_one_millisecond_is_counted_as_such(con: Any, tmp_path: Path) -> None:
    """The interesting case: two fills sharing a `ts_ms` that do not chain.

    That is what a reordering inside a millisecond looks like, and it is the only thing
    in this check that would falsify the ordering claim.
    """
    rows = [
        fill(tid=1, ts_ms=1_000, side="B", sz=2.0, start_position=0.0),
        fill(tid=2, ts_ms=1_000, side="B", sz=3.0, start_position=9.0),  # should be 2.0
    ]
    result = check.check_day(con, [write(tmp_path, "hyperliquid_00", rows)])
    assert result["perp"]["links"] == 1
    assert result["perp"]["violations"] == 1
    assert result["perp"]["violations_same_ms"] == 1
    assert result["perp"]["worst_err"] == pytest.approx(7.0)


def test_each_user_and_coin_chains_on_its_own(con: Any, tmp_path: Path) -> None:
    """Interleaved accounts and coins must not be walked as one position."""
    rows = [
        fill(tid=1, ts_ms=1_000, side="B", sz=2.0, start_position=0.0),
        fill(tid=2, ts_ms=1_100, side="B", sz=7.0, start_position=0.0, user_address=OTHER),
        fill(tid=3, ts_ms=1_200, side="B", sz=5.0, start_position=0.0, coin="ETH"),
        fill(tid=4, ts_ms=1_300, side="B", sz=1.0, start_position=2.0),
        fill(tid=5, ts_ms=1_400, side="A", sz=1.0, start_position=7.0, user_address=OTHER),
        fill(tid=6, ts_ms=1_500, side="A", sz=2.0, start_position=5.0, coin="ETH"),
    ]
    result = check.check_day(con, [write(tmp_path, "hyperliquid_00", rows)])
    assert result["perp"]["links"] == 3
    assert result["perp"]["violations"] == 0


def test_spot_breaks_are_counted_apart_from_perp_breaks(con: Any, tmp_path: Path) -> None:
    """Only perps carry the ordering claim.

    On a spot market `start_position` is the base token balance, which also moves on
    deposits, transfers and the fee when the fee is taken in that token, none of which
    are in this file. Measured on 2026-01-10: 48% of spot links break and 0 of 2,379,722
    perp links do, so folding the two together would drown the signal.
    """
    rows = [
        fill(tid=1, ts_ms=1_000, coin="@107", side="B", sz=100.0, start_position=0.0),
        # the fee was taken in the token, so the balance is short of the round number
        fill(tid=2, ts_ms=2_000, coin="@107", side="B", sz=50.0, start_position=99.9),
        fill(tid=3, ts_ms=1_000, coin="PURR/USDC", side="B", sz=10.0, start_position=0.0),
        fill(tid=4, ts_ms=2_000, coin="PURR/USDC", side="B", sz=5.0, start_position=9.99),
        fill(tid=5, ts_ms=1_000, side="B", sz=2.0, start_position=0.0),
        fill(tid=6, ts_ms=2_000, side="B", sz=3.0, start_position=2.0),
    ]
    result = check.check_day(con, [write(tmp_path, "hyperliquid_00", rows)])
    assert result["perp"] == {
        "links": 1,
        "violations": 0,
        "violations_same_ms": 0,
        "worst_err": 0.0,
    }
    assert result["spot"]["links"] == 2
    assert result["spot"]["violations"] == 2


def test_the_chain_follows_the_file_list_and_not_the_file_name(con: Any, tmp_path: Path) -> None:
    """Ties inside a millisecond are broken by position in the list `check_day` is given.

    The hourly files of an un-compacted day do not sort in write order, so the caller
    passes `sorted(lake_files(...))` and that order is what the walk uses. Read the same
    two files the other way round and the chain that added up no longer does.
    """
    first = write(tmp_path, "a", [fill(tid=1, ts_ms=1_000, side="B", sz=2.0, start_position=0.0)])
    second = write(tmp_path, "b", [fill(tid=2, ts_ms=1_000, side="B", sz=3.0, start_position=2.0)])

    assert check.check_day(con, [first, second])["perp"]["violations"] == 0
    assert check.check_day(con, [second, first])["perp"]["violations"] == 1


def test_a_missing_partition_is_reported_and_exits_non_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = check.main(["2026-03-01", "--data-dir", str(tmp_path)])
    assert code == 1
    assert "no fills_full partition" in capsys.readouterr().out
