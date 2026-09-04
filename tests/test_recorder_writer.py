"""Parquet writes are atomic, and compaction never deletes data it did not fold in."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from liqfade.data.schemas import SchemaError, rows_to_table, validate_table
from liqfade.recorder.writer import compact_day, partition_dir, write_atomic, write_rows

DATE = "2026-08-30"


def read(path: Path) -> pa.Table:
    """The partition directory is named date=..., so hive discovery is turned off."""
    return pq.read_table(path, partitioning=None)


def liq(ts_ms: int, venue: str = "bybit") -> dict[str, object]:
    return {
        "venue": venue,
        "coin": "BTC",
        "venue_symbol": "BTCUSDT",
        "ts_ms": ts_ms,
        "rx_ms": ts_ms + 3,
        "side": "long",
        "px": 60000.0,
        "sz": 0.5,
        "notional_usd": 30000.0,
        "method": "unknown",
        "complete": True,
    }


def bbo(ts_ms: int) -> dict[str, object]:
    return {
        "coin": "BTC",
        "ts_ms": ts_ms,
        "rx_ms": ts_ms,
        "bid_px": 1.0,
        "bid_sz": 1.0,
        "ask_px": 2.0,
        "ask_sz": 1.0,
    }


def test_a_written_file_matches_the_canonical_schema(tmp_path: Path) -> None:
    path = write_rows(tmp_path, ("liquidations", "bybit", DATE, 12), [liq(1), liq(2)])
    assert path is not None
    assert path.name == "bybit_12.parquet"
    assert path.parent == partition_dir(tmp_path, "liquidations", DATE)
    table = read(path)
    validate_table("liquidations", table)
    assert table.num_rows == 2


def test_no_rows_writes_no_file(tmp_path: Path) -> None:
    assert write_rows(tmp_path, ("bbo", "hyperliquid", DATE, 12), []) is None


def test_a_second_flush_in_the_same_hour_does_not_overwrite_the_first(tmp_path: Path) -> None:
    key = ("bbo", "hyperliquid", DATE, 12)
    first = write_rows(tmp_path, key, [bbo(1)])
    second = write_rows(tmp_path, key, [bbo(2)])
    assert first != second
    assert second is not None and second.name == "hyperliquid_12_1.parquet"
    assert (
        sum(
            pq.read_metadata(p).num_rows
            for p in partition_dir(tmp_path, "bbo", DATE).glob("*.parquet")
        )
        == 2
    )


ROUNDS = 20


def test_a_reader_never_catches_a_partial_file(tmp_path: Path) -> None:
    """A reader globbing the partition only ever sees complete files.

    write_rows is synchronous and lands each file with temp + os.replace, so the property
    under test is that rename, not a thread race: the recorder is what calls it through
    asyncio.to_thread. A non-atomic writer (straight to the final path) fails here on the
    first round, because pyarrow is still flushing pages when read_table opens it.

    Bounded and counted rather than looped for a fixed wall clock: the writer adds a file
    each round and the reader re-reads the whole partition, so an unbounded version is
    quadratic and used to take 27 of the suite's 28 seconds.
    """
    directory = partition_dir(tmp_path, "bbo", DATE)
    reads = 0
    for i in range(1, ROUNDS + 1):
        write_rows(tmp_path, ("bbo", "hyperliquid", DATE, 12), [bbo(i)] * 50)
        for path in sorted(directory.glob("*.parquet")):
            pq.read_table(path)  # raises on a truncated file
            reads += 1
        assert not list(directory.glob("*.tmp"))  # never visible, not merely cleaned up
    assert reads == ROUNDS * (ROUNDS + 1) // 2
    assert len(list(directory.glob("*.parquet"))) == ROUNDS


def test_compaction_preserves_row_counts_and_deletes_the_hourlies(tmp_path: Path) -> None:
    for hour in (10, 11, 12):
        write_rows(
            tmp_path, ("bbo", "hyperliquid", DATE, hour), [bbo(hour * 1000 + i) for i in range(4)]
        )
    assert compact_day(tmp_path, DATE) == {"bbo": 12}

    directory = partition_dir(tmp_path, "bbo", DATE)
    files = sorted(p.name for p in directory.glob("*.parquet"))
    assert files == ["hyperliquid_day.parquet"]
    table = read(directory / "hyperliquid_day.parquet")
    validate_table("bbo", table)
    assert table.num_rows == 12
    # Sorted by exchange time so a range scan over the day is cheap.
    assert table.column("ts_ms").to_pylist() == sorted(table.column("ts_ms").to_pylist())


def test_a_table_several_venues_write_compacts_to_one_all_day_file(tmp_path: Path) -> None:
    write_rows(tmp_path, ("liquidations", "bybit", DATE, 10), [liq(1)])
    write_rows(tmp_path, ("liquidations", "okx", DATE, 11), [liq(2, "okx")])
    assert compact_day(tmp_path, DATE) == {"liquidations": 2}
    directory = partition_dir(tmp_path, "liquidations", DATE)
    assert sorted(p.name for p in directory.glob("*.parquet")) == ["all_day.parquet"]


def test_compaction_is_idempotent(tmp_path: Path) -> None:
    write_rows(tmp_path, ("bbo", "hyperliquid", DATE, 10), [bbo(1)])
    compact_day(tmp_path, DATE)
    assert compact_day(tmp_path, DATE) == {}
    assert (
        pq.read_metadata(partition_dir(tmp_path, "bbo", DATE) / "hyperliquid_day.parquet").num_rows
        == 1
    )


def test_compaction_leaves_the_hourlies_alone_when_it_fails(tmp_path: Path) -> None:
    """A corrupt or off-schema file must not cost us the day's data."""
    write_rows(tmp_path, ("bbo", "hyperliquid", DATE, 10), [bbo(1)])
    directory = partition_dir(tmp_path, "bbo", DATE)
    wrong = pa.table({"coin": ["BTC"], "ts_ms": [1]})
    pq.write_table(wrong, directory / "hyperliquid_11.parquet")

    with pytest.raises((SchemaError, pa.ArrowInvalid)):
        compact_day(tmp_path, DATE)
    assert sorted(p.name for p in directory.glob("*.parquet")) == [
        "hyperliquid_10.parquet",
        "hyperliquid_11.parquet",
    ]


