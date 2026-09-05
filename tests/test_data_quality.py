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


def liq_rows(day: date, count: int, venue: str = "hyperliquid") -> list[dict[str, object]]:
    base = int(datetime.combine(day, time.min, tzinfo=UTC).timestamp() * 1000)
    return [
        {
            "venue": venue,
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
    assert counts == {"liquidations": {"2026-09-01": (3, 1), "2026-09-03": (9, 1)}}


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

    assert counts == {"liquidations": {"2026-09-01": (3, 1), "2026-09-03": (0, 0)}}
    assert "UNREADABLE" in capsys.readouterr().err


def test_an_appledouble_sidecar_does_not_break_the_report(lake: Path) -> None:
    """macOS leaves a 4 KiB `._x.parquet` beside every file our code writes to the drive.

    This report is the Phase 2 gate, and a bare glob made it raise on the owner's lake
    the moment it met one.
    """
    partition = lake / "parquet" / "liquidations" / "date=2026-09-01"
    (partition / "._hyperliquid_00.parquet").write_bytes(b"not parquet at all")
    assert dq.scan(lake / "parquet") == {
        "liquidations": {"2026-09-01": (3, 1), "2026-09-03": (9, 1)}
    }


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
    assert "1 days short of expected" in capsys.readouterr().out


def test_the_breakdown_splits_a_day_by_venue_and_source(tmp_path: Path) -> None:
    """One day, two venues, two pulls: the day is not empty and is still half missing.

    The count per day says nothing on its own for `liquidations` and `trades`, which is
    why the report splits them: a partition full of Binance rows from Tardis with every
    Hyperliquid row gone reads as a healthy day until you look at this.
    """
    root = tmp_path / "parquet"
    day = date(2026, 9, 1)
    write_partitioned(root, "liquidations", "hyperliquid", "hl_s3", liq_rows(day, 3))
    write_partitioned(root, "liquidations", "binance", "tardis", liq_rows(day, 5, "binance"))
    write_partitioned(
        root, "liquidations", "binance", "tardis", liq_rows(date(2026, 9, 2), 2, "binance")
    )

    found = dq.breakdown(root, ["2026-09-01", "2026-09-02"], tables=("liquidations",))
    assert found == {
        "liquidations": {
            ("hyperliquid", "hl_s3"): {"2026-09-01": 3},
            ("binance", "tardis"): {"2026-09-01": 5, "2026-09-02": 2},
        }
    }

    text = dq.render(dq.scan(root), dq.all_days(dq.scan(root)), found)
    assert "| liquidations | binance | tardis | 7 | 2 | 2026-09-01 | 2026-09-02 |" in text
    assert "| liquidations | hyperliquid | hl_s3 | 3 | 1 | 2026-09-01 | 2026-09-01 |" in text


def test_the_side_rule_reads_falling_prices_into_long_liquidations(tmp_path: Path) -> None:
    """The same measurement `SideRuleCheck` makes, in SQL, over a day of the lake."""
    root = tmp_path / "parquet"
    day = date(2026, 9, 1)
    base = int(datetime.combine(day, time.min, tzinfo=UTC).timestamp() * 1000)
    # A price falling 1% over three minutes, and a long liquidation at the end of it.
    trades = [
        {
            "venue": "hyperliquid",
            "coin": "BTC",
            "ts_ms": base + minute * 60_000,
            "rx_ms": 0,
            "px": 100.0 - minute,
            "sz": 1.0,
            "side": "sell",
            "tid": minute,
        }
        for minute in range(4)
    ]
    write_partitioned(root, "trades", "hyperliquid", "hl_s3", trades)
    liq = liq_rows(day, 1)
    liq[0]["ts_ms"] = base + 3 * 60_000
    write_partitioned(root, "liquidations", "hyperliquid", "hl_s3", liq)

    totals = dq.side_rule(root, ["2026-09-01"])
    count, summed = totals["2026-09"]["long"]
    assert count == 1
    # 97 against 98 one minute earlier: -102.04 bps, and the sign is the whole point.
    assert summed == pytest.approx(-102.0408, abs=1e-3)


def test_the_script_exits_one_when_there_is_no_lake(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert dq.main(["--data-dir", str(tmp_path)]) == 1
    assert "nothing to report" in capsys.readouterr().out
