"""`liqfade refusals` end to end: a real lake, a real registry, a report on disk.

The tracer bullet for the whole session. The refusals go into `orders.sqlite` the way the
live drain writes them, the tape comes out of a Parquet lake the way `liqfade sync` leaves
it, and the report's per-reason totals have to equal the outcomes `score_refusal` produces
on its own. If those two ever disagree, the summary is lying about the trades under it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import polars as pl

from liqfade.backtest.shadow import score_all
from liqfade.cli import main
from liqfade.config import load_settings
from liqfade.execution.orders import OrderStore
from liqfade.strategy.engine import Rejection
from tests.fixtures.golden_lake import CASCADE_STARTS, COIN, mid_at, write_lake

DAY = "2026-03-01"


def a_refusal(ts_ms: int, reason: str, stage: str = "filter") -> Rejection:
    """A long fade priced off the fixture's own price path, so the outcome is derivable."""
    entry = mid_at(ts_ms)
    return Rejection(
        coin=COIN,
        side="long",
        ts_ms=ts_ms,
        reason=reason,
        stage=stage,
        entry_px=entry,
        target_px=entry + 0.5,
        stop_px=entry - 1.0,
        time_stop_s=300.0,
        urgency="normal",
        explain={"liq_z_60s": 4.0},
    )


def seed(db: Path, refusals: list[Rejection]) -> None:
    async def run() -> None:
        store = await OrderStore.open(db)
        try:
            await store.add_refusals(refusals)
        finally:
            await store.close()

    asyncio.run(run())


def test_the_report_totals_equal_the_outcomes_under_them(tmp_path: Path) -> None:
    lake = tmp_path / "lake"
    write_lake(lake, 1)
    db = tmp_path / "orders.sqlite"
    # One inside the recovery, which reaches the target, and one on the way down, which
    # does not. Two reasons, so the grouping has something to separate.
    refusals = [
        a_refusal(CASCADE_STARTS[0] + 60_000, "spread_too_wide"),
        a_refusal(CASCADE_STARTS[1] + 5_000, "edge_too_small"),
    ]
    seed(db, refusals)
    out = tmp_path / "reports"

    code = main(
        [
            "refusals",
            "--env",
            "paper",
            "--start",
            DAY,
            "--end",
            DAY,
            "--data-dir",
            str(lake),
            "--db",
            str(db),
            "--out",
            str(out),
            "--equity",
            "10000",
        ]
    )

    assert code == 0
    report = (out / f"refusals_{DAY}_{DAY}.md").read_text()
    assert "This is a bound, not an answer" in report
    assert "effective_ts" in report
    assert "spread_too_wide" in report and "edge_too_small" in report

    frame = pl.read_parquet(out / f"refusals_{DAY}_{DAY}.parquet")
    assert frame.height == 2
    assert set(frame["reason"]) == {"spread_too_wide", "edge_too_small"}

    settings = load_settings("paper", environ={"LIQFADE_DATA_DIR": str(lake)})
    expected = score_all(refusals, lake, settings, equity=10_000.0)
    assert [o.reason for _r, o in expected] == frame["outcome"].to_list()
    for (_refusal, outcome), row in zip(expected, frame.iter_rows(named=True), strict=True):
        assert row["net_pnl"] == outcome.net_pnl
        # The table in the report is a sum of exactly these rows and nothing else.
        assert f"{outcome.net_pnl:+,.2f}" in report or outcome.reason == "no_tape"


def test_an_empty_window_writes_a_report_that_says_so(tmp_path: Path) -> None:
    """A week with no refusals is a result, not an error: the loop refused nothing."""
    lake = tmp_path / "lake"
    write_lake(lake, 1)
    db = tmp_path / "orders.sqlite"
    seed(db, [])
    out = tmp_path / "reports"

    assert (
        main(
            [
                "refusals",
                "--env",
                "paper",
                "--start",
                DAY,
                "--end",
                DAY,
                "--data-dir",
                str(lake),
                "--db",
                str(db),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert "0 refusals in the window." in (out / f"refusals_{DAY}_{DAY}.md").read_text()


def test_a_missing_registry_is_an_error_not_an_empty_report(tmp_path: Path) -> None:
    """Reporting "nothing refused" off a database that is not there would be a lie."""
    assert (
        main(
            [
                "refusals",
                "--env",
                "paper",
                "--start",
                DAY,
                "--end",
                DAY,
                "--db",
                str(tmp_path / "nope.sqlite"),
                "--out",
                str(tmp_path),
            ]
        )
        == 1
    )