def test_compacting_a_day_with_nothing_in_it_is_a_no_op(tmp_path: Path) -> None:
    assert compact_day(tmp_path, DATE) == {}
    write_rows(tmp_path, ("bbo", "hyperliquid", "2026-08-29", 10), [bbo(1)])
    assert compact_day(tmp_path, DATE) == {}


def test_compaction_streams_suffixed_files_across_hours_into_one_sorted_file(
    tmp_path: Path,
) -> None:
    """The backfill's names: a second flush of an hour, and the staging mover's hex tag."""
    directory = partition_dir(tmp_path, "bbo", DATE)
    directory.mkdir(parents=True)
    expected: list[int] = []
    for hour, names in {
        10: ["hyperliquid_10.parquet", "hyperliquid_10_1.parquet"],
        11: ["hyperliquid_11_2_a1b2c3.parquet"],
        12: ["hyperliquid_12_0_ffffff.parquet", "hyperliquid_12.parquet"],
    }.items():
        for index, name in enumerate(names):
            # Deliberately out of order inside the hour, so a missing sort shows up.
            stamps = [hour * 3_600_000 + index * 100 + offset for offset in (7, 1, 4)]
            expected += stamps
            write_atomic(
                rows_to_table("bbo", [bbo(ts) for ts in stamps], "recorder"), directory / name
            )

    assert compact_day(tmp_path, DATE) == {"bbo": len(expected)}
    assert [p.name for p in directory.glob("*.parquet")] == ["hyperliquid_day.parquet"]
    table = read(directory / "hyperliquid_day.parquet")
    validate_table("bbo", table)
    assert table.column("ts_ms").to_pylist() == sorted(expected)
    # One row group per hour, which is what makes an hour range scan cheap.
    assert pq.read_metadata(directory / "hyperliquid_day.parquet").num_row_groups == 3


def test_a_day_holding_backfilled_and_recorded_rows_compacts_to_all_day(tmp_path: Path) -> None:
    """`liquidations` carries hl_s3 rows and tardis rows (other venues) for the same day."""
    write_rows(
        tmp_path, ("liquidations", "hyperliquid", DATE, 10), [liq(1, "hyperliquid")], source="hl_s3"
    )
    write_rows(tmp_path, ("liquidations", "bybit", DATE, 11), [liq(2)], source="tardis")
    assert compact_day(tmp_path, DATE) == {"liquidations": 2}
    directory = partition_dir(tmp_path, "liquidations", DATE)
    assert sorted(p.name for p in directory.glob("*.parquet")) == ["all_day.parquet"]
    assert sorted(read(directory / "all_day.parquet").column("source").to_pylist()) == [
        "hl_s3",
        "tardis",
    ]


def test_an_unreadable_hour_in_a_file_name_is_an_error_not_hour_zero(tmp_path: Path) -> None:
    write_rows(tmp_path, ("bbo", "hyperliquid", DATE, 10), [bbo(1)])
    directory = partition_dir(tmp_path, "bbo", DATE)
    write_atomic(rows_to_table("bbo", [bbo(2)], "recorder"), directory / "hyperliquid.parquet")
    with pytest.raises(RuntimeError, match="cannot read the hour"):
        compact_day(tmp_path, DATE)
    assert len(list(directory.glob("*.parquet"))) == 2


