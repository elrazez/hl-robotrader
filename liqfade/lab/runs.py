"""Backtest runs as subprocesses, and the queue that lets one at a time near the lake.

The lab is a client of the CLI, not of the engine: it builds the argv the owner would have
typed and spawns it. That is the whole reason a run survives the lab being closed and the
lab survives a run dying. Progress comes back through the run's own `--progress-file`, read
by `backtest/watch.py` so the lab and `liqfade watch` can never disagree about a frame.

One lake job at a time, twice enforced: the queue starts a job only when none of its own is
running, and only when `pgrep -f "liqfade backtest"` finds nothing, so a year run started in
another terminal holds the queue instead of sharing 8 GB with it.

**The queue lives only as long as the lab.** Quitting takes the queue with it: a running job
survives, because it was spawned into its own session and finishing it is the point, and a
job still waiting to start simply never starts. Nothing is written down that would let a
later lab pick the queue back up, and nothing needs to be: a queued job has no process and
no report, only a form the owner can fill in again. What a dead lab does leave behind are
the running job's sidecars, and `_file_orphans` files those on the next start.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import orjson

from liqfade.backtest.progress import ProgressState, read_progress
from liqfade.config.loader import ENV_PREFIX

__all__ = [
    "REFERENCE_DAYS",
    "THE_YEAR",
    "Job",
    "RunQueue",
    "RunSpec",
    "lake_busy",
    "lake_days",
    "window_presets",
]

# The three days Phase 3 and Phase 4 both report on, and the backtested year. Fixed
# windows, so a run over them is comparable with every earlier report on them.
REFERENCE_DAYS = ("2025-10-09", "2025-10-11")
THE_YEAR = ("2025-09-01", "2026-08-30")
# What another chat's run looks like from the outside. The lab's own children match it too,
# which is what we want: two lab runs are as bad as a lab run beside a terminal one.
LAKE_PATTERN = "liqfade backtest"
# Given to a cancelled process before SIGKILL, long enough for the engine to unwind a day.
TERM_GRACE_S = 10.0


def env_key(dotted: str) -> str:
    """A config key spelled as the loader's env override, e.g. backtest.latency_ms."""
    return ENV_PREFIX + dotted.upper().replace(".", "__")


class Process(Protocol):
    """The part of `subprocess.Popen` the queue uses, so tests can hand it a stand-in."""

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...


@dataclass(frozen=True)
class RunSpec:
    """One backtest, as the Run screen describes it before anything is spawned."""

    config: Path
    coins: tuple[str, ...]
    start: str
    end: str
    equity: float
    latency_ms: int | None
    lagged_ms: int | None
    cache: bool
    data_dir: Path | None
    reports_dir: Path

    def argv(self, *, progress: Path) -> list[str]:
        """The command. `--coins` is left off entirely when the run wants the core universe,
        because an empty string would be a different thing to argparse than an absent flag."""
        argv = [
            "liqfade",
            "backtest",
            "--config",
            str(self.config),
            "--start",
            self.start,
            "--end",
            self.end,
            "--equity",
            str(self.equity),
            "--out",
            str(self.reports_dir),
            "--progress-file",
            str(progress),
        ]
        if self.coins:
            argv += ["--coins", ",".join(self.coins)]
        if self.data_dir is not None:
            argv += ["--data-dir", str(self.data_dir)]
        if self.lagged_ms is not None:
            argv += ["--lagged-ms", str(self.lagged_ms)]
        if not self.cache:
            argv.append("--no-cache")
        return argv

    def env(self) -> dict[str, str]:
        """Extra environment for the child. Latency is a config key, so it is spelled as
        the loader's env override rather than as a flag `liqfade backtest` does not have."""
        if self.latency_ms is None:
            return {}
        return {env_key("backtest.latency_ms"): str(self.latency_ms)}

    def command(self, *, progress: Path) -> str:
        """The same argv as one line, which is what the screen shows before launch."""
        prefix = "".join(f"{key}={value} " for key, value in sorted(self.env().items()))
        return prefix + shlex.join(self.argv(progress=progress))

    @property
    def label(self) -> str:
        coins = ",".join(self.coins) if self.coins else "core"
        return f"{self.config.stem} {coins} {self.start}..{self.end}"


