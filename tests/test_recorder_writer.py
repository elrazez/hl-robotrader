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


def position(ts_ms: int, address: str = "0xa", coin: str = "BTC") -> dict[str, object]:
    return {
        "coin": coin,
        "ts_ms": ts_ms,
        "rx_ms": ts_ms + 3,
        "address": address,
        "szi": 1.5,
        "entry_px": 60000.0,
        "liquidation_px": None,
        "position_value_usd": 90000.0,
        "margin_used_usd": 9000.0,
        "leverage": 10.0,
        "leverage_type": "cross",
        "max_leverage": 40,
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


def trade(ts_ms: int, tid: int, px: float = 60000.0, coin: str = "BTC") -> dict[str, object]:
    return {
        "venue": "hyperliquid",
        "coin": coin,
        "ts_ms": ts_ms,
        "rx_ms": ts_ms + 3,
        "px": px,
        "sz": 0.5,
        "side": "buy",
        "tid": tid,
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


def test_a_file_name_that_carries_no_hour_is_folded_like_any_other(tmp_path: Path) -> None:
    """Order comes from ts_ms now, so nothing downstream reads the hour off a name."""
    write_rows(tmp_path, ("bbo", "hyperliquid", DATE, 10), [bbo(2)])
    directory = partition_dir(tmp_path, "bbo", DATE)
    write_atomic(rows_to_table("bbo", [bbo(1)], "recorder"), directory / "hyperliquid.parquet")

    assert compact_day(tmp_path, DATE) == {"bbo": 2}
    assert read(directory / "hyperliquid_day.parquet").column("ts_ms").to_pylist() == [1, 2]


def test_a_compacted_file_beside_loose_ones_is_refused(tmp_path: Path) -> None:
    """Compaction does not dedupe, and a day file is already a day's fold, so folding it
    again with files that may hold the same rows writes them twice. `liqfade derive
    candles` leaves exactly this. `_hour_of` used to refuse it by accident."""
    write_rows(tmp_path, ("bbo", "hyperliquid", DATE, 10), [bbo(1)])
    directory = partition_dir(tmp_path, "bbo", DATE)
    write_atomic(rows_to_table("bbo", [bbo(1)], "recorder"), directory / "hyperliquid_day.parquet")

    with pytest.raises(RuntimeError, match="compacted day file beside"):
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


def test_a_late_delivery_of_any_length_merges_instead_of_refusing(tmp_path: Path) -> None:
    """The real 2026-09-05 shape on the box, which compact_day refused.

    Hyperliquid replayed DOGE trades up to 356s old after a reconnect at 22:42 UTC, so
    hour 23's file starts at ts 22:57:46, 132,942 ms before the end of hour 22. Under
    ORDER_TOLERANCE_MS that was `compaction_failed` and the day's 14 hourly files stayed
    on disk. Late delivery is a fact about the feed, not a corrupt file: the rows are
    real, they are simply out of order across files, and a merge on ts_ms orders them.
    """
    hour_22, hour_23 = 22 * 3_600_000, 23 * 3_600_000
    write_rows(tmp_path, ("bbo", "hyperliquid", DATE, 22), [bbo(hour_22), bbo(hour_23 - 1)])
    # Sent at 22:57:46, delivered in hour 23. 132,942 ms behind the previous hour's end.
    late = hour_23 - 1 - 132_942
    write_rows(tmp_path, ("bbo", "hyperliquid", DATE, 23), [bbo(late), bbo(hour_23 + 5)])

    assert compact_day(tmp_path, DATE) == {"bbo": 4}
    directory = partition_dir(tmp_path, "bbo", DATE)
    stamps = read(directory / "hyperliquid_day.parquet").column("ts_ms").to_pylist()
    assert stamps == sorted(stamps)
    assert stamps == [hour_22, late, hour_23 - 1, hour_23 + 5]


def test_a_day_of_hourly_files_merges_in_exchange_time_across_every_one(tmp_path: Path) -> None:
    """Interleave 24 hours so no file's rows are contiguous: a merge orders them, and
    writing the files one after another in hour order could not."""
    for hour in range(24):
        write_rows(
            tmp_path,
            ("bbo", "hyperliquid", DATE, hour),
            [bbo(stamp) for stamp in range(hour, 2_400, 24)],
        )

    assert compact_day(tmp_path, DATE) == {"bbo": 2_400}
    directory = partition_dir(tmp_path, "bbo", DATE)
    assert read(directory / "hyperliquid_day.parquet").column("ts_ms").to_pylist() == list(
        range(2_400)
    )


def test_a_file_whose_own_rows_cannot_be_ordered_is_the_only_refusal_left(
    tmp_path: Path,
) -> None:
    """A null ts_ms has no place in a file that claims to be sorted by it, and no honest
    place to put it. The hourly files stay so a human can look at the one that is wrong."""
    write_rows(tmp_path, ("bbo", "hyperliquid", DATE, 10), [bbo(1)])
    directory = partition_dir(tmp_path, "bbo", DATE)
    broken = rows_to_table("bbo", [bbo(2)], "recorder")
    broken = broken.set_column(
        broken.schema.get_field_index("ts_ms"),
        broken.schema.field("ts_ms"),
        pa.array([None], type=pa.int64()),
    )
    write_atomic(broken, directory / "hyperliquid_11.parquet")

    with pytest.raises(RuntimeError, match="ts_ms is null"):
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


def test_a_short_write_never_replaces_the_file_it_would_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mount has truncated a Parquet mid-write before; the footer check catches it.

    Every lake writer outside `compact_day` and `hl_s3._assemble` goes through
    `write_atomic`, so the guard lives there rather than in each of them.
    """
    from liqfade.recorder import writer

    path = partition_dir(tmp_path, "bbo", DATE) / "hyperliquid_10.parquet"
    path.parent.mkdir(parents=True)
    good = rows_to_table("bbo", [bbo(1), bbo(2)], "recorder")
    write_atomic(good, path)

    real = writer.pq.write_table

    def short(table: pa.Table, where: object, **kwargs: object) -> None:
        real(table.slice(0, table.num_rows - 1), where, **kwargs)

    monkeypatch.setattr(writer.pq, "write_table", short)
    with pytest.raises(RuntimeError, match="wrote 1 rows, expected 2"):
        write_atomic(good, path)

    assert read(path).num_rows == 2
    assert not list(path.parent.glob(".*.tmp"))


# --- dedupe ---------------------------------------------------------------------


def test_compaction_drops_a_trade_the_recorder_wrote_twice(tmp_path: Path) -> None:
    """2026-09-05 on the box carries 2,336 duplicate (coin, tid) trades, written by the
    parser bug that has since been fixed. Compaction is the only pass that sees a whole
    day at once, so it is where they come out."""
    write_rows(tmp_path, ("trades", "hyperliquid", DATE, 10), [trade(1, 100), trade(2, 101)])
    write_rows(tmp_path, ("trades", "hyperliquid", DATE, 11), [trade(2, 101), trade(3, 102)])

    assert compact_day(tmp_path, DATE) == {"trades": 3}
    table = read(partition_dir(tmp_path, "trades", DATE) / "all_day.parquet")
    assert table.column("tid").to_pylist() == [100, 101, 102]


def test_the_first_of_the_duplicates_is_the_one_kept(tmp_path: Path) -> None:
    """Deterministic: the merge is by ts_ms and ties go to the earlier file name, so two
    runs over the same lake produce the same file."""
    write_rows(tmp_path, ("trades", "hyperliquid", DATE, 10), [trade(5, 100, px=1.0)])
    write_rows(tmp_path, ("trades", "hyperliquid", DATE, 11), [trade(5, 100, px=2.0)])

    assert compact_day(tmp_path, DATE) == {"trades": 1}
    table = read(partition_dir(tmp_path, "trades", DATE) / "all_day.parquet")
    assert table.column("px").to_pylist() == [1.0]


def test_the_same_tid_on_two_coins_is_two_trades(tmp_path: Path) -> None:
    """The key is the coin with the tid, never the tid alone."""
    write_rows(
        tmp_path,
        ("trades", "hyperliquid", DATE, 10),
        [trade(1, 100, coin="BTC"), trade(1, 100, coin="ETH")],
    )
    assert compact_day(tmp_path, DATE) == {"trades": 2}


def test_a_table_with_no_tid_is_never_deduped(tmp_path: Path) -> None:
    """Two bbo rows of the same millisecond are two real quotes: nothing identifies them
    apart, so dropping either would be inventing a fact about the book."""
    write_rows(tmp_path, ("bbo", "hyperliquid", DATE, 10), [bbo(1), bbo(1)])
    assert compact_day(tmp_path, DATE) == {"bbo": 2}


def test_an_already_compacted_day_can_be_folded_again_to_dedupe_it(tmp_path: Path) -> None:
    """The duplicates on the box are already inside day files, so there has to be a way to
    run compaction over one. Without `force` a lone day file is a no-op, which is what
    every nightly run wants."""
    write_rows(tmp_path, ("trades", "hyperliquid", DATE, 10), [trade(1, 100), trade(1, 100)])
    day = partition_dir(tmp_path, "trades", DATE) / "all_day.parquet"
    write_atomic(rows_to_table("trades", [trade(1, 100), trade(2, 101)], "recorder"), day)
    for path in partition_dir(tmp_path, "trades", DATE).glob("*.parquet"):
        if path != day:
            path.unlink()

    assert compact_day(tmp_path, DATE) == {}, "a lone day file is left alone by default"
    assert compact_day(tmp_path, DATE, force=True) == {"trades": 2}
    assert read(day).column("tid").to_pylist() == [100, 101]


def test_compaction_drops_a_poll_that_was_flushed_twice(tmp_path: Path) -> None:
    """Two hl_positions rows with the same (address, coin, ts_ms) are one poll written
    twice, never two facts: `ts_ms` is the response's own `time`, so one address cannot
    hold two different positions in the same coin at the same venue millisecond."""
    write_rows(
        tmp_path,
        ("hl_positions", "hyperliquid", DATE, 10),
        [position(1, coin="BTC"), position(1, coin="ETH")],
    )
    write_rows(
        tmp_path,
        ("hl_positions", "hyperliquid", DATE, 11),
        [position(1, coin="ETH"), position(2, coin="ETH"), position(1, address="0xb")],
    )

    assert compact_day(tmp_path, DATE) == {"hl_positions": 4}
    # Single venue, so the day folds under the venue name rather than to all_day.
    table = read(partition_dir(tmp_path, "hl_positions", DATE) / "hyperliquid_day.parquet")
    assert sorted(
        zip(table.column("address").to_pylist(), table.column("coin").to_pylist(), strict=True)
    ) == [
        ("0xa", "BTC"),
        ("0xa", "ETH"),
        ("0xa", "ETH"),
        ("0xb", "BTC"),
    ]
