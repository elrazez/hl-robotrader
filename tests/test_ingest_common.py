"""The money guards. These are the tests that stand between a typo and an AWS bill."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from liqfade.data.ingest.common import (
    COST_GATE_USD,
    GIB,
    BackfillError,
    Estimate,
    Manifest,
    check_cost_gate,
    day_range,
    download_object,
    enough_disk,
    estimate,
    free_gib,
    list_objects,
    parse_day,
    raw_path,
    read_lz4_lines,
    write_partitioned,
)
from tests.conftest import FakeS3

# 100 GiB at $0.09/GiB is $9.00, comfortably over the gate.
BIG = [{"Key": f"k{i}", "Size": 10 * GIB} for i in range(10)]
SMALL = [{"Key": "k", "Size": GIB}]


# --- estimate and the gate ----------------------------------------------------


def test_estimate_sums_bytes_and_prices_them_per_gib() -> None:
    est = estimate(BIG)
    assert est.objects == 10
    assert est.gib == pytest.approx(100.0)
    assert est.usd == pytest.approx(9.0)


def test_the_estimate_line_names_objects_gib_and_dollars() -> None:
    assert estimate(SMALL).line() == "1 objects, 1.00 GiB, estimated $0.09 at $0.09/GB"


def test_egress_is_logged_in_dollars_as_well_as_gib(tmp_path: Path) -> None:
    """The Phase 2 DoD says egress spend is logged, not just printed as bytes."""
    import structlog

    from liqfade.data.ingest.common import Manifest, log_egress

    manifest = Manifest.load(tmp_path, "s3-fills")
    manifest.record("k", 10 * 1024**3, {})

    with structlog.testing.capture_logs() as entries:
        line = log_egress("s3-fills", manifest)

    assert line == "  egress so far: 10.00 GiB, $0.90 at $0.09/GB"
    assert entries == [
        {
            "event": "egress",
            "log_level": "info",
            "job": "s3-fills",
            "bytes": 10 * 1024**3,
            "gib": 10.0,
            "usd": 0.9,
        }
    ]


def test_an_empty_listing_costs_nothing() -> None:
    assert estimate([]) == Estimate(objects=0, bytes=0)
    assert estimate([]).usd == 0.0


def test_a_pull_under_the_gate_needs_no_confirmation() -> None:
    check_cost_gate(estimate(SMALL), yes=False)


def test_a_pull_over_the_gate_refuses_without_yes() -> None:
    with pytest.raises(BackfillError, match=r"over the \$5.00 gate"):
        check_cost_gate(estimate(BIG), yes=False)


def test_a_pull_over_the_gate_proceeds_with_yes() -> None:
    check_cost_gate(estimate(BIG), yes=True)


def test_the_gate_is_inclusive_so_exactly_five_dollars_passes() -> None:
    exact = Estimate(objects=1, bytes=int(COST_GATE_USD / 0.09 * GIB))
    assert exact.usd == pytest.approx(COST_GATE_USD)
    check_cost_gate(exact, yes=False)


# --- dates --------------------------------------------------------------------


def test_days_are_inclusive_and_newest_first_by_default() -> None:
    days = day_range(date(2026, 8, 30), date(2026, 9, 1))
    assert days == [date(2026, 9, 1), date(2026, 8, 31), date(2026, 8, 30)]


def test_a_single_day_range_is_that_day() -> None:
    assert day_range(date(2026, 8, 30), date(2026, 8, 30)) == [date(2026, 8, 30)]


def test_a_backwards_range_is_an_error() -> None:
    with pytest.raises(BackfillError, match="is before start"):
        day_range(date(2026, 9, 1), date(2026, 8, 30))


def test_a_bad_date_names_itself() -> None:
    with pytest.raises(BackfillError, match="2026-13-01"):
        parse_day("2026-13-01")


# --- manifest -----------------------------------------------------------------


def test_a_fresh_manifest_has_nothing_done(tmp_path: Path) -> None:
    manifest = Manifest.load(tmp_path, "s3-fills")
    assert manifest.objects == {}
    assert manifest.pending(["a", "b"]) == ["a", "b"]


def test_a_recorded_key_is_skipped_on_the_next_run(tmp_path: Path) -> None:
    manifest = Manifest.load(tmp_path, "s3-fills")
    manifest.record("a", size=10, rows={"trades": 3})

    resumed = Manifest.load(tmp_path, "s3-fills")
    assert resumed.done("a")
    assert resumed.pending(["a", "b", "c"]) == ["b", "c"]


def test_the_manifest_is_saved_after_every_object_so_a_crash_costs_one(tmp_path: Path) -> None:
    manifest = Manifest.load(tmp_path, "s3-fills")
    manifest.record("a", size=10, rows={"trades": 3})
    # No explicit save(): the point is that record() persisted on its own.
    assert Manifest.load(tmp_path, "s3-fills").done("a")


def test_the_manifest_totals_bytes_and_rows_across_objects(tmp_path: Path) -> None:
    manifest = Manifest.load(tmp_path, "s3-fills")
    manifest.record("a", size=10, rows={"trades": 3, "liquidations": 1})
    manifest.record("b", size=5, rows={"trades": 4})
    assert manifest.total_bytes() == 15
    assert manifest.total_rows() == {"trades": 7, "liquidations": 1}


def test_the_manifest_lands_where_the_money_rules_say(tmp_path: Path) -> None:
    manifest = Manifest.load(tmp_path, "s3-fills")
    manifest.save()
    assert manifest.path == tmp_path / "ingest_state" / "s3-fills.json"
    assert manifest.path.exists()


# --- disk ---------------------------------------------------------------------


def test_free_space_is_read_through_a_path_that_does_not_exist_yet(tmp_path: Path) -> None:
    assert free_gib(tmp_path / "not" / "created" / "yet") > 0


def test_the_disk_guard_stops_when_free_space_is_under_the_floor(tmp_path: Path) -> None:
    assert enough_disk(tmp_path, min_free_gib=0.0)
    assert not enough_disk(tmp_path, min_free_gib=10.0**9)


# --- S3 ---------------------------------------------------------------------


def test_listing_pages_through_every_object_and_sorts_by_key() -> None:
    client = FakeS3({f"p/{i:03d}": b"x" * (i + 1) for i in range(25)}, page_size=10)
    found = list_objects(client, "bucket", "p/")
    assert [obj["Key"] for obj in found] == [f"p/{i:03d}" for i in range(25)]
    assert client.list_calls == 3


def test_listing_a_prefix_with_nothing_under_it_returns_nothing() -> None:
    client = FakeS3({"other/1": b"x"})
    assert list_objects(client, "bucket", "p/") == []


def test_listing_only_returns_the_requested_prefix() -> None:
    client = FakeS3({"a/1": b"x", "b/1": b"yy"})
    assert [obj["Key"] for obj in list_objects(client, "bucket", "a/")] == ["a/1"]


def test_a_download_writes_the_object_and_creates_its_parent(tmp_path: Path) -> None:
    client = FakeS3({"a/b/c.lz4": b"payload"})
    dest = tmp_path / "raw" / "a" / "b" / "c.lz4"
    assert download_object(client, "bucket", "a/b/c.lz4", dest) == dest
    assert dest.read_bytes() == b"payload"


def test_every_s3_call_carries_the_requester_pays_header(tmp_path: Path) -> None:
    """FakeS3 asserts it. Without the header a requester-pays bucket answers 403."""
    client = FakeS3({"a": b"x"})
    list_objects(client, "bucket", "")
    download_object(client, "bucket", "a", tmp_path / "a")


def test_a_raw_path_mirrors_the_s3_key_so_two_days_cannot_collide(tmp_path: Path) -> None:
    first = raw_path(tmp_path, "s3-fills", "node_fills_by_block/hourly/20260901/13.lz4")
    second = raw_path(tmp_path, "s3-fills", "node_fills_by_block/hourly/20260902/13.lz4")
    assert first != second
    assert first.name == "13.lz4"
    assert first.is_relative_to(tmp_path / "raw" / "s3-fills")


# --- lz4 ----------------------------------------------------------------------


def test_lz4_lines_are_streamed_and_blank_lines_dropped(tmp_path: Path) -> None:
    import lz4.frame

    path = tmp_path / "x.lz4"
    path.write_bytes(lz4.frame.compress(b'{"a":1}\n\n{"a":2}\n'))
    assert [line.strip() for line in read_lz4_lines(path)] == [b'{"a":1}', b'{"a":2}']


# --- writing ------------------------------------------------------------------


def _trade(ts_ms: int) -> dict[str, object]:
    return {
        "venue": "hyperliquid",
        "coin": "BTC",
        "ts_ms": ts_ms,
        "rx_ms": 0,
        "px": 1.0,
        "sz": 2.0,
        "side": "buy",
        "tid": ts_ms,
    }


def test_rows_are_split_into_the_utc_hour_partitions_they_belong_to(tmp_path: Path) -> None:
    import pyarrow.parquet as pq

    # 2026-09-01T23:59:59Z and 2026-09-02T00:00:00Z: different day and hour.
    rows = [_trade(1_788_307_199_000), _trade(1_788_307_200_000)]
    assert write_partitioned(tmp_path, "trades", "hyperliquid", "hl_s3", rows) == 2

    files = sorted(tmp_path.glob("trades/date=*/*.parquet"))
    assert [path.parent.name for path in files] == ["date=2026-09-01", "date=2026-09-02"]
    assert [path.name for path in files] == ["hyperliquid_23.parquet", "hyperliquid_00.parquet"]
    for path in files:
        assert pq.read_table(path).column("source").to_pylist() == ["hl_s3"]


def test_writing_no_rows_writes_no_file(tmp_path: Path) -> None:
    assert write_partitioned(tmp_path, "trades", "hyperliquid", "hl_s3", []) == 0
    assert not tmp_path.exists() or not list(tmp_path.glob("**/*.parquet"))