@dataclass
class Job:
    """A spec with a process behind it. `note` is why it is not running yet."""

    id: str
    spec: RunSpec
    progress_path: Path
    log_path: Path
    sidecar_path: Path
    state: str = "queued"
    note: str = ""
    process: Process | None = None
    submitted_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    cancelled_at: float = 0.0
    finished_at: float = 0.0
    returncode: int | None = None
    report_dir: Path | None = None

    @property
    def active(self) -> bool:
        return self.state in ("queued", "running")

    def progress(self) -> ProgressState | None:
        return read_progress(self.progress_path)


def lake_days(data_dir: Path) -> list[str]:
    """Every UTC day the lake holds trades for, ascending. The window presets read this."""
    root = Path(data_dir) / "parquet" / "trades"
    return sorted(path.name.removeprefix("date=") for path in root.glob("date=*") if path.is_dir())


def window_presets(days: list[str]) -> Iterator[tuple[str, tuple[str, str]]]:
    """Named windows for the Run screen. The lake-relative ones need a lake to exist."""
    yield "the three reference days", REFERENCE_DAYS
    yield "the year", THE_YEAR
    for label, span in (("7 days", 7), ("30 days", 30), ("quarter", 90)):
        if not days:
            continue
        name = f"the last {label} of the lake" if span != 90 else "the last quarter of the lake"
        # A lake shorter than the window is clamped rather than dropped: a preset that
        # asked for days the lake does not hold would fail at the first partition.
        yield name, (days[-min(span, len(days))], days[-1])


