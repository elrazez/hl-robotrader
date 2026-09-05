"""The S3 fills job: envelope parsing, dedup, the side rule, and the pay-once archive.

Everything runs against the committed LZ4 fixture and a fake S3 client. No network, no
credentials, no bytes bought.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import orjson
import pyarrow.parquet as pq
import pytest

from liqfade.data.ingest import common, hl_s3
from liqfade.data.ingest.common import BackfillError, Manifest
from liqfade.data.schemas import SOURCE_HL_S3, validate_table
from tests.conftest import FakeS3

FIXTURES = Path(__file__).parent / "fixtures" / "ingest"
SAMPLE_LZ4 = FIXTURES / "node_fills_by_block_sample.lz4"
SAMPLE_JSONL = FIXTURES / "node_fills_by_block_sample.jsonl"

VICTIM = "0x1111111111111111111111111111111111111111"
DAY = date(2026, 9, 5)
KEY = "node_fills_by_block/hourly/20260905/11.lz4"


@pytest.fixture
def fills() -> list[dict[str, object]]:
    from liqfade.data.ingest.common import read_lz4_lines

    return list(hl_s3.parse_fills(read_lz4_lines(SAMPLE_LZ4)))


@pytest.fixture
def client() -> FakeS3:
    return FakeS3({KEY: SAMPLE_LZ4.read_bytes()})


# --- envelope parsing ---------------------------------------------------------


def test_the_lz4_fixture_matches_the_readable_jsonl_beside_it() -> None:
    """If these drift, the committed .lz4 was not regenerated from the .jsonl."""
    from liqfade.data.ingest.common import read_lz4_lines

    assert [line.strip() for line in read_lz4_lines(SAMPLE_LZ4)] == [
        line for line in SAMPLE_JSONL.read_bytes().splitlines() if line.strip()
    ]


def test_every_event_of_every_block_becomes_a_row(fills: list[dict[str, object]]) -> None:
    blocks = [orjson.loads(line) for line in SAMPLE_JSONL.read_bytes().splitlines() if line]
    assert len(fills) == sum(len(block["events"]) for block in blocks)


def test_iso_envelope_times_become_integer_milliseconds(fills: list[dict[str, object]]) -> None:
    first = fills[0]
    assert first["block_time"] == 1_788_609_540_000
    assert first["local_time"] == 1_788_609_540_020
    assert first["ts_ms"] == 1_788_609_540_000  # the fill's own epoch-ms `time`


def test_historical_rows_carry_no_receive_time(fills: list[dict[str, object]]) -> None:
    assert {row["rx_ms"] for row in fills} == {0}


def test_the_liquidation_object_is_flattened_and_null_on_ordinary_fills(
    fills: list[dict[str, object]],
) -> None:
    liq = [row for row in fills if row["liq_user"]]
    plain = [row for row in fills if not row["liq_user"]]
    assert len(liq) == 4  # two events, both halves of each
    assert {row["liq_method"] for row in liq} == {"market", "backstop"}
    assert all(row["liq_mark_px"] is None and row["liq_method"] is None for row in plain)


def test_optional_fields_survive_when_present_and_are_null_when_absent(
    fills: list[dict[str, object]],
) -> None:
    tagged = next(row for row in fills if row["cloid"])
    assert tagged["builder"] == "0x5555555555555555555555555555555555555555"
    assert tagged["builder_fee"] == 0.42
    assert tagged["twap_id"] == 77
    untagged = next(row for row in fills if row["tid"] == 111)
    assert (untagged["cloid"], untagged["builder"], untagged["twap_id"]) == (None, None, None)


# --- fills_full is lossless ---------------------------------------------------

# fills_full column -> the source field it came from. Every field of the fill envelope
# is in here, which is what "no second egress bill" rests on.
FIELD_MAP = {
    "coin": "coin",
    "ts_ms": "time",
    "px": "px",
    "sz": "sz",
    "side": "side",
    "start_position": "startPosition",
    "dir": "dir",
    "closed_pnl": "closedPnl",
    "hash": "hash",
    "oid": "oid",
    "crossed": "crossed",
    "fee": "fee",
    "fee_token": "feeToken",
    "tid": "tid",
    "cloid": "cloid",
    "builder": "builder",
    "builder_fee": "builderFee",
    "twap_id": "twapId",
}
NUMERIC = {"px", "sz", "start_position", "closed_pnl", "fee", "builder_fee"}


def _source_events() -> list[tuple[str, dict[str, object], dict[str, object]]]:
    events = []
    for line in SAMPLE_JSONL.read_bytes().splitlines():
        if not line.strip():
            continue
        envelope = orjson.loads(line)
        for user, fill in envelope["events"]:
            events.append((user, fill, envelope))
    return events


def test_fills_full_round_trips_every_field_through_parquet(tmp_path: Path) -> None:
    """Parse, write, read back, compare against the raw JSON. Nothing may be dropped."""
    hl_s3.write_one_file(tmp_path, SAMPLE_LZ4)
    files = sorted(tmp_path.glob("fills_full/date=*/*.parquet"))
    rows = [row for path in files for row in pq.read_table(path, partitioning=None).to_pylist()]
    by_key = {(row["tid"], row["user_address"]): row for row in rows}

    events = _source_events()
    assert len(rows) == len(events)
    for user, fill, envelope in events:
        row = by_key[(fill["tid"], user)]
        for column, field in FIELD_MAP.items():
            expected = fill.get(field)
            if expected is None:
                assert row[column] is None, column
            elif column in NUMERIC:
                assert row[column] == pytest.approx(float(expected)), column
            else:
                assert row[column] == expected, column
        liq = fill.get("liquidation") or {}
        assert row["liq_user"] == liq.get("liquidatedUser")
        assert row["liq_method"] == liq.get("method")
        assert row["user_address"] == user
        assert row["block_number"] == envelope["block_number"]


def test_fills_full_matches_its_canonical_schema(tmp_path: Path) -> None:
    hl_s3.write_one_file(tmp_path, SAMPLE_LZ4)
    for path in sorted(tmp_path.glob("fills_full/date=*/*.parquet")):
        validate_table("fills_full", pq.read_table(path, partitioning=None))


def test_the_archive_keeps_every_coin_with_no_universe_filter(
    fills: list[dict[str, object]],
) -> None:
    assert {row["coin"] for row in fills} == {"BTC", "ETH"}


# --- derivation ---------------------------------------------------------------


def test_a_maker_and_taker_pair_counts_as_one_liquidation(
    fills: list[dict[str, object]],
) -> None:
    """Four liquidation fills, two events. Counting twice doubles every number."""
    assert len([row for row in fills if row["liq_user"]]) == 4
    assert len(hl_s3.derive_liquidations(fills)) == 2


def test_the_forced_party_selling_means_longs_were_liquidated(
    fills: list[dict[str, object]],
) -> None:
    liqs = {row["coin"]: row for row in hl_s3.derive_liquidations(fills)}
    # BTC: the liquidated user's own fill has side "A", so it was forced to sell a long.
    assert liqs["BTC"]["side"] == "long"
    # ETH: side "B", forced to buy back a short.
    assert liqs["ETH"]["side"] == "short"


def test_the_side_comes_from_the_liquidated_user_not_the_counterparty(
    fills: list[dict[str, object]],
) -> None:
    """The HLP side of the same event is the opposite; reading it would invert the rule."""
    counterparty_only = [
        row for row in fills if row["liq_user"] and row["user_address"] != row["liq_user"]
    ]
    assert counterparty_only  # the fixture has them
    btc = next(row for row in counterparty_only if row["coin"] == "BTC")
    assert btc["side"] == "B"  # the buyer, opposite the forced seller
    assert next(r for r in hl_s3.derive_liquidations(fills) if r["coin"] == "BTC")["side"] == "long"


def test_only_the_counterparty_half_still_yields_the_right_side(
    fills: list[dict[str, object]],
) -> None:
    """A truncated file may hold one half. The counterparty's side is inverted to recover."""
    half = [
        row
        for row in fills
        if row["liq_user"] and row["user_address"] != row["liq_user"] and row["coin"] == "BTC"
    ]
    assert hl_s3.derive_liquidations(half)[0]["side"] == "long"


