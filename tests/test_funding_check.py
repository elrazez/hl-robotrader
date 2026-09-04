"""The funding gate: the applied rate is the sample one minute before the hour."""

from __future__ import annotations

import importlib.util
from datetime import UTC, date, datetime
from datetime import time as dt_time
from pathlib import Path

import httpx
import pytest

from liqfade.data.schemas import SOURCE_HL_ARCHIVE, rows_to_table
from liqfade.recorder.writer import partition_dir, write_atomic

SCRIPT = Path(__file__).parents[1] / "scripts" / "research" / "funding_check.py"
DAY = date(2026, 3, 1)
MIDNIGHT = int(datetime.combine(DAY, dt_time(0), tzinfo=UTC).timestamp() * 1000)
HOUR_MS = 3_600_000
APPLIED = 1.25e-5  # what the exchange charged
RESET = 9.9e-4  # what the HH:00 sample already holds: the next hour's rate


def load() -> object:
    spec = importlib.util.spec_from_file_location("funding_check", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fc = load()


def write_asset_ctx(data_dir: Path, hours: int = 3) -> None:
    """A minute-sampled day where only HH:59 carries the rate that was applied."""
    rows = [
        {
            "coin": coin,
            "ts_ms": MIDNIGHT + minute * 60_000,
            "rx_ms": 0,
            "funding": APPLIED if minute % 60 == 59 else RESET,
            "open_interest": 1.0,
        }
        for minute in range(hours * 60)
        for coin in fc.COINS  # the gate requires every coin, so the fixture has every coin
    ]
    directory = partition_dir(data_dir / "parquet", "asset_ctx", DAY.isoformat())
    directory.mkdir(parents=True, exist_ok=True)
    write_atomic(
        rows_to_table("asset_ctx", rows, SOURCE_HL_ARCHIVE),  # type: ignore[arg-type]
        directory / "hyperliquid_day.parquet",
    )


class FakeApi:
    """`fundingHistory` for the hours after midnight, stamped with the usual jitter."""

    def __init__(self, hours: int = 3, jitter_ms: int = 69) -> None:
        self.hours = hours
        self.jitter_ms = jitter_ms

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        import orjson

        coin = orjson.loads(request.content)["coin"]
        return httpx.Response(
            200,
            json=[
                {
                    "coin": coin,
                    "fundingRate": str(APPLIED),
                    "premium": "0.0",
                    # The API stamps the instant the rate was applied, not the round hour.
                    "time": MIDNIGHT + hour * HOUR_MS + self.jitter_ms,
                }
                for hour in range(1, self.hours)
            ],
        )


def run(tmp_path: Path, api: FakeApi) -> tuple[list[dict], str, bool]:
    import duckdb

    with api.client() as client:
        rows = fc.check(
            duckdb.connect(), client, tmp_path / "parquet", "https://api.example", DAY, DAY
        )
    report, passed = fc.render(DAY, DAY, rows)
    return rows, report, passed


def test_the_sample_before_the_hour_carries_the_applied_rate(tmp_path: Path) -> None:
    write_asset_ctx(tmp_path)
    rows, report, passed = run(tmp_path, FakeApi())

    assert passed
    assert "**Gate: PASS.**" in report
    for row in rows:
        assert row["hours"] == 2
        assert row["within"] == 2
        assert row["worst"] == pytest.approx(0.0)


def test_the_top_of_the_hour_would_have_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The offset is measured, not chosen: at HH:00 the column has already reset."""
    write_asset_ctx(tmp_path)
    monkeypatch.setattr(fc, "OFFSET_MIN", 0)
    _rows, report, passed = run(tmp_path, FakeApi())

    assert not passed
    assert "**Gate: FAIL.**" in report


def test_hours_with_no_sample_are_reported_not_counted_against_the_gate(tmp_path: Path) -> None:
    """The archive's day files are truncated on some days; that is a source gap."""
    write_asset_ctx(tmp_path, hours=2)  # the API publishes 3 hours, the archive holds 2
    rows, report, passed = run(tmp_path, FakeApi(hours=4))

    assert passed
    assert rows[0]["published"] == 3
    # Hours 1 and 2 read 00:59 and 01:59, both written; hour 3 would need 02:59, which
    # the truncated day does not reach.
    assert rows[0]["hours"] == 2
    assert "| BTC | 3 | 2 | 100.00% |" in report


def test_the_api_jitter_past_the_hour_does_not_shift_the_join(tmp_path: Path) -> None:
    """`fundingHistory` stamps the instant of the charge, tens of ms past the boundary."""
    write_asset_ctx(tmp_path)
    rows, _report, passed = run(tmp_path, FakeApi(jitter_ms=931))
    assert passed
    assert rows[0]["hours"] == 2