def lake_busy(pattern: str = LAKE_PATTERN) -> bool:
    """True when any `liqfade backtest` process is alive, ours or anyone's.

    `pgrep` rather than a lock file: the run the lab must not collide with is usually one
    started by hand in another terminal, which never took the lab's lock.
    """
    try:
        found = subprocess.run(  # noqa: S603
            ["pgrep", "-f", pattern],  # noqa: S607
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        # No pgrep, or it would not run. Say busy: refusing to start is the safe answer
        # on a machine where a year run may already own the memory.
        return True
    return found.returncode == 0


def _move(source: Path, target: Path) -> None:
    """Put a sidecar beside its report, if it is still where the lab left it."""
    if source.exists() and source != target:
        source.replace(target)


def _spawn(argv: list[str], log: Path, env: dict[str, str]) -> Process:
    log.parent.mkdir(parents=True, exist_ok=True)
    handle = log.open("wb")
    return subprocess.Popen(  # noqa: S603
        argv,
        stdout=handle,
        stderr=subprocess.STDOUT,
        env={**os.environ, **env},
        # Its own session, so the run does not take a Ctrl-C aimed at the lab and does not
        # die when the terminal the lab was started in goes away.
        start_new_session=True,
    )


class RunQueue:
    """Jobs in submission order, at most one of them running.

    `poll` is the whole state machine and is called from the screen's timer. Nothing here
    blocks: a job that has not finished is simply still running next tick.
    """

    def __init__(
        self,
        reports_dir: Path,
        *,
        spawn: Callable[[list[str], Path, dict[str, str]], Any] = _spawn,
        lake_busy: Callable[[], bool] = lake_busy,
    ) -> None:
        self.reports_dir = Path(reports_dir)
        self.work_dir = self.reports_dir / ".lab"
        self.spawn = spawn
        self.lake_busy = lake_busy
        self.jobs: list[Job] = []
        self._next = 1
        with suppress(OSError):
            self._file_orphans()

    def submit(self, spec: RunSpec) -> Job:
        job_id = f"{time.strftime('%Y%m%dT%H%M%S')}-{self._next:02d}"
        self._next += 1
        job = Job(
            id=job_id,
            spec=spec,
            progress_path=self.work_dir / f"{job_id}.progress.json",
            log_path=self.work_dir / f"{job_id}.log",
            sidecar_path=self.work_dir / f"{job_id}.lab.json",
        )
        self.jobs.append(job)
        return job

    @property
    def running(self) -> Job | None:
        return next((job for job in self.jobs if job.state == "running"), None)

    def poll(self) -> None:
        """Reap what finished, then start the next job if the lake is free."""
        for job in self.jobs:
            if job.state in ("running", "cancelling"):
                self._reap(job)
        if self.running is not None:
            return
        for job in self.jobs:
            if job.state != "queued":
                continue
            if self.lake_busy():
                job.note = "waiting: another backtest is on the lake"
                return
            self._start(job)
            return

    def _start(self, job: Job) -> None:
        job.progress_path.parent.mkdir(parents=True, exist_ok=True)
        job.process = self.spawn(
            job.spec.argv(progress=job.progress_path), job.log_path, job.spec.env()
        )
        job.state = "running"
        job.started_at = time.time()
        job.note = ""
        # Written now rather than at the reaper. The run is spawned into its own session so
        # that it outlives the lab, and a note only the reaper writes is a note nobody
        # writes when the lab is closed mid-run. The config is read here too, so the note
        # records the file as it was launched rather than as it was later edited.
        with suppress(OSError):
            job.sidecar_path.write_bytes(
                orjson.dumps(self._sidecar(job), option=orjson.OPT_INDENT_2)
            )

    def _reap(self, job: Job) -> None:
        process = job.process
        if process is None:  # pragma: no cover - a running job always has one
            return
        code = process.poll()
        if code is None:
            if job.state == "cancelling" and time.time() - job.cancelled_at > TERM_GRACE_S:
                # The engine had its grace period to unwind and did not take it.
                job.note = "SIGTERM ignored, killed"
                with suppress(OSError):
                    process.kill()
            return
        job.returncode = code
        job.finished_at = time.time()
        state = read_progress(job.progress_path)
        if state is not None and state.report_dir:
            job.report_dir = Path(state.report_dir)
        if job.state == "cancelling" or code in (-signal.SIGTERM, -signal.SIGKILL):
            job.state = "cancelled"
        else:
            job.state = "done" if code == 0 else "failed"
        self._file_the_sidecars(job)

    def _file_the_sidecars(self, job: Job) -> None:
        """Put the run's log and the lab's own note beside the report it produced.

        `summary.json` identifies a run by the hash of its resolved config, which is right
        for reproducibility and useless for reading a list. `lab.json` is the lab saying
        which file it launched and what that file overrode.

        The report directory is `<--out>/<timestamp>_<config hash>`, and the timestamp and
        the hash are both decided when the report is written, so the lab cannot know the
        path at launch: it can only move the note there once the run says where it went.
        Both files are therefore written into the work directory from the start and moved
        here, and `_file_orphans` does the moving for a run the lab did not live to reap.
        """
        if job.report_dir is None:
            return
        try:
            job.report_dir.mkdir(parents=True, exist_ok=True)
            job.sidecar_path.write_bytes(
                orjson.dumps(self._sidecar(job), option=orjson.OPT_INDENT_2)
            )
            _move(job.log_path, job.report_dir / "run.log")
            _move(job.sidecar_path, job.report_dir / "lab.json")
            job.log_path = job.report_dir / "run.log"
            job.sidecar_path = job.report_dir / "lab.json"
        except OSError:
            return

    def _file_orphans(self) -> None:
        """File what a lab that was closed mid-run left behind, on the next start.

        A run outlives the lab on purpose. When it finishes it writes its report and stamps
        the directory into its own progress file, and that file is still in the work
        directory, so the next lab has everything it needs to finish the job the last one
        started. Without this, Results falls back to showing the config hash.
        """
        for progress_path in sorted(self.work_dir.glob("*.progress.json")):
            state = read_progress(progress_path)
            if state is None or not state.report_dir:
                continue
            report_dir = Path(state.report_dir)
            if not report_dir.is_dir():
                continue
            job_id = progress_path.name.removesuffix(".progress.json")
            with suppress(OSError):
                _move(self.work_dir / f"{job_id}.log", report_dir / "run.log")
                _move(self.work_dir / f"{job_id}.lab.json", report_dir / "lab.json")

    def _sidecar(self, job: Job) -> dict[str, Any]:
        from liqfade.lab.configs import LabConfigError, read_config

        name, notes, overrides = job.spec.config.stem, "", {}
        try:
            config = read_config(job.spec.config)
        except (LabConfigError, OSError):
            pass
        else:
            name, notes, overrides = config.name, config.notes, config.overrides
        return {
            "job_id": job.id,
            "config_name": name,
            "config_path": str(job.spec.config),
            "notes": notes,
            "overrides": overrides,
            "coins": list(job.spec.coins),
            "command": job.spec.command(progress=job.progress_path),
            "started_at": job.started_at,
            "finished_at": job.finished_at,
        }

    def cancel(self, job: Job) -> None:
        """SIGTERM a running job; a queued one never starts. Both end as cancelled."""
        if job.state == "queued":
            job.state = "cancelled"
            job.note = "cancelled before it started"
            return
        if job.state != "running" or job.process is None:
            return
        job.state = "cancelling"
        job.cancelled_at = time.time()
        job.note = "SIGTERM sent"
        try:
            job.process.terminate()
        except OSError:
            job.state = "cancelled"
