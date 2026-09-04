"""The lake scanner: counts from Parquet footers, holes flagged, sparkline readable."""

from __future__ import annotations

import importlib.util
from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest

from liqfade.data.ingest.common import write_partitioned

SCRIPT = Path(__file__).parents[1] / "scripts" / "research" / "data_quality.py"


def load() -> object:
    spec = importlib.util.spec_from_file_location("data_quality", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dq = load()


def liq_rows(day: date, count: int) -> list[dict[str, object]]:
    base = int(datetime.combine(day, time.min, tzinfo=UTC).timestamp() * 1000)
    return [
        {
            "venue": "hyperliquid",
            "coin": "BTC",
            "venue_symbol": "BTC",
            "ts_ms": base + index * 1000,
            "rx_ms": 0,
            "side": "long",
            "px": 100.0,
            "sz": 1.0,
            "notional_usd": 100.0,
            "method": "market",
            "complete": True,
        }
        for index in range(count)
    ]


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    root = tmp_path / "parquet"
    # 2026-09-01 and 2026-09-03 have rows; 2026-09-02 has no partition at all.
    write_partitioned(root, "liquidations", "hyperliquid", "hl_s3", liq_rows(date(2026, 9, 1), 3))
    write_partitioned(root, "liquidations", "hyperliquid", "hl_s3", liq_rows(date(2026, 9, 3), 9))
    return tmp_path


def test_counts_come_from_the_footers_per_table_per_day(lake: Path) -> None:
    counts = dq.scan(lake / "parquet")
    assert counts == {"liquidations": {"2026-09-01": 3, "2026-09-03": 9}}


def test_an_empty_lake_scans_to_nothing(tmp_path: Path) -> None:
    assert dq.scan(tmp_path / "parquet") == {}


def test_a_partition_the_filesystem_cannot_open_is_a_hole_not_a_crash(
    lake: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A drive drop left one directory entry on the owner's volume raising EINVAL.

    A `date=*` glob dies on it before reaching any good day, and this report is the
    Phase 2 gate, so it has to name the partition and carry on.
    """
    from liqfade.recorder import writer

    real = writer.lake_files

    def fails_on_one(directory: Path) -> list[Path]:
        if directory.name == "date=2026-09-03":
            raise OSError(22, "Invalid argument")
        return real(directory)

    monkeypatch.setattr(writer, "lake_files", fails_on_one)
    counts = dq.scan(lake / "parquet")

    assert counts == {"liquidations": {"2026-09-01": 3, "2026-09-03": 0}}
    assert "UNREADABLE" in capsys.readouterr().err


def test_an_appledouble_sidecar_does_not_break_the_report(lake: Path) -> None:
    """macOS leaves a 4 KiB `._x.parquet` beside every file our code writes to the drive.

    This report is the Phase 2 gate, and a bare glob made it raise on the owner's lake
    the moment it met one.
    """
    partition = lake / "parquet" / "liquidations" / "date=2026-09-01"
    (partition / "._hyperliquid_00.parquet").write_bytes(b"not parquet at all")
    assert dq.scan(lake / "parquet") == {"liquidations": {"2026-09-01": 3, "2026-09-03": 9}}


def test_a_day_with_no_partition_at_all_still_appears_in_the_range(lake: Path) -> None:
    """A missing directory is the worst hole, so it must not be silently absent."""
    counts = dq.scan(lake / "parquet")
    assert dq.all_days(counts) == ["2026-09-01", "2026-09-02", "2026-09-03"]


def test_a_sparkline_draws_a_hole_as_a_space(lake: Path) -> None:
    assert dq.sparkline([3, 0, 9]) == "▃ █"
    assert dq.sparkline([]) == ""
    assert dq.sparkline([0, 0]) == "  "


def test_a_nonzero_day_never_renders_as_blank() -> None:
    """One liquidation against a peak of a million must still show something."""
    assert dq.sparkline([1, 1_000_000])[0] != " "


def test_the_report_flags_the_zero_row_day_and_names_the_peak(lake: Path) -> None:
    counts = dq.scan(lake / "parquet")
    text = dq.render(counts, dq.all_days(counts))
    assert "## Days with zero rows" in text
    assert "2026-09-02" in text
    assert "peak 9 on 2026-09-03" in text
    assert "| **total** | **12** |" in text


def test_a_lake_with_no_holes_says_so(tmp_path: Path) -> None:
    root = tmp_path / "parquet"
    write_partitioned(root, "liquidations", "hyperliquid", "hl_s3", liq_rows(date(2026, 9, 1), 2))
    counts = dq.scan(root)
    assert "Every table has rows on every day" in dq.render(counts, dq.all_days(counts))


def test_the_script_writes_the_report_and_exits_zero(
    lake: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reports = lake / "reports"
    assert dq.main(["--data-dir", str(lake), "--reports-dir", str(reports)]) == 0
    assert (reports / "data_quality" / "counts.md").exists()
    assert "1 zero-row days" in capsys.readouterr().out


def test_the_script_exits_one_when_there_is_no_lake(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert dq.main(["--data-dir", str(tmp_path)]) == 1
    assert "nothing to report" in capsys.readouterr().out
