"""The candle validation script: the match maths, and one run end to end offline."""

from __future__ import annotations

import importlib.util
from datetime import UTC, date, datetime
from datetime import time as dt_time
from pathlib import Path

import httpx
import polars as pl
import pytest

from liqfade.data.schemas import SOURCE_DERIVED, SOURCE_RECORDER, rows_to_table
from liqfade.recorder.writer import partition_dir, write_atomic

SCRIPT = Path(__file__).parents[1] / "scripts" / "research" / "candle_validate.py"
DAY = date(2026, 9, 3)
MINUTE = int(datetime.combine(DAY, dt_time(0), tzinfo=UTC).timestamp() * 1000)


def load() -> object:
    spec = importlib.util.spec_from_file_location("candle_validate", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cv = load()


def bar(
    ts_ms: int, o: float, h: float, low: float, c: float, v: float, n: int
) -> dict[str, object]:
    return {"coin": "BTC", "ts_ms": ts_ms, "o": o, "h": h, "l": low, "c": c, "v": v, "n": n}


def frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(rows).select(["coin", "ts_ms", *cv.FIELDS])


def test_two_identical_sets_match_on_everything() -> None:
    rows = [bar(MINUTE, 1.0, 2.0, 0.5, 1.5, 3.0, 4)]
    table = cv.compare_bars(frame(rows), frame(rows))
    assert table.row(0, named=True)["minutes"] == 1
    assert all(table.row(0, named=True)[field] == 1.0 for field in cv.FIELDS)
    assert table.row(0, named=True)["ohlc_where_n_agrees"] == 1.0


def test_a_minute_the_reference_does_not_have_is_not_compared() -> None:
    ours = frame(
        [bar(MINUTE, 1.0, 1.0, 1.0, 1.0, 1.0, 1), bar(MINUTE + 60_000, 2.0, 2, 2, 2, 1, 1)]
    )
    theirs = frame([bar(MINUTE, 1.0, 1.0, 1.0, 1.0, 1.0, 1)])
    assert cv.compare_bars(ours, theirs).row(0, named=True)["minutes"] == 1
    assert cv.coverage(ours, theirs) == {"ours": 2, "reference": 1, "shared": 1}


def test_the_rate_that_matters_ignores_minutes_where_the_trade_counts_differ() -> None:
    """One side missed a trade there, so `o` and `c` can differ for that reason alone."""
    ours = frame(
        [
            bar(MINUTE, 1.0, 1.0, 1.0, 1.0, 1.0, 2),  # same n, o agrees
            bar(MINUTE + 60_000, 9.0, 9.0, 9.0, 9.0, 1.0, 1),  # n differs, o does not agree
        ]
    )
    theirs = frame(
        [
            bar(MINUTE, 1.0, 1.0, 1.0, 1.0, 1.0, 2),
            bar(MINUTE + 60_000, 8.0, 8.0, 8.0, 8.0, 1.0, 2),
        ]
    )
    row = cv.compare_bars(ours, theirs).row(0, named=True)
    assert row["o"] == 0.5  # half the shared minutes
    assert row["same_n_minutes"] == 1
    assert row["ohlc_where_n_agrees"] == 1.0  # and the derivation is clean on that one


def test_volume_is_compared_relatively_because_the_sums_are_built_in_two_orders() -> None:
    ours = frame([bar(MINUTE, 1.0, 1.0, 1.0, 1.0, 30_800.3529, 4)])
    theirs = frame([bar(MINUTE, 1.0, 1.0, 1.0, 1.0, 30_800.352900000004, 4)])
    assert cv.compare_bars(ours, theirs).row(0, named=True)["v"] == 1.0


def test_no_shared_minute_is_an_error_that_says_why(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cv.main(
        [
            "--day",
            DAY.isoformat(),
            "--reference",
            "recorder",
            "--data-dir",
            str(tmp_path),
            "--reports-dir",
            str(tmp_path / "reports"),
        ]
    )
    assert code == 1
    assert "keeps about 3 days" in capsys.readouterr().err


def write_bars(data_dir: Path, source: str, name: str, rows: list[dict[str, object]]) -> None:
    directory = partition_dir(data_dir / "parquet", "candles_1m", DAY.isoformat())
    directory.mkdir(parents=True, exist_ok=True)
    write_atomic(rows_to_table("candles_1m", rows, source), directory / name)  # type: ignore[arg-type]


def test_a_run_against_the_recorder_stream_writes_the_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_bars(
        tmp_path,
        SOURCE_DERIVED,
        "hyperliquid_day.parquet",
        [bar(MINUTE, 1.0, 2.0, 0.5, 1.5, 3.0, 4), bar(MINUTE + 60_000, 1.5, 3.0, 1.0, 2.0, 5.0, 6)],
    )
    write_bars(
        tmp_path,
        SOURCE_RECORDER,
        "hyperliquid_00.parquet",
        [bar(MINUTE, 1.0, 2.0, 0.5, 1.5, 3.0, 4), bar(MINUTE + 60_000, 1.9, 3.0, 1.0, 2.0, 5.0, 6)],
    )
    code = cv.main(
        [
            "--day",
            DAY.isoformat(),
            "--reference",
            "recorder",
            "--coins",
            "BTC",
            "--data-dir",
            str(tmp_path),
            "--reports-dir",
            str(tmp_path / "reports"),
        ]
    )
    assert code == 0
    out = tmp_path / "reports" / "data_quality" / f"candle_validation_{DAY}_recorder.md"
    text = out.read_text()
    assert "| BTC | 2 | 50.00% |" in text  # `o` differs on the second minute
    assert "50.00% of 2" in text  # and n agrees on both, so the split is honest
    assert str(out) in capsys.readouterr().out


class FakeApi:
    """Serves `candleSnapshot` for one coin and two minutes, and nothing older."""

    def __init__(self, bars: list[dict[str, object]]) -> None:
        self.bars = bars

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        import orjson

        req = orjson.loads(request.content)["req"]
        inside = [
            {
                "t": row["ts_ms"],
                "s": row["coin"],
                "o": str(row["o"]),
                "h": str(row["h"]),
                "l": str(row["l"]),
                "c": str(row["c"]),
                "v": str(row["v"]),
                "n": row["n"],
            }
            for row in self.bars
            if int(req["startTime"]) <= int(row["ts_ms"]) <= int(req["endTime"])  # type: ignore[arg-type]
        ]
        return httpx.Response(200, json=inside)


def test_the_api_side_reads_candlesnapshot_for_the_day(tmp_path: Path) -> None:
    api = FakeApi([bar(MINUTE, 1.0, 2.0, 0.5, 1.5, 3.0, 4)])
    bars = cv.api_bars(api.client(), "https://api.example", DAY, ["BTC"])
    assert bars.select(["coin", "ts_ms", "o"]).rows() == [("BTC", MINUTE, 1.0)]
