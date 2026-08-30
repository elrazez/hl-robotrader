"""Read the committed five minute capture the way the backtest will read the lake.

These are the tests that would have caught a parser writing nulls, a schema drifting, or
a partition layout nobody can query. They run offline: the data is in the repo.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow.parquet as pq
import pytest

from liqfade.data.schemas import validate_table

LAKE = Path(__file__).parent / "fixtures" / "parquet"
TABLES = sorted(path.name for path in LAKE.iterdir() if path.is_dir())


def read(table: str) -> duckdb.DuckDBPyRelation:
    return duckdb.sql(f"select * from read_parquet('{LAKE}/{table}/date=*/*.parquet')")


def test_the_fixture_covers_the_hyperliquid_streams_and_one_venue_of_liquidations() -> None:
    assert set(TABLES) == {"asset_ctx", "bbo", "candles_1m", "l2", "liquidations", "trades"}


@pytest.mark.parametrize("table", TABLES)
def test_every_file_matches_its_canonical_schema(table: str) -> None:
    for path in sorted((LAKE / table).glob("date=*/*.parquet")):
        validate_table(table, pq.read_table(path, partitioning=None))


@pytest.mark.parametrize("table", TABLES)
def test_duckdb_can_count_every_table(table: str) -> None:
    """The Phase 1 definition of done: duckdb reads what the recorder wrote."""
    assert read(table).aggregate("count(*)").fetchone()[0] > 0


@pytest.mark.parametrize("table", TABLES)
def test_the_partition_layout_is_the_documented_one(table: str) -> None:
    for path in sorted((LAKE / table).glob("**/*.parquet")):
        assert path.parent.name.startswith("date=")
        assert path.parent.parent.name == table


def test_receive_time_is_never_before_exchange_time() -> None:
    """rx_ms < ts_ms would mean the no lookahead rule is unenforceable on this data."""
    for table in ("trades", "bbo", "l2", "liquidations"):
        assert read(table).filter("rx_ms < ts_ms").aggregate("count(*)").fetchone()[0] == 0


def test_recorded_rows_all_carry_a_receive_time() -> None:
    """rx_ms = 0 is reserved for historical pulls, which have no local receive time."""
    for table in ("trades", "bbo", "l2", "asset_ctx", "liquidations"):
        assert (
            read(table).filter("rx_ms is null or rx_ms = 0").aggregate("count(*)").fetchone()[0]
            == 0
        )


def distinct(table: str, column: str) -> set[object]:
    return {row[0] for row in read(table).aggregate(f"{column}").fetchall()}


def test_enum_columns_only_hold_the_documented_values() -> None:
    assert distinct("trades", "side") <= {"buy", "sell"}
    assert distinct("liquidations", "side") <= {"long", "short"}
    assert distinct("liquidations", "method") <= {"market", "backstop", "unknown"}


def test_the_book_is_recorded_twenty_levels_deep() -> None:
    assert distinct("l2", "level") == set(range(20))
    # Every snapshot has all twenty, so a depth feature never sees a ragged book.
    counts = read("l2").aggregate("count(*) n", "ts_ms").project("n").fetchall()
    assert {row[0] for row in counts} == {20}


def test_prices_are_populated_rather_than_null() -> None:
    assert (
        read("bbo").filter("bid_px is null or ask_px is null").aggregate("count(*)").fetchone()[0]
        == 0
    )
    assert (
        read("trades").filter("px is null or sz is null").aggregate("count(*)").fetchone()[0] == 0
    )


def test_asset_ctx_carries_the_fields_the_forced_flow_proxy_needs() -> None:
    row = (
        read("asset_ctx")
        .project("mark_px, oracle_px, mid_px, open_interest, day_ntl_vlm")
        .fetchone()
    )
    assert all(value is not None and value > 0 for value in row)


def test_compaction_over_the_real_fixture_preserves_every_row(tmp_path: Path) -> None:
    import shutil

    from liqfade.recorder.writer import compact_day

    shutil.copytree(LAKE, tmp_path / "parquet")
    before = {table: read(table).aggregate("count(*)").fetchone()[0] for table in TABLES}
    assert compact_day(tmp_path / "parquet", "2026-08-30") == before
    for table in TABLES:
        files = sorted((tmp_path / "parquet" / table / "date=2026-08-30").glob("*.parquet"))
        assert [path.name.endswith("_day.parquet") for path in files] == [True]
