"""The backtest's progress file, and the state `liqfade watch` draws from it.

The replay prints nothing until the report is written, and a year is minutes of silence
on a slow drive. This is one small JSON file, `reports/.progress.json` by default, that the
run rewrites at most once a second and that `liqfade watch` reads from another terminal.
Nothing in the engine waits on it: a write that fails is logged once and ignored, because
a progress bar must never be the reason a backtest dies.

Two fields describe where the run is. `phase` is which run of the command this is, `main`
or `lagged` (the `--lagged-ms` rerun); `stage` is what that run is doing: `loading`,
`features`, `replaying`, `report`, `done` or `failed`. `sim_ms` is the replay clock, so the
fraction done is `(sim_ms - window_start_ms) / (window_end_ms - window_start_ms)`.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Wall-clock seconds between writes. A year is a few million ticks; the file is for eyes.
WRITE_EVERY_S = 1.0
# Simulated milliseconds between checks of the wall clock, so the hot loop pays an
# integer comparison per tick and a `time.monotonic()` once a simulated minute.
CHECK_EVERY_SIM_MS = 60_000
PROGRESS_FILE = ".progress.json"


@dataclass
class ProgressState:
    """Everything the watcher shows. Times are unix seconds; the window is ms UTC."""

    phase: str = "main"
    stage: str = "starting"
    coins: list[str] = field(default_factory=list)
    window_start_ms: int = 0
    window_end_ms: int = 0
    sim_ms: int = 0
    starting_equity: float = 0.0
    equity: float = 0.0
    trades: int = 0
    net: float = 0.0
    intents: int = 0
    started_at: float = 0.0
    phase_started_at: float = 0.0
    updated_at: float = 0.0
    lagged_ms: int | None = None
    report_dir: str = ""
    error: str = ""
    pid: int = 0

    @property
    def fraction(self) -> float:
        """Share of the window replayed, 0 to 1. Zero until the replay has a clock."""
        span = self.window_end_ms - self.window_start_ms
        if span <= 0 or self.sim_ms <= self.window_start_ms:
            return 0.0
        return min(1.0, (self.sim_ms - self.window_start_ms) / span)


class ProgressWriter:
    """Writes `ProgressState` to `path` atomically, at most once a second.

    `path=None` disables it entirely, which is what the tests and the golden run use.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.state = ProgressState(
            started_at=time.time(), phase_started_at=time.time(), pid=os.getpid()
        )
        self._next_sim_ms = 0
        self._next_wall = 0.0
        self._failed = False

    def set(self, **fields: object) -> None:
        """Update fields and write now. Used at stage changes, not in the hot loop."""
        if "phase" in fields and fields["phase"] != self.state.phase:
            self.state.phase_started_at = time.time()
        for key, value in fields.items():
            setattr(self.state, key, value)
        self._write()

    def tick(self, sim_ms: int, *, trades: int, net: float, equity: float, intents: int) -> None:
        """Called from the replay loop. Cheap until a simulated minute has passed, then
        writes only if a wall-clock second has too."""
        if sim_ms < self._next_sim_ms:
            return
        self._next_sim_ms = sim_ms + CHECK_EVERY_SIM_MS
        now = time.monotonic()
        if now < self._next_wall:
            return
        self._next_wall = now + WRITE_EVERY_S
        self.state.sim_ms = sim_ms
        self.state.trades = trades
        self.state.net = net
        self.state.equity = equity
        self.state.intents = intents
        self._write()

    def _write(self) -> None:
        if self.path is None or self._failed:
            return
        self.state.updated_at = time.time()
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(asdict(self.state)))
            os.replace(tmp, self.path)
        except OSError:
            # The bar is not worth the run. Stop trying rather than fail every second.
            self._failed = True


def process_alive(pid: int) -> bool | None:
    """True or False when the pid can be checked, None when it is unknown (zero)."""
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_progress(path: Path) -> ProgressState | None:
    """The state on disk, or None when there is no file or it is mid-write and unreadable."""
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    try:
        return ProgressState(**payload)
    except TypeError:
        return None