def test_liquidation_rows_carry_notional_method_and_completeness(
    fills: list[dict[str, object]],
) -> None:
    btc = next(row for row in hl_s3.derive_liquidations(fills) if row["coin"] == "BTC")
    assert btc["notional_usd"] == pytest.approx(78400.0 * 2.0)
    assert btc["method"] == "market"
    assert btc["complete"] is True
    assert btc["rx_ms"] == 0
    assert btc["venue"] == "hyperliquid"


def test_trades_dedup_a_maker_and_taker_pair_into_one_row(
    fills: list[dict[str, object]],
) -> None:
    trades = hl_s3.derive_trades(fills)
    assert len(trades) == len({(row["block_time"], row["coin"], row["tid"]) for row in fills})
    assert len(trades) == 5
    assert len({row["tid"] for row in trades}) == 5


def test_the_aggressor_side_comes_from_the_crossed_record(
    fills: list[dict[str, object]],
) -> None:
    trades = {row["tid"]: row for row in hl_s3.derive_trades(fills)}
    assert trades[111]["side"] == "sell"  # taker crossed with side "A"
    assert trades[222]["side"] == "sell"  # the forced seller was the taker
    assert trades[333]["side"] == "buy"


def test_trades_of_one_millisecond_keep_the_blocks_event_order() -> None:
    """`tid` is a hash, so sorting ties by it picks the wrong first and last trade.

    One sweep filling three makers in a millisecond, with tids deliberately out of
    order. The rows must come back in the order the block listed them, because that is
    the only record of the exchange's sequence that `trades` carries.
    """
    sweep = [
        {
            "block_time": 1,
            "coin": "BTC",
            "ts_ms": 1_000,
            "tid": tid,
            "px": px,
            "sz": 1.0,
            "side": "B",
            "crossed": True,
            "user_address": "0xtaker",
        }
        for tid, px in (
            (997_296_749_534_590, 78_784.0),
            (14_159_527_887_985, 78_782.0),
            (653_061_949_558_227, 78_780.0),
        )
    ]
    rows = hl_s3.derive_trades(sweep)
    assert [row["px"] for row in rows] == [78_784.0, 78_782.0, 78_780.0]


