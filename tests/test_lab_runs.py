"""The run spec, the window presets and the queue.

The lab never imports the engine to run a replay. It builds an argv for `liqfade backtest`
and spawns it, so a crash in the lab cannot take a run down and closing the lab leaves the
run going. These tests drive that argv and that queue; the one subprocess test at the end
spawns a real process, because SIGTERM is the part a fake cannot prove.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from liqfade.lab.runs import (
    REFERENCE_DAYS,
    THE_YEAR,
    Job,
    RunQueue,
    RunSpec,
    lake_days,
    window_presets,
)

LAKE = Path(__file__).resolve().parent / "fixtures"


def spec(tmp_path: Path, **over: object) -> RunSpec:
    fields: dict[str, object] = {
        "config": Path("config/backtest/v0.yaml"),
        "coins": ("BTC",),
        "start": "2026-08-30",
        "end": "2026-08-30",
        "equity": 10000.0,
        "latency_ms": None,
        "lagged_ms": None,
        "cache": True,
        "data_dir": LAKE,
        "reports_dir": tmp_path,
    }
    fields.update(over)
    return RunSpec(**fields)  # type: ignore[arg-type]


def test_the_resolved_command_is_the_one_the_owner_would_have_typed(tmp_path: Path) -> None:
    argv = spec(tmp_path).argv(progress=tmp_path / "p.json")
    assert argv[:2] == ["liqfade", "backtest"]
    assert "--config" in argv and "config/backtest/v0.yaml" in argv
    assert argv[argv.index("--coins") + 1] == "BTC"
    assert argv[argv.index("--start") + 1] == "2026-08-30"
    assert argv[argv.index("--equity") + 1] == "10000.0"
    assert argv[argv.index("--out") + 1] == str(tmp_path)
    assert argv[argv.index("--progress-file") + 1] == str(tmp_path / "p.json")
    # Defaults that must not be spelled out: the cache is on and there is no lagged rerun.
    assert "--no-cache" not in argv
    assert "--lagged-ms" not in argv


def test_latency_is_set_the_way_every_other_config_key_is_overridden(tmp_path: Path) -> None:
    """`liqfade backtest` has no --latency flag and does not need one: the env layer is
    already how a single key is overridden, and it goes through the same validation."""
    run = spec(tmp_path, latency_ms=1500)
    assert run.env() == {"LIQFADE_BACKTEST__LATENCY_MS": "1500"}
    assert run.command(progress=tmp_path / "p.json").startswith(
        "LIQFADE_BACKTEST__LATENCY_MS=1500 "
    )
    assert spec(tmp_path).env() == {}


def test_the_lagged_rerun_and_the_cache_switch_appear_when_asked(tmp_path: Path) -> None:
    argv = spec(tmp_path, lagged_ms=3000, cache=False).argv(progress=tmp_path / "p.json")
    assert argv[argv.index("--lagged-ms") + 1] == "3000"
    assert "--no-cache" in argv


def test_no_coins_means_the_core_universe_and_the_flag_is_left_off(tmp_path: Path) -> None:
    assert "--coins" not in spec(tmp_path, coins=()).argv(progress=tmp_path / "p.json")


def test_the_command_shown_before_launch_is_the_argv(tmp_path: Path) -> None:
    run = spec(tmp_path)
    shown = run.command(progress=tmp_path / "p.json")
    for part in run.argv(progress=tmp_path / "p.json"):
        assert part in shown


def test_the_lakes_days_come_from_the_trades_partitions() -> None:
    assert lake_days(LAKE) == ["2026-08-30"]


def test_the_presets_cover_the_reference_days_the_year_and_the_tail_of_the_lake() -> None:
    days = [f"2026-08-{day:02d}" for day in range(1, 31)]
    presets = dict(window_presets(days))
    assert presets["the three reference days"] == REFERENCE_DAYS
    assert presets["the year"] == THE_YEAR
    assert presets["the last 7 days of the lake"] == ("2026-08-24", "2026-08-30")
    assert presets["the last 30 days of the lake"] == ("2026-08-01", "2026-08-30")
    # A lake shorter than the preset is clamped to what is there rather than dropped.
    assert presets["the last quarter of the lake"] == ("2026-08-01", "2026-08-30")


def test_presets_with_an_empty_lake_still_offer_the_fixed_windows() -> None:
    presets = dict(window_presets([]))
    assert presets["the three reference days"] == REFERENCE_DAYS
    assert "the last 7 days of the lake" not in presets


class FakeProcess:
    def __init__(self, code: int | None = None) -> None:
        self.code = code
        self.terminated = False

    def poll(self) -> int | None:
        return self.code

    def terminate(self) -> None:
        self.terminated = True
        self.code = -15

    def kill(self) -> None:
        self.code = -9


def test_the_queue_runs_one_job_at_a_time(tmp_path: Path) -> None:
    spawned: list[list[str]] = []

    def spawn(argv: list[str], log: Path, env: dict[str, str]) -> FakeProcess:
        spawned.append(argv)
        return FakeProcess()

    queue = RunQueue(tmp_path, spawn=spawn, lake_busy=lambda: False)
    first = queue.submit(spec(tmp_path))
    second = queue.submit(spec(tmp_path))
    queue.poll()
    assert first.state == "running"
    assert second.state == "queued"
    assert len(spawned) == 1

    first.process.code = 0  # type: ignore[union-attr]
    queue.poll()
    assert first.state == "done"
    assert second.state == "running"
    assert len(spawned) == 2


def test_a_lake_busy_with_another_chats_run_holds_the_queue(tmp_path: Path) -> None:
    """One lake job at a time, enforced by the lab and by pgrep: another terminal's year
    run must not have a second replay started on top of it."""
    busy = True

    queue = RunQueue(tmp_path, spawn=lambda argv, log, env: FakeProcess(), lake_busy=lambda: busy)
    job = queue.submit(spec(tmp_path))
    queue.poll()
    assert job.state == "queued"
    assert "another backtest" in job.note

    busy = False
    queue.poll()
    assert job.state == "running"


def test_a_nonzero_exit_is_failed_not_done(tmp_path: Path) -> None:
    queue = RunQueue(tmp_path, spawn=lambda argv, log, env: FakeProcess(), lake_busy=lambda: False)
    job = queue.submit(spec(tmp_path))
    queue.poll()
    job.process.code = 1  # type: ignore[union-attr]
    queue.poll()
    assert job.state == "failed"


def test_cancelling_a_queued_job_never_starts_it(tmp_path: Path) -> None:
    queue = RunQueue(tmp_path, spawn=lambda argv, log, env: FakeProcess(), lake_busy=lambda: True)
    job = queue.submit(spec(tmp_path))
    queue.cancel(job)
    queue.poll()
    assert job.state == "cancelled"
    assert job.process is None


def test_the_log_moves_beside_the_report_when_the_run_finishes(tmp_path: Path) -> None:
    import json

    report = tmp_path / "20260906T101010Z_abcdef12"
    report.mkdir()
    queue = RunQueue(tmp_path, spawn=lambda argv, log, env: FakeProcess(), lake_busy=lambda: False)
    job = queue.submit(spec(tmp_path))
    queue.poll()
    job.log_path.write_text("the run said things\n")
    job.progress_path.write_text(json.dumps({"stage": "done", "report_dir": str(report)}))
    job.process.code = 0  # type: ignore[union-attr]
    queue.poll()
    assert (report / "run.log").read_text() == "the run said things\n"


def test_the_lab_records_which_config_produced_the_report(tmp_path: Path) -> None:
    """summary.json identifies a run by config hash. The name and the overrides behind that
    hash are the lab's to remember, and Results reads them back."""
    import json

    from liqfade.lab.configs import save_config

    config = save_config(
        tmp_path / "tight.yaml", "Tight stops", "k_stop halved", {"intent.k_stop": 0.5}
    )
    report = tmp_path / "20260906T101010Z_abcdef12"
    report.mkdir()
    queue = RunQueue(tmp_path, spawn=lambda argv, log, env: FakeProcess(), lake_busy=lambda: False)
    job = queue.submit(spec(tmp_path, config=config))
    queue.poll()
    job.progress_path.write_text(json.dumps({"stage": "done", "report_dir": str(report)}))
    job.process.code = 0  # type: ignore[union-attr]
    queue.poll()
    sidecar = json.loads((report / "lab.json").read_text())
    assert sidecar["config_name"] == "Tight stops"
    assert sidecar["overrides"] == {"intent.k_stop": 0.5}
    assert sidecar["config_path"] == str(config)
    assert sidecar["finished_at"] >= sidecar["started_at"] > 0
    assert "liqfade backtest" in sidecar["command"]