def test_a_short_output_keeps_every_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The row count is the only thing standing between compaction and data loss."""

    class Lossy(pq.ParquetWriter):  # type: ignore[misc]
        def write_table(self, table: pa.Table, **kwargs: object) -> None:
            super().write_table(table.slice(0, 1))

    for hour in (10, 11):
        write_rows(tmp_path, ("bbo", "hyperliquid", DATE, hour), [bbo(hour), bbo(hour + 1)])
    monkeypatch.setattr(pq, "ParquetWriter", Lossy)

    with pytest.raises(RuntimeError, match="expected 4"):
        compact_day(tmp_path, DATE)
    directory = partition_dir(tmp_path, "bbo", DATE)
    assert sorted(p.name for p in directory.glob("*.parquet")) == [
        "hyperliquid_10.parquet",
        "hyperliquid_11.parquet",
    ]
    assert not list(directory.glob(".*"))


def test_compaction_can_be_restricted_to_some_tables(tmp_path: Path) -> None:
    write_rows(tmp_path, ("bbo", "hyperliquid", DATE, 10), [bbo(1)])
    write_rows(tmp_path, ("liquidations", "bybit", DATE, 10), [liq(1)])
    assert compact_day(tmp_path, DATE, tables=["bbo"]) == {"bbo": 1}
    assert sorted(p.name for p in partition_dir(tmp_path, "liquidations", DATE).glob("*")) == [
        "bybit_10.parquet"
    ]


def test_a_table_with_no_canonical_schema_is_skipped_not_attempted(tmp_path: Path) -> None:
    """`liqfade record --compact-only` used to raise on any of the twelve Tardis days.

    The Tardis side tables keep Tardis's own columns by design, so they have no schema to
    write and no `ts_ms` to sort on, and the job already writes them one file per venue
    per day.
    """
    import pyarrow as pa

    write_rows(tmp_path, ("bbo", "hyperliquid", DATE, 10), [bbo(1)])
    side = partition_dir(tmp_path, "tardis_book_snapshot_5", DATE)
    side.mkdir(parents=True)
    write_atomic(pa.table({"whatever_tardis_called_it": [1, 2]}), side / "binance_BTC_00.parquet")
    write_atomic(pa.table({"whatever_tardis_called_it": [3]}), side / "bybit_BTC_00.parquet")

    assert compact_day(tmp_path, DATE) == {"bbo": 1}
    assert len(list(side.glob("*.parquet"))) == 2


def test_receive_time_partitioning_may_straddle_an_hour_by_the_feed_latency(
    tmp_path: Path,
) -> None:
    """The recorder files by rx_ms and compaction sorts by ts_ms, so boundaries overlap."""
    hour_11 = 11 * 3_600_000
    write_rows(tmp_path, ("bbo", "hyperliquid", DATE, 10), [bbo(hour_11 - 5), bbo(hour_11 - 1)])
    # Sent at 10:59:59.998, arrived at 11:00:00.x, so it is in hour 11's file.
    write_rows(tmp_path, ("bbo", "hyperliquid", DATE, 11), [bbo(hour_11 - 2), bbo(hour_11 + 9)])
    assert compact_day(tmp_path, DATE) == {"bbo": 4}


def test_an_hour_that_holds_the_wrong_hours_rows_is_refused(tmp_path: Path) -> None:
    write_rows(tmp_path, ("bbo", "hyperliquid", DATE, 10), [bbo(10 * 3_600_000)])
    directory = partition_dir(tmp_path, "bbo", DATE)
    # Named hour 11, but the rows are from hour 3: a mis-parsed name, not feed latency.
    write_atomic(
        rows_to_table("bbo", [bbo(3 * 3_600_000)], "recorder"), directory / "hyperliquid_11.parquet"
    )
    with pytest.raises(RuntimeError, match="refusing to write a misordered file"):
        compact_day(tmp_path, DATE)
    assert len(list(directory.glob("*.parquet"))) == 2


def test_macos_sidecar_files_are_not_lake_data(tmp_path: Path) -> None:
    """Copying onto the owner's external drive leaves a `._x.parquet` beside every file."""
    write_rows(tmp_path, ("bbo", "hyperliquid", DATE, 10), [bbo(1)])
    directory = partition_dir(tmp_path, "bbo", DATE)
    (directory / "._hyperliquid_10.parquet").write_bytes(b"\x00\x05\x16\x07not parquet")

    assert compact_day(tmp_path, DATE) == {"bbo": 1}
    assert (directory / "hyperliquid_day.parquet").exists()
    assert not (directory / "hyperliquid_10.parquet").exists()
    # Untouched: it is not ours to delete, and it is not data.
    assert (directory / "._hyperliquid_10.parquet").exists()
    # And a second run still sees a compacted day.
    assert compact_day(tmp_path, DATE) == {}
