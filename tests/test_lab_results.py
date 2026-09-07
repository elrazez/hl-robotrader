"""Reading finished runs back out of `reports/<stamp>_<hash>/`."""

from __future__ import annotations

import json
from pathlib import Path

from liqfade.lab.results import (
    compare_metrics,
    config_diff,
    filter_runs,
    lagged_net,
    list_runs,
    promote,
    sort_runs,
)

LAKE = Path(__file__).resolve().parent / "fixtures"

SUMMARY = {
    "run_id": "20260906T120000Z_abcd1234",
    "config_hash": "abcd1234ef",
    "env": "paper",
    "trigger": "exact",
    "exhaustion_mode": "rule",
    "latency_ms": 1000,
    "lagged_ms": 3000,
    "latency_fragile": False,
    "start_ms": 1756684800000,
    "end_ms": 1756771200000,
    "coins": ["BTC", "ETH"],
    "starting_equity": 10000.0,
    "metrics": {"trades": 12, "net_pnl": 340.5, "hit_rate": 0.58, "max_drawdown_frac": 0.031},
    "train": {"net_pnl": 300.0},
    "holdout": {"net_pnl": 40.5},
    "funnel": {"intents": 90, "trades": 12},
    "rules": {
        "oi_flush": {"evaluated": 90, "passed": 20, "killed": 0, "required": False},
        "vwap_stretch": {"evaluated": 90, "passed": 83, "killed": 0, "required": False},
    },
}


def write_run(root: Path, run_id: str, **over: object) -> Path:
    directory = root / run_id
    directory.mkdir(parents=True)
    payload = {**SUMMARY, "run_id": run_id, **over}
    (directory / "summary.json").write_text(json.dumps(payload))
    (directory / "report.md").write_text(f"# Backtest {run_id}\n\n## Headline\n\nnothing\n")
    return directory


def test_a_run_directory_becomes_a_row(tmp_path: Path) -> None:
    write_run(tmp_path, "20260906T120000Z_abcd1234")
    (row,) = list_runs(tmp_path)
    assert row.run_id == "20260906T120000Z_abcd1234"
    assert row.coins == ("BTC", "ETH")
    assert row.start == "2025-09-01" and row.end == "2025-09-01"
    assert row.trades == 12
    assert row.net == 340.5
    assert row.hit_rate == 0.58
    assert row.max_drawdown_frac == 0.031
    assert row.holdout_net == 40.5
    assert row.lagged_ms == 3000
    assert row.finished is True


def test_a_directory_without_a_summary_is_listed_as_unfinished(tmp_path: Path) -> None:
    (tmp_path / "20260906T130000Z_deadbeef").mkdir(parents=True)
    (row,) = list_runs(tmp_path)
    assert row.finished is False
    assert row.trades == 0


def test_the_labs_own_sidecar_names_the_config_and_the_wall_clock(tmp_path: Path) -> None:
    directory = write_run(tmp_path, "20260906T120000Z_abcd1234")
    (directory / "lab.json").write_text(
        json.dumps(
            {
                "config_name": "v1 confluence",
                "config_path": "config/backtest/v1_confluence.yaml",
                "overrides": {"intent.k_stop": 1.0},
                "started_at": 100.0,
                "finished_at": 460.0,
            }
        )
    )
    (row,) = list_runs(tmp_path)
    assert row.config == "v1 confluence"
    assert row.wall_clock_s == 360.0


def test_a_run_the_lab_did_not_launch_falls_back_to_the_config_hash(tmp_path: Path) -> None:
    write_run(tmp_path, "20260906T120000Z_abcd1234")
    (row,) = list_runs(tmp_path)
    assert row.config == "abcd1234"
    assert row.wall_clock_s is None


def test_the_lagged_net_is_read_from_the_run_log_the_cli_wrote(tmp_path: Path) -> None:
    """`liqfade backtest` prints it and summary.json does not carry it, so the log is
    where it lives. A run without a log simply has no number, not a wrong one."""
    log = tmp_path / "run.log"
    log.write_text(
        "reports/20260906T120000Z_abcd1234\n"
        "lagged rerun at 3000 ms: 51 trades, net -558.72 USD\n"
        "trigger=exact coins=4 intents=540 trades=59 net=-410.00 USD\n"
    )
    assert lagged_net(log) == -558.72
    assert lagged_net(tmp_path / "absent.log") is None