@pytest.mark.parametrize("signal_name", ["term"])
def test_cancel_really_stops_a_real_process(tmp_path: Path, signal_name: str) -> None:
    """The one test with a real subprocess: SIGTERM is what a fake cannot prove."""
    import subprocess
    import sys

    queue = RunQueue(tmp_path, lake_busy=lambda: False)
    queue.spawn = lambda argv, log, env: subprocess.Popen(  # type: ignore[assignment,method-assign]
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=log.open("wb"),
        stderr=subprocess.STDOUT,
    )
    job = queue.submit(spec(tmp_path))
    queue.poll()
    assert job.state == "running"
    queue.cancel(job)
    deadline = time.monotonic() + 10
    while job.state in ("running", "cancelling") and time.monotonic() < deadline:
        time.sleep(0.05)
        queue.poll()
    assert job.state == "cancelled"


def test_a_job_id_is_stable_and_filesystem_safe(tmp_path: Path) -> None:
    queue = RunQueue(tmp_path, spawn=lambda argv, log, env: FakeProcess(), lake_busy=lambda: False)
    job: Job = queue.submit(spec(tmp_path))
    assert job.progress_path.parent == tmp_path / ".lab"
    assert job.id in job.progress_path.name
    assert "/" not in job.id


def test_the_lab_note_is_written_at_launch_not_at_the_reaper(tmp_path: Path) -> None:
    """Closing the lab mid-run must not lose which config produced the report.

    `lab.json` was built in `_reap`, so a run that outlived the lab, which is the whole
    point of spawning it in its own session, finished with nobody left to write it. Results
    then fell back to the config hash and the run showed as `abcdef12`. It is written when
    the job starts instead, into the work directory beside the log, and moved next to the
    report when the run is reaped.
    """
    import orjson

    from liqfade.lab.configs import save_config

    config = save_config(
        tmp_path / "tight.yaml", "Tight stops", "k_stop halved", {"intent.k_stop": 0.5}
    )
    queue = RunQueue(tmp_path, spawn=lambda argv, log, env: FakeProcess(), lake_busy=lambda: False)
    job = queue.submit(spec(tmp_path, config=config))
    queue.poll()
    assert job.sidecar_path.exists(), "nothing recorded the job before it finished"
    note = orjson.loads(job.sidecar_path.read_bytes())
    assert note["config_name"] == "Tight stops"
    assert note["overrides"] == {"intent.k_stop": 0.5}


def test_a_report_left_by_a_dead_lab_gets_its_sidecars_on_the_next_start(
    tmp_path: Path,
) -> None:
    """The lab was closed while the run was going. The next one files what it left."""
    import orjson

    report = tmp_path / "20260906T101010Z_abcdef12"
    report.mkdir()
    queue = RunQueue(tmp_path, spawn=lambda argv, log, env: FakeProcess(), lake_busy=lambda: False)
    job = queue.submit(spec(tmp_path))
    queue.poll()
    job.log_path.write_text("the run said things\n")
    # The lab dies here: the run goes on and writes its own report and progress file.
    job.progress_path.write_text(
        orjson.dumps({"stage": "done", "report_dir": str(report)}).decode()
    )

    RunQueue(tmp_path, spawn=lambda argv, log, env: FakeProcess(), lake_busy=lambda: False)
    assert (report / "run.log").read_text() == "the run said things\n"
    assert orjson.loads((report / "lab.json").read_bytes())["job_id"] == job.id