def test_a_group_with_only_the_maker_half_inverts_to_the_aggressor_side(
    fills: list[dict[str, object]],
) -> None:
    maker_only = [row for row in fills if row["tid"] == 111 and not row["crossed"]]
    assert maker_only[0]["side"] == "B"
    assert hl_s3.derive_trades(maker_only)[0]["side"] == "sell"


def test_derived_tables_match_their_canonical_schemas(tmp_path: Path) -> None:
    hl_s3.write_one_file(tmp_path, SAMPLE_LZ4)
    for table in ("liquidations", "trades"):
        for path in sorted(tmp_path.glob(f"{table}/date=*/*.parquet")):
            arrow = pq.read_table(path, partitioning=None)
            validate_table(table, arrow)
            assert set(arrow.column("source").to_pylist()) == {SOURCE_HL_S3}


# --- side rule verification ---------------------------------------------------


def test_the_side_rule_check_confirms_the_rule_on_the_fixture(
    fills: list[dict[str, object]],
) -> None:
    check = hl_s3.SideRuleCheck()
    check.add(hl_s3.derive_liquidations(fills), hl_s3.derive_trades(fills))
    result = check.result()
    assert result["verdict"] == "consistent"
    # Price fell into the long liquidation and rose into the short one.
    assert result["means_bps"]["long"] < 0 < result["means_bps"]["short"]
    assert "OK:" in check.report()


def test_the_side_rule_check_shouts_when_the_rule_is_inverted(
    monkeypatch: pytest.MonkeyPatch, fills: list[dict[str, object]]
) -> None:
    monkeypatch.setattr(hl_s3, "LIQUIDATED_ON_SELL", "short")
    monkeypatch.setattr(hl_s3, "LIQUIDATED_ON_BUY", "long")
    check = hl_s3.SideRuleCheck()
    check.add(hl_s3.derive_liquidations(fills), hl_s3.derive_trades(fills))
    assert check.result()["verdict"] == "INVERTED"
    assert "INVERTED" in check.report()


def test_the_check_reads_only_the_columns_it_uses(
    fills: list[dict[str, object]],
) -> None:
    """The parent-side inspection projects these, so it never holds a whole partition."""
    liquidations = [
        {key: row[key] for key in hl_s3.SIDE_CHECK_LIQ_COLUMNS}
        for row in hl_s3.derive_liquidations(fills)
    ]
    trades = [
        {key: row[key] for key in hl_s3.SIDE_CHECK_TRADE_COLUMNS}
        for row in hl_s3.derive_trades(fills)
    ]
    check = hl_s3.SideRuleCheck()
    check.add(liquidations, trades)
    assert check.result()["verdict"] == "consistent"