def test_sorting_and_filtering(tmp_path: Path) -> None:
    write_run(
        tmp_path, "20260906T120000Z_aaaa1111", metrics={**SUMMARY["metrics"], "net_pnl": 10.0}
    )
    write_run(
        tmp_path, "20260906T130000Z_bbbb2222", metrics={**SUMMARY["metrics"], "net_pnl": 99.0}
    )
    rows = list_runs(tmp_path)
    assert [row.net for row in sort_runs(rows, "net", reverse=True)] == [99.0, 10.0]
    assert [row.net for row in sort_runs(rows, "run_id")] == [10.0, 99.0]
    assert len(filter_runs(rows, "bbbb")) == 1
    assert len(filter_runs(rows, "BTC")) == 2
    assert filter_runs(rows, "") == rows


def test_two_runs_compare_field_by_field(tmp_path: Path) -> None:
    write_run(tmp_path, "20260906T120000Z_aaaa1111")
    write_run(
        tmp_path,
        "20260906T130000Z_bbbb2222",
        metrics={**SUMMARY["metrics"], "net_pnl": 99.0, "trades": 5},
    )
    left, right = list_runs(tmp_path)
    rows = {label: (a, b) for label, a, b in compare_metrics(left, right)}
    assert rows["net"] == (340.5, 99.0)
    assert rows["trades"] == (12, 5)


def test_comparing_configs_diffs_only_the_keys_that_differ(tmp_path: Path) -> None:
    first = write_run(tmp_path, "20260906T120000Z_aaaa1111")
    second = write_run(tmp_path, "20260906T130000Z_bbbb2222")
    first.joinpath("lab.json").write_text(
        json.dumps(
            {"config_name": "a", "overrides": {"intent.k_stop": 1.0, "intent.k_target": 0.8}}
        )
    )
    second.joinpath("lab.json").write_text(
        json.dumps(
            {"config_name": "b", "overrides": {"intent.k_stop": 1.5, "intent.k_target": 0.8}}
        )
    )
    left, right = list_runs(tmp_path)
    assert config_diff(left, right) == [("intent.k_stop", 1.0, 1.5)]


def test_configs_cannot_be_diffed_when_the_lab_did_not_launch_the_runs(tmp_path: Path) -> None:
    write_run(tmp_path, "20260906T120000Z_aaaa1111")
    write_run(tmp_path, "20260906T130000Z_bbbb2222")
    left, right = list_runs(tmp_path)
    assert config_diff(left, right) == []


def test_promote_reruns_the_same_config_and_coins_over_the_year(tmp_path: Path) -> None:
    from liqfade.lab.runs import THE_YEAR

    directory = write_run(tmp_path, "20260906T120000Z_abcd1234")
    directory.joinpath("lab.json").write_text(
        json.dumps(
            {
                "config_name": "v1",
                "config_path": "config/backtest/v1_confluence.yaml",
                "overrides": {},
            }
        )
    )
    (row,) = list_runs(tmp_path)
    spec = promote(row, reports_dir=tmp_path, data_dir=LAKE)
    assert spec is not None
    assert spec.start == THE_YEAR[0] and spec.end == THE_YEAR[1]
    assert spec.coins == ("BTC", "ETH")
    assert spec.config == Path("config/backtest/v1_confluence.yaml")
    assert spec.equity == 10000.0


def test_promote_needs_to_know_which_config_it_was(tmp_path: Path) -> None:
    write_run(tmp_path, "20260906T120000Z_abcd1234")
    (row,) = list_runs(tmp_path)
    assert promote(row, reports_dir=tmp_path, data_dir=LAKE) is None


def test_a_real_report_directory_parses(tmp_path: Path) -> None:
    """The strongest pin available: run the CLI over the fixture day and read it back."""
    import io
    from contextlib import redirect_stdout

    from liqfade.cli import main

    with redirect_stdout(io.StringIO()):
        code = main(
            [
                "backtest",
                "--data-dir",
                str(LAKE),
                "--coins",
                "BTC",
                "--start",
                "2026-08-30",
                "--end",
                "2026-08-30",
                "--out",
                str(tmp_path),
            ]
        )
    assert code == 0
    (row,) = list_runs(tmp_path)
    assert row.finished is True
    assert row.coins == ("BTC",)
    assert row.start == "2026-08-30"
    assert row.trigger == "exact"
