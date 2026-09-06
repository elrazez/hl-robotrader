"""The Run screen: pick a config, a window and a size, see the command, launch it.

The command is shown before anything is spawned, in the exact form it will be run, because
a run over the year is an hour of the machine and the owner should be able to read what it
is about to do. The progress panel calls `backtest/watch.py`'s `render`, so the lab shows
the same frame `liqfade watch` does and the two cannot drift apart.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Checkbox, DataTable, Input, Label, Select, Static

from liqfade.backtest.watch import render
from liqfade.lab.configs import list_configs
from liqfade.lab.runs import Job, RunQueue, RunSpec, lake_days, window_presets

TYPED = "typed"


def _int_or_none(raw: str) -> int | None:
    text = raw.strip()
    return int(text) if text.isdigit() else None


class RunPane(VerticalScroll):
    """The form, the queue and one progress frame for whatever is running."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("ctrl+r", "launch", "launch"),
        ("ctrl+t", "cancel", "terminate run"),
    ]

    def __init__(
        self,
        config_dir: Path,
        reports_dir: Path,
        data_dir: Path,
        queue: RunQueue,
        core_coins: tuple[str, ...],
    ) -> None:
        super().__init__(id="run-pane")
        self.config_dir = config_dir
        self.reports_dir = reports_dir
        self.data_dir = data_dir
        self.queue = queue
        self.core_coins = core_coins
        self.presets = dict(window_presets(lake_days(data_dir)))
        # Whatever the lake actually holds, when it holds anything: a run over days that
        # are not on disk fails at the first partition, and the reference days are only
        # there on the owner's machine.
        lake_windows = [name for name in self.presets if "lake" in name]
        self.default_window = lake_windows[0] if lake_windows else next(iter(self.presets), TYPED)

    def compose(self) -> ComposeResult:
        configs = list_configs(self.config_dir)
        options = [(config.name, str(config.path)) for config in configs]
        first = options[0][1] if options else Select.BLANK
        with Horizontal(classes="run-row"):
            yield Label("config", classes="run-key")
            yield Select(options, value=first, allow_blank=not options, id="run-config")
        with Horizontal(classes="run-row"):
            yield Label("coins", classes="run-key")
            yield Input(value=",".join(self.core_coins), id="run-coins")
        with Horizontal(classes="run-row"):
            yield Label("window", classes="run-key")
            yield Select(
                [(name, name) for name in self.presets] + [("typed below", TYPED)],
                value=self.default_window,
                id="run-window",
            )
        with Horizontal(classes="run-row"):
            yield Label("start", classes="run-key")
            yield Input(placeholder="YYYY-MM-DD", id="run-start")
            yield Label("end", classes="run-key")
            yield Input(placeholder="YYYY-MM-DD", id="run-end")
        with Horizontal(classes="run-row"):
            yield Label("equity", classes="run-key")
            yield Input(value="10000", id="run-equity")
            yield Label("latency ms", classes="run-key")
            yield Input(placeholder="config", id="run-latency")
            yield Label("lagged ms", classes="run-key")
            yield Input(placeholder="off", id="run-lagged")
        with Horizontal(classes="run-row"):
            yield Checkbox("feature cache", value=True, id="run-cache")
        yield Static("", id="run-command")
        yield Static("", id="run-status")
        yield Static("", id="run-progress")
        jobs: DataTable[Any] = DataTable(id="run-jobs", cursor_type="row")
        jobs.add_columns("job", "run", "state", "note")
        yield jobs

    def on_mount(self) -> None:
        self._apply_window(self.default_window)
        self.refresh_command()
        self.set_interval(1.0, self.tick)

    def _apply_window(self, name: str) -> None:
        window = self.presets.get(name)
        if window is None:
            return
        self.query_one("#run-start", Input).value = window[0]
        self.query_one("#run-end", Input).value = window[1]

    # --- the form -------------------------------------------------------------

    def spec(self) -> RunSpec | None:
        """What the form describes, or None when it does not yet describe a run."""
        chosen = self.query_one("#run-config", Select).value
        start = self.query_one("#run-start", Input).value.strip()
        end = self.query_one("#run-end", Input).value.strip()
        if chosen is Select.BLANK or not start or not end:
            return None
        coins = tuple(
            part.strip().upper()
            for part in self.query_one("#run-coins", Input).value.split(",")
            if part.strip()
        )
        try:
            equity = float(self.query_one("#run-equity", Input).value or 0)
        except ValueError:
            return None
        return RunSpec(
            config=Path(str(chosen)),
            coins=coins,
            start=start,
            end=end,
            equity=equity,
            latency_ms=_int_or_none(self.query_one("#run-latency", Input).value),
            lagged_ms=_int_or_none(self.query_one("#run-lagged", Input).value),
            cache=self.query_one("#run-cache", Checkbox).value,
            data_dir=self.data_dir,
            reports_dir=self.reports_dir,
        )

    def refresh_command(self) -> None:
        run = self.spec()
        widget = self.query_one("#run-command", Static)
        if run is None:
            widget.update(Text("pick a config and a window", style="dim"))
            return
        preview = self.reports_dir / ".lab" / "<job>.progress.json"
        widget.update(Text(run.command(progress=preview), style="bold"))

    @on(Select.Changed, "#run-window")
    def window_chosen(self, event: Select.Changed) -> None:
        self._apply_window(str(event.value))
        self.refresh_command()

    @on(Input.Changed)
    @on(Select.Changed, "#run-config")
    @on(Checkbox.Changed)
    def form_changed(self, event: object) -> None:
        self.refresh_command()

    # --- the queue ------------------------------------------------------------

    def action_launch(self) -> None:
        run = self.spec()
        if run is None:
            self._say("[red]pick a config, a start and an end first[/red]")
            return
        job = self.queue.submit(run)
        self._say(f"queued {job.id}: {run.label}")
        self.tick()

    @property
    def selected(self) -> Job | None:
        table = self.query_one("#run-jobs", DataTable)
        if not self.queue.jobs:
            return None
        return self.queue.jobs[min(table.cursor_row, len(self.queue.jobs) - 1)]

    def action_cancel(self) -> None:
        job = self.selected
        if job is None or not job.active:
            return
        self.queue.cancel(job)
        self._say(f"SIGTERM sent to {job.id}")
        self.tick()

    def tick(self) -> None:
        """One beat: advance the queue, redraw the table and the progress frame."""
        self.queue.poll()
        table = self.query_one("#run-jobs", DataTable)
        cursor = table.cursor_row
        table.clear()
        for job in self.queue.jobs:
            table.add_row(job.id, job.spec.label, job.state, job.note)
        if self.queue.jobs:
            table.move_cursor(row=min(cursor, len(self.queue.jobs) - 1))
        self._draw_progress()
        waiting = next((job for job in self.queue.jobs if job.state == "queued"), None)
        if waiting is not None and waiting.note:
            self._say(waiting.note)

    def _draw_progress(self) -> None:
        job = self.queue.running or next(
            (j for j in reversed(self.queue.jobs) if j.state != "queued"), None
        )
        widget = self.query_one("#run-progress", Static)
        if job is None:
            widget.update(Text("nothing running", style="dim"))
            return
        frame = render(
            job.progress(), time.time(), width=max(60, self.size.width - 4), path=job.progress_path
        )
        widget.update(Text.from_ansi(frame))

    def _say(self, message: str) -> None:
        self.query_one("#run-status", Static).update(message)
