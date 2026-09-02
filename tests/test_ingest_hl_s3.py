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


def test_rederive_preserves_rows_from_other_sources(client: FakeS3, tmp_path: Path) -> None:
    """A recorded day and a backfilled day share a partition after compaction."""
    from liqfade.data.ingest.common import write_partitioned

    hl_s3.run(client, tmp_path, DAY, DAY)
    recorded = {
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
    write_partitioned(tmp_path / "parquet", "liquidations", "bybit", "recorder", [recorded])

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