def test_the_side_rule_check_says_so_when_there_is_nothing_to_check() -> None:
    check = hl_s3.SideRuleCheck()
    assert check.result()["verdict"] == "inconclusive"
    assert "inconclusive" in check.report()


# --- the job ------------------------------------------------------------------


def test_the_key_prefix_is_the_documented_one() -> None:
    assert hl_s3.day_prefix(DAY) == "node_fills_by_block/hourly/20260905/"


def test_estimate_only_prints_a_cost_and_downloads_nothing(
    client: FakeS3, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert hl_s3.run(client, tmp_path, DAY, DAY, estimate_only=True) == 0
    assert "estimated $" in capsys.readouterr().out
    assert client.downloaded == []


def test_a_pull_writes_three_tables_and_records_the_object(client: FakeS3, tmp_path: Path) -> None:
    assert hl_s3.run(client, tmp_path, DAY, DAY) == 0
    for table in hl_s3.TABLES:
        assert list((tmp_path / "parquet" / table).glob("date=*/*.parquet"))
    manifest = Manifest.load(tmp_path, hl_s3.JOB)
    assert manifest.done(KEY)
    assert manifest.total_rows()["liquidations"] == 2


def test_batches_are_bounded_by_bytes_not_line_count() -> None:
    """One fat line fills a batch on its own; small lines are grouped up to the budget.

    Line count is the wrong unit: a block during a cascade carries hundreds of events, so
    a fixed number of lines is megabytes on a quiet hour and gigabytes on 2025-10-10.
    """
    assert list(hl_s3._batches([b"a" * 100], 10)) == [[b"a" * 100]]
    assert list(hl_s3._batches([b"ab", b"cd", b"ef"], 4)) == [[b"ab", b"cd"], [b"ef"]]
    assert list(hl_s3._batches([], 10)) == []


def test_batching_writes_the_same_rows_as_one_pass(tmp_path: Path, monkeypatch) -> None:
    """A batch per line must not change a single row.

    The largest hour in the archive (the 2025-10-10 cascade, 340 MB compressed) cannot
    be held in memory at once, so the parse runs in batches. Batches split on line
    boundaries and a line is one block, so trade pairs stay together and the output has
    to be identical however small the batches are.
    """

    def rows(root: Path, table: str) -> list[dict[str, object]]:
        files = sorted((root / table).glob("date=*/*.parquet"))
        return [row for file in files for row in pq.read_table(file).to_pylist()]

    whole = tmp_path / "whole"
    hl_s3.write_one_file(whole, SAMPLE_LZ4)

    monkeypatch.setattr(hl_s3, "BATCH_BYTES", 1)
    batched = tmp_path / "batched"
    counts = hl_s3.write_one_file(batched, SAMPLE_LZ4)

    for table in hl_s3.TABLES:
        one_pass, in_batches = rows(whole, table), rows(batched, table)
        assert len(one_pass) == len(in_batches) == counts[table]
        assert sorted(map(str, one_pass)) == sorted(map(str, in_batches))


def test_the_raw_file_is_deleted_after_parsing_unless_keep_raw(
    client: FakeS3, tmp_path: Path
) -> None:
    hl_s3.run(client, tmp_path, DAY, DAY)
    assert not list((tmp_path / "raw").rglob("*.lz4"))


def test_keep_raw_leaves_the_download_in_place(client: FakeS3, tmp_path: Path) -> None:
    hl_s3.run(client, tmp_path, DAY, DAY, keep_raw=True)
    assert (tmp_path / "raw" / hl_s3.JOB / KEY).exists()


def test_a_second_run_skips_the_completed_object(
    client: FakeS3, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hl_s3.run(client, tmp_path, DAY, DAY)
    client.downloaded.clear()
    hl_s3.run(client, tmp_path, DAY, DAY)
    assert client.downloaded == []
    assert "already done, skipped" in capsys.readouterr().out


def test_the_disk_guard_stops_the_loop_before_downloading(
    client: FakeS3,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The guard moved into common.parallel_pull when the loop went parallel.
    monkeypatch.setattr(common, "enough_disk", lambda *a, **k: False)
    assert hl_s3.run(client, tmp_path, DAY, DAY) == 0
    assert client.downloaded == []
    assert "free disk is under the floor" in capsys.readouterr().out


def test_a_range_before_the_hourly_prefix_is_flagged_not_silently_dropped(
    client: FakeS3, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hl_s3.run(client, tmp_path, date(2025, 7, 25), DAY, estimate_only=True)
    out = capsys.readouterr().out
    assert "before 2025-07-27" in out
    assert "node_fills and node_trades prefixes" in out


def test_a_range_entirely_before_the_hourly_prefix_is_an_error(
    client: FakeS3, tmp_path: Path
) -> None:
    with pytest.raises(BackfillError, match="no days in range"):
        hl_s3.run(client, tmp_path, date(2025, 1, 1), date(2025, 1, 2))


def test_a_day_with_no_objects_warns(
    client: FakeS3, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hl_s3.run(client, tmp_path, date(2026, 9, 4), DAY, estimate_only=True)
    assert "nothing under s3://" in capsys.readouterr().out


# --- rederive -----------------------------------------------------------------


def _read_day(root: Path, table: str) -> list[dict[str, object]]:
    """Every row of one table's partitions, in file order then row order."""
    from liqfade.recorder.writer import lake_files

    paths = sorted(p for d in sorted((root / table).glob("date=*")) for p in lake_files(d))
    return [row for path in paths for row in pq.read_table(path, partitioning=None).to_pylist()]


def test_rederive_reproduces_the_canonical_tables_from_the_local_archive(
    client: FakeS3, tmp_path: Path
) -> None:
    hl_s3.run(client, tmp_path, DAY, DAY)

    def read(table: str) -> list[dict[str, object]]:
        paths = sorted((tmp_path / "parquet" / table).glob("date=*/*.parquet"))
        rows = [r for p in paths for r in pq.read_table(p, partitioning=None).to_pylist()]
        return sorted(rows, key=lambda row: (row["ts_ms"], row.get("tid") or 0))

    before = {table: read(table) for table in ("liquidations", "trades")}
    assert hl_s3.rederive(tmp_path, DAY, DAY) == 0
    assert {table: read(table) for table in ("liquidations", "trades")} == before


def test_rederive_is_idempotent_and_does_not_duplicate_rows(client: FakeS3, tmp_path: Path) -> None:
    hl_s3.run(client, tmp_path, DAY, DAY)
    counts = []
    for _ in range(2):
        hl_s3.rederive(tmp_path, DAY, DAY)
        paths = sorted((tmp_path / "parquet" / "liquidations").glob("date=*/*.parquet"))
        counts.append(sum(pq.read_metadata(p).num_rows for p in paths))
    assert counts == [2, 2]


def _recorded() -> dict[str, object]:
    """One `liquidations` row from another venue and source, for the day the fixture uses."""
    return {
        "venue": "bybit",
        "coin": "BTC",
        "venue_symbol": "BTCUSDT",
        "ts_ms": 1_788_609_600_000,
        "rx_ms": 1_788_609_600_050,
        "side": "long",
        "px": 78_500.0,
        "sz": 1.0,
        "notional_usd": 78_500.0,
        "method": "unknown",
        "complete": True,
    }


def test_rederive_preserves_rows_from_other_sources(client: FakeS3, tmp_path: Path) -> None:
    """A recorded day and a backfilled day share a partition after compaction."""
    from liqfade.data.ingest.common import write_partitioned

    hl_s3.run(client, tmp_path, DAY, DAY)
    write_partitioned(tmp_path / "parquet", "liquidations", "bybit", "recorder", [_recorded()])

    hl_s3.rederive(tmp_path, DAY, DAY)
    paths = sorted((tmp_path / "parquet" / "liquidations").glob("date=*/*.parquet"))
    rows = [r for p in paths for r in pq.read_table(p, partitioning=None).to_pylist()]
    assert sorted(row["source"] for row in rows) == ["hl_s3", "hl_s3", "recorder"]


def test_rederive_with_no_archive_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert hl_s3.rederive(tmp_path, DAY, DAY) == 0
    assert "no fills_full" in capsys.readouterr().out


def test_a_pull_with_process_workers_matches_the_serial_pull(tmp_path: Path) -> None:
    """workers=2 exercises the spawned-process path end to end, side rule included."""
    from functools import partial

    factory = partial(FakeS3, {KEY: SAMPLE_LZ4.read_bytes()})
    serial_dir = tmp_path / "serial"
    pooled_dir = tmp_path / "pooled"

    assert hl_s3.run(FakeS3({KEY: SAMPLE_LZ4.read_bytes()}), serial_dir, DAY, DAY) == 0
    assert (
        hl_s3.run(
            FakeS3({KEY: SAMPLE_LZ4.read_bytes()}),
            pooled_dir,
            DAY,
            DAY,
            workers=2,
            client_factory=factory,
        )
        == 0
    )

    import pyarrow.parquet as pq

    for table in ("fills_full", "liquidations", "trades"):
        serial_rows = sum(
            pq.read_metadata(p).num_rows
            for p in (serial_dir / "parquet" / table).rglob("*.parquet")
        )
        pooled_rows = sum(
            pq.read_metadata(p).num_rows
            for p in (pooled_dir / "parquet" / table).rglob("*.parquet")
        )
        assert serial_rows == pooled_rows, table


def _trade(**overrides: object) -> dict[str, object]:
    row = {
        "venue": "binance",
        "coin": "BTC",
        "ts_ms": 1_788_609_540_000,
        "rx_ms": 0,
        "px": 78_000.0,
        "sz": 1.0,
        "side": "buy",
        "tid": 9_001,
    }
    return row | overrides


def _write_raw(
    root: Path, table: str, rows: list[dict[str, object]], *, source: str | None
) -> None:
    """One partition file with `source` written verbatim, nulls included.

    `write_partitioned` stamps a literal, so it cannot produce the null this exercises.
    """
    import pyarrow as pa

    from liqfade.data.schemas import schema_for
    from liqfade.recorder.writer import partition_dir, write_atomic

    schema = schema_for(table)
    columns = [
        pa.array([source] * len(rows), pa.string())
        if field.name == "source"
        else pa.array([row[field.name] for row in rows], field.type)
        for field in schema
    ]
    directory = partition_dir(root, table, DAY.isoformat())
    directory.mkdir(parents=True, exist_ok=True)
    write_atomic(pa.Table.from_arrays(columns, schema=schema), directory / "other_23.parquet")


def test_rederive_drops_every_hyperliquid_row_that_is_not_ours(
    client: FakeS3, tmp_path: Path
) -> None:
    """`hl_s3` is the one source of Hyperliquid trades, and this day has `fills_full`.

    So our derived rows are the day's complete Hyperliquid set and any other Hyperliquid
    row is a second copy: Tardis's on the twelve free days, under the same `tid`, and the
    recorder's from the day it starts. A surviving one would sit in `kept`, which is
    concatenated ahead of ours, and sort first inside a shared millisecond. Binance,
    Bybit and OKX rows have no other source and stay.
    """
    from liqfade.data.ingest.common import write_partitioned

    hl_s3.run(client, tmp_path, DAY, DAY)
    root = tmp_path / "parquet"
    write_partitioned(root, "trades", "hyperliquid", "tardis", [_trade(venue="hyperliquid")])
    write_partitioned(root, "trades", "hyperliquid", "recorder", [_trade(venue="hyperliquid")])
    write_partitioned(root, "trades", "binance", "tardis", [_trade()])
    # A foreign row with no `source`. The rule is about the venue, so this one stays:
    # the filter used to AND in `source != hl_s3`, and a null compares to neither.
    _write_raw(root, "trades", [_trade(coin="ETH", tid=9_002)], source=None)

    hl_s3.rederive(tmp_path, DAY, DAY, workers=1)

    rows = _read_day(root, "trades")
    assert {(row["venue"], row["source"]) for row in rows} == {
        ("hyperliquid", SOURCE_HL_S3),
        ("binance", "tardis"),
        ("binance", None),
    }


def test_rederive_writes_one_file_per_day_under_the_compacted_name(
    client: FakeS3, tmp_path: Path
) -> None:
    """`liqfade compact` skips a partition that is already a single `_day` file."""
    from liqfade.recorder.writer import lake_files, partition_dir

    hl_s3.run(client, tmp_path, DAY, DAY)
    hl_s3.rederive(tmp_path, DAY, DAY, workers=1)

    for table in ("trades", "liquidations"):
        files = lake_files(partition_dir(tmp_path / "parquet", table, DAY.isoformat()))
        assert [path.name for path in files] == [hl_s3.REDERIVE_OUTPUT]


def test_a_second_rederive_skips_the_day_and_a_forced_one_does_not(
    client: FakeS3, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The resume rule: the run is detached behind a retry loop, so a restart is cheap."""
    hl_s3.run(client, tmp_path, DAY, DAY)
    assert hl_s3.rederive_day(str(tmp_path), DAY.isoformat(), False)[0] == "done"
    assert hl_s3.rederive_day(str(tmp_path), DAY.isoformat(), False)[0] == "skipped"
    assert hl_s3.rederive_day(str(tmp_path), DAY.isoformat(), True)[0] == "done"
    capsys.readouterr()


def test_a_short_output_leaves_every_input_in_place(
    client: FakeS3, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same check `compact_day` has: a short file must never replace its inputs."""
    hl_s3.run(client, tmp_path, DAY, DAY)
    directory = tmp_path / "parquet" / "trades" / f"date={DAY.isoformat()}"
    before = sorted(p.name for p in directory.glob("*.parquet"))
    real = pq.write_table

    def writes_one_row_short(table: object, where: object, **kwargs: object) -> None:
        real(table.slice(1), where, **kwargs)  # type: ignore[attr-defined, arg-type]

    monkeypatch.setattr(pq, "write_table", writes_one_row_short)
    with pytest.raises(BackfillError, match="expected"):
        hl_s3.rederive_day(str(tmp_path), DAY.isoformat(), True)

    monkeypatch.undo()
    assert sorted(p.name for p in directory.glob("*.parquet")) == before
    assert not (directory / hl_s3.PENDING_OUTPUT).exists()


def test_a_crash_between_the_write_and_the_unlink_does_not_double_the_kept_rows(
    client: FakeS3, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window that used to leave `all_day.parquet` and the files it was built from.

    A rerun then kept the old files' foreign rows and the copy of them already inside the
    new file. One `unlink` failing on this mount is enough; it times out an open every
    few hours.
    """
    from liqfade.data.ingest.common import write_partitioned

    hl_s3.run(client, tmp_path, DAY, DAY)
    write_partitioned(tmp_path / "parquet", "liquidations", "bybit", "recorder", [_recorded()])
    directory = tmp_path / "parquet" / "liquidations" / f"date={DAY.isoformat()}"
    real_unlink = Path.unlink

    def fails_on_the_second(self: Path, **kwargs: object) -> None:
        if self.parent == directory and getattr(fails_on_the_second, "seen", 0):
            raise OSError(60, "Operation timed out")
        fails_on_the_second.seen = 1  # type: ignore[attr-defined]
        real_unlink(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", fails_on_the_second)
    with pytest.raises(OSError, match="timed out"):
        hl_s3.rederive_day(str(tmp_path), DAY.isoformat(), True)
    monkeypatch.undo()

    # The finished temp is still there and holds the whole partition.
    assert (directory / hl_s3.PENDING_OUTPUT).exists()
    assert hl_s3.rederive_day(str(tmp_path), DAY.isoformat(), True)[0] == "done"

    rows = _read_day(tmp_path / "parquet", "liquidations")
    assert sorted(row["source"] for row in rows) == ["hl_s3", "hl_s3", "recorder"]
    assert [p.name for p in directory.iterdir()] == [hl_s3.REDERIVE_OUTPUT]


def test_a_day_the_compaction_folds_mid_read_is_listed_again_and_redone(
    client: FakeS3, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one race with the detached `fills_full` compaction, which runs alongside this.

    It reads a day's hourly files, writes one compacted file and deletes the inputs. A
    rederive worker that had already listed those inputs then fails to open one; the day
    is listed again and redone from what is there now.
    """
    from liqfade.data.derive import from_fills
    from liqfade.recorder.writer import compact_day

    hl_s3.run(client, tmp_path, DAY, DAY)
    root = tmp_path / "parquet"
    before = _read_day(root, "trades")
    seen: list[list[Path]] = []
    real = from_fills.read_fills

    def folds_the_day(files: list[Path]) -> object:
        seen.append(list(files))
        if len(seen) == 1:
            compact_day(root, DAY.isoformat(), tables=["fills_full"])
            raise FileNotFoundError(files[0])
        return real(files)

    monkeypatch.setattr(from_fills, "read_fills", folds_the_day)
    status, _, _seconds = hl_s3.rederive_day(str(tmp_path), DAY.isoformat(), True)

    assert status == "done"
    assert len(seen) == 2 and len(seen[1]) == 1  # re-listed, and found the folded file
    assert _read_day(root, "trades") == before


def test_a_read_failure_that_is_not_a_vanished_file_is_raised(
    client: FakeS3, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mount times out an open every few hours; that is a restart, not a re-list."""
    from liqfade.data.derive import from_fills

    hl_s3.run(client, tmp_path, DAY, DAY)

    def times_out(files: list[Path]) -> object:
        raise OSError("Input/output error")

    monkeypatch.setattr(from_fills, "read_fills", times_out)
    # BackfillError, not the OSError itself: this runs in a pool worker and a polars
    # PanicException cannot be pickled back to the parent, so the cause is carried in
    # the message instead of the type.
    with pytest.raises(BackfillError, match="Input/output error"):
        hl_s3.rederive_day(str(tmp_path), DAY.isoformat(), True)


def test_a_partition_compacted_but_not_rederived_is_not_mistaken_for_current(
    client: FakeS3, tmp_path: Path
) -> None:
    """`liqfade compact` already wrote `all_day.parquet` for every day of the year.

    Out of the wrongly ordered rows this job exists to replace, and with a newer mtime
    than the `fills_full` files they were derived from. So neither the file name nor the
    timestamps can tell a compacted day from a rederived one; only the stamp this job
    puts in the Parquet footer can.
    """
    from liqfade.recorder.writer import compact_day

    hl_s3.run(client, tmp_path, DAY, DAY)
    compact_day(tmp_path / "parquet", DAY.isoformat(), tables=["trades", "liquidations"])

    assert hl_s3.rederive_day(str(tmp_path), DAY.isoformat(), False)[0] == "done"
    assert hl_s3.rederive_day(str(tmp_path), DAY.isoformat(), False)[0] == "skipped"


def test_a_polars_panic_is_treated_like_any_other_bad_read(
    client: FakeS3, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """polars raises `PanicException`, which derives from BaseException, not Exception.

    So it has to be named: a blanket handler never sees it, and the year run hit one
    (`Invalid argument (os error 22)`) within an hour of starting. What decides the
    response is still the listing, not the exception type.
    """
    import polars as pl

    from liqfade.data.derive import from_fills

    hl_s3.run(client, tmp_path, DAY, DAY)
    root = tmp_path / "parquet"
    fills_dir = root / "fills_full" / f"date={DAY.isoformat()}"
    seen: list[list[Path]] = []
    real = from_fills.read_fills

    def panics_once(files: list[Path]) -> object:
        seen.append(list(files))
        if len(seen) == 1:
            (fills_dir / "hyperliquid_99_gone.parquet").write_bytes(b"")
            raise pl.exceptions.PanicException("Invalid argument (os error 22)")
        return real([path for path in files if path.stat().st_size])

    monkeypatch.setattr(from_fills, "read_fills", panics_once)
    assert hl_s3.rederive_day(str(tmp_path), DAY.isoformat(), True)[0] == "done"
    assert len(seen) == 2


def test_a_day_that_finished_during_shutdown_is_still_reported() -> None:
    """`cancel_futures` cannot recall a day already handed to the pool's call queue.

    Such a day runs to completion while the consuming loop has already stopped, and its
    work is in the lake and stamped. `unconsumed` is what stops its row counts going
    unreported: the first year run was 26 days short in its own summary for exactly this
    reason, over a complete lake. Cancelled days, failed days and days already taken are
    not reported again.
    """
    import concurrent.futures

    def future(**outcome: object) -> concurrent.futures.Future[object]:
        made: concurrent.futures.Future[object] = concurrent.futures.Future()
        if "result" in outcome:
            made.set_running_or_notify_cancel()
            made.set_result(outcome["result"])
        elif "exception" in outcome:
            made.set_running_or_notify_cancel()
            made.set_exception(outcome["exception"])  # type: ignore[arg-type]
        else:
            made.cancel()
        return made

    taken = future(result=("done", {}, 1.0))
    finished_late = future(result=("done", {"trades": 7}, 2.0))
    started = {
        taken: "2026-03-01",
        finished_late: "2026-03-02",
        future(exception=OSError("mount")): "2026-03-03",
        future(): "2026-03-04",
        concurrent.futures.Future(): "2026-03-05",  # never started
    }

    left = hl_s3.unconsumed(started, seen={"2026-03-01"})
    assert [stamp for stamp, _ in left] == ["2026-03-02"]
    assert left[0][1].result() == ("done", {"trades": 7}, 2.0)
