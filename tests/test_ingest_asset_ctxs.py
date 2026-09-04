"""The daily asset-context archive: parsing, the partition it writes, and resume.

A hand-built two-coin, three-minute CSV through the same fake S3 the other jobs use, so
nothing here touches the network and nothing spends.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import lz4.frame
import pyarrow.parquet as pq
import pytest

from liqfade.data.ingest import hl_asset_ctxs
from liqfade.data.ingest.common import BackfillError, Manifest
from liqfade.data.schemas import SOURCE_HL_ARCHIVE, validate_table
from tests.conftest import FakeS3

DAY = date(2026, 3, 1)
KEY = "asset_ctxs/20260301.csv.lz4"
# 2026-03-01T00:00:00Z. One row per coin per minute, as the archive writes it.
MINUTE_MS = 1_772_323_200_000

HEADER = (
    "time,coin,funding,open_interest,prev_day_px,day_ntl_vlm,premium,"
    "oracle_px,mark_px,mid_px,impact_bid_px,impact_ask_px"
)
ROWS = [
    "2026-03-01T00:00:00,BTC,0.0000125,37332.4,77661.0,1224472984.79,-0.00024,78882.6,78862.0,78862.5,78862.0,78863.0",
    "2026-03-01T00:00:00,ETH,0.0000100,912344.2,3011.5,411244984.10,0.00011,3050.1,3050.4,3050.2,3050.1,3050.6",
    "2026-03-01T00:01:00,BTC,0.0000126,37340.1,77661.0,1224999984.79,-0.00021,78890.2,78871.0,78870.5,78870.0,78871.5",
]


def csv_lz4(rows: list[str] | None = None, header: str = HEADER) -> bytes:
    body = "\n".join([header, *(ROWS if rows is None else rows)]) + "\n"
    return lz4.frame.compress(body.encode())


@pytest.fixture
def client() -> FakeS3:
    return FakeS3({KEY: csv_lz4()})


def local_file(tmp_path: Path, payload: bytes) -> Path:
    path = tmp_path / "20260301.csv.lz4"
    path.write_bytes(payload)
    return path


# --- parsing ------------------------------------------------------------------


def test_the_key_is_one_file_per_utc_day() -> None:
    assert hl_asset_ctxs.object_key(DAY) == KEY
    assert hl_asset_ctxs.day_of_key(KEY) == "2026-03-01"


def test_a_day_parses_to_canonical_rows(tmp_path: Path) -> None:
    table = hl_asset_ctxs.parse_day(local_file(tmp_path, csv_lz4()))
    validate_table("asset_ctx", table)
    assert table.num_rows == 3
    assert table.column("source").to_pylist() == [SOURCE_HL_ARCHIVE] * 3
    assert table.column("rx_ms").to_pylist() == [0, 0, 0]

    first = table.slice(0, 1).to_pylist()[0]
    assert first["coin"] == "BTC"
    assert first["ts_ms"] == MINUTE_MS
    assert first["mark_px"] == 78862.0
    assert first["open_interest"] == 37332.4
    assert (first["impact_bid_px"], first["impact_ask_px"]) == (78862.0, 78863.0)


def test_rows_come_out_sorted_by_time(tmp_path: Path) -> None:
    """One file per day, sorted, so `liqfade compact` never has to touch the partition."""
    table = hl_asset_ctxs.parse_day(local_file(tmp_path, csv_lz4(list(reversed(ROWS)))))
    assert table.column("ts_ms").to_pylist() == sorted(table.column("ts_ms").to_pylist())


def test_prev_day_px_is_dropped_and_said_so(tmp_path: Path) -> None:
    """The one archive column the canonical schema has no home for."""
    table = hl_asset_ctxs.parse_day(local_file(tmp_path, csv_lz4()))
    assert "prev_day_px" not in table.schema.names
    assert hl_asset_ctxs.DROPPED_COLUMNS == ("prev_day_px",)


def test_a_column_the_archive_lacks_becomes_null_not_zero(tmp_path: Path) -> None:
    """The oldest days predate the impact prices; a zero spread would look like a fill."""
    header = HEADER.replace(",impact_bid_px,impact_ask_px", "")
    rows = [",".join(row.split(",")[:-2]) for row in ROWS]
    table = hl_asset_ctxs.parse_day(local_file(tmp_path, csv_lz4(rows, header=header)))
    assert table.column("impact_bid_px").to_pylist() == [None] * 3


def test_a_file_with_no_time_column_is_an_error_that_names_the_columns(tmp_path: Path) -> None:
    header = HEADER.replace("time,", "moment,")
    with pytest.raises(BackfillError, match="no time column"):
        hl_asset_ctxs.parse_day(local_file(tmp_path, csv_lz4(header=header)))


# --- the job ------------------------------------------------------------------


def test_a_pull_writes_one_day_file_the_compaction_leaves_alone(
    client: FakeS3, tmp_path: Path
) -> None:
    assert hl_asset_ctxs.run(client, tmp_path, DAY, DAY) == 0

    directory = tmp_path / "parquet" / "asset_ctx" / "date=2026-03-01"
    assert [path.name for path in directory.iterdir()] == [hl_asset_ctxs.OUTPUT_NAME]
    assert pq.read_metadata(directory / hl_asset_ctxs.OUTPUT_NAME).num_rows == 3

    from liqfade.recorder.writer import compact_day

    assert compact_day(tmp_path / "parquet", "2026-03-01") == {}


def test_the_estimate_prints_before_anything_downloads(
    client: FakeS3, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert hl_asset_ctxs.run(client, tmp_path, DAY, DAY, estimate_only=True) == 0
    assert "1 objects" in capsys.readouterr().out
    assert client.downloaded == []


def test_a_pull_over_the_gate_refuses_without_yes(tmp_path: Path) -> None:
    big = FakeS3({KEY: csv_lz4()})
    big.list_objects_v2 = lambda **kw: {  # type: ignore[method-assign]
        "Contents": [{"Key": KEY, "Size": 100 * 1024**3}],
        "IsTruncated": False,
    }
    with pytest.raises(BackfillError, match=r"over the \$5.00 gate"):
        hl_asset_ctxs.run(big, tmp_path, DAY, DAY)
    assert big.downloaded == []


def test_a_second_run_skips_the_day_already_in_the_manifest(client: FakeS3, tmp_path: Path) -> None:
    hl_asset_ctxs.run(client, tmp_path, DAY, DAY)
    hl_asset_ctxs.run(client, tmp_path, DAY, DAY)
    assert client.downloaded == [KEY]
    assert Manifest.load(tmp_path, hl_asset_ctxs.JOB).total_rows() == {"asset_ctx": 3}


def test_a_day_the_archive_does_not_have_is_reported_not_guessed(
    client: FakeS3, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """It starts 2023-05-20 and lags two days, so a range can legitimately overshoot."""
    assert hl_asset_ctxs.run(client, tmp_path, DAY, date(2026, 3, 3)) == 0
    out = capsys.readouterr().out
    assert "2 of 3 days are not in the archive" in out
    assert client.downloaded == [KEY]


def test_the_raw_file_is_deleted_unless_keep_raw(client: FakeS3, tmp_path: Path) -> None:
    hl_asset_ctxs.run(client, tmp_path, DAY, DAY)
    assert not (tmp_path / "raw").exists()

    other = tmp_path / "kept"
    hl_asset_ctxs.run(FakeS3({KEY: csv_lz4()}), other, DAY, DAY, keep_raw=True)
    assert (other / "raw" / hl_asset_ctxs.JOB / KEY).exists()
