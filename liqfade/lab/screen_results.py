"""The Results screen: what the runs under `reports/` came to.

Open renders `report.md`, which is the page the engine wrote and the one the owner reads
anyway; the funnel per rule comes from `summary.json` beside it, because a pass rate is a
column and markdown is prose. Two marked runs go side by side with the diff of the configs
that produced them. Promote takes a short run and queues the same config over the year,
which is the loop this whole tool exists to shorten.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Markdown, Static

from liqfade.lab.results import (
    RunRow,
    compare_metrics,
    config_diff,
    filter_runs,
    list_runs,
    promote,
    sort_runs,
)
from liqfade.lab.runs import RunQueue

SORTS = ("run_id", "config", "net", "trades", "hit_rate", "max_drawdown_frac", "wall_clock_s")


def _num(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.4g}"
    return str(value)


class ReportScreen(ModalScreen[None]):
    """`report.md` as the engine wrote it, with the per-rule funnel beside it."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape,q", "close", "close")]

    def __init__(self, row: RunRow) -> None:
        super().__init__()
        self.row = row

    def compose(self) -> ComposeResult:
        try:
            markdown = (self.row.directory / "report.md").read_text()
        except OSError:
            markdown = f"no report.md under {self.row.directory}"
        funnel = Text()
        funnel.append("rule                  evaluated   passed   killed  required\n", style="bold")
        for name, stats in self.row.rules.items():
            funnel.append(
                f"{name:<20} {stats.get('evaluated', 0):>10} "
                f"{stats.get('passed', 0):>8} {stats.get('killed', 0):>8}"
                f"  {'yes' if stats.get('required') else 'no'}\n"
            )
        if not self.row.rules:
            funnel.append("no rules ran in this configuration\n", style="dim")
        with VerticalScroll(id="report-box"):
            yield Static(funnel, id="report-funnel")
            yield Markdown(markdown, id="report-body")

    def action_close(self) -> None:
        self.dismiss(None)


class CompareScreen(ModalScreen[None]):
    """Two runs' numbers, then the keys their configs disagree on."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape,q", "close", "close")]

    def __init__(self, left: RunRow, right: RunRow) -> None:
        super().__init__()
        self.left = left
        self.right = right

    def compose(self) -> ComposeResult:
        text = Text()
        text.append(f"{'':<18}{self.left.config:>22}{self.right.config:>22}\n", style="bold")
        text.append(f"{'':<18}{self.left.run_id:>22}{self.right.run_id:>22}\n\n", style="dim")
        for label, left, right in compare_metrics(self.left, self.right):
            text.append(f"{label:<18}{_num(left):>22}{_num(right):>22}\n")
        text.append("\nconfig\n", style="bold")
        diff = config_diff(self.left, self.right)
        if not diff:
            text.append(
                "the same overrides, or the lab did not launch both of these runs\n", style="dim"
            )
        for key, left, right in diff:
            text.append(f"{key:<18}{_num(left):>22}{_num(right):>22}\n", style="cyan")
        with VerticalScroll(id="compare-box"):
            yield Static(text, id="compare-body")

    def action_close(self) -> None:
        self.dismiss(None)


class ResultsPane(VerticalScroll):
    """The run list, sorted and filtered, with open, compare and promote on it."""

    FOCUS_FIRST = "#results-table"
    BINDINGS: ClassVar[list[BindingType]] = [
        ("o", "open", "open"),
        ("space", "mark", "mark"),
        ("c", "compare", "compare"),
        ("p", "promote", "promote to the year"),
        ("s", "sort", "sort"),
        ("i", "invert", "reverse"),
        ("r", "refresh", "refresh"),
        ("slash", "focus_filter", "filter"),
    ]

    def __init__(self, reports_dir: Path, data_dir: Path, queue: RunQueue) -> None:
        super().__init__(id="results-pane")
        self.reports_dir = reports_dir
        self.data_dir = data_dir
        self.queue = queue
        self.rows: list[RunRow] = []
        self.marked: list[str] = []
        self.sort_key = "run_id"
        self.reverse = True

    def compose(self) -> ComposeResult:
        yield Input(placeholder="filter: config, coin, trigger, run id", id="results-filter")
        table: DataTable[Any] = DataTable(id="results-table", cursor_type="row")
        table.add_columns(
            "",
            "config",
            "window",
            "coins",
            "trades",
            "net",
            "hit",
            "worst dd",
            "holdout",
            "lagged",
            "wall s",
            "state",
        )
        yield table
        yield Static("", id="results-status")

    def on_mount(self) -> None:
        self.action_refresh()

    def action_refresh(self) -> None:
        table = self.query_one("#results-table", DataTable)
        cursor = table.cursor_row
        table.clear()
        rows = list_runs(self.reports_dir)
        rows = filter_runs(rows, self.query_one("#results-filter", Input).value)
        self.rows = sort_runs(rows, self.sort_key, reverse=self.reverse)
        for row in self.rows:
            table.add_row(
                "*" if row.run_id in self.marked else " ",
                row.config,
                row.window,
                ",".join(row.coins) or "core",
                row.trades,
                f"{row.net:,.2f}",
                f"{row.hit_rate:.0%}",
                f"{row.max_drawdown_frac:.1%}",
                f"{row.holdout_net:,.2f}",
                _num(row.lagged_net),
                _num(row.wall_clock_s),
                "done" if row.finished else "unfinished",
            )
        if self.rows:
            table.move_cursor(row=min(cursor, len(self.rows) - 1))
        self._say(
            f"{len(self.rows)} runs, sorted by {self.sort_key}{' desc' if self.reverse else ''}"
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        self.action_refresh()

    @property
    def selected(self) -> RunRow | None:
        table = self.query_one("#results-table", DataTable)
        if not self.rows:
            return None
        return self.rows[min(table.cursor_row, len(self.rows) - 1)]

    def _say(self, message: str) -> None:
        self.query_one("#results-status", Static).update(message)

    def action_open(self) -> None:
        row = self.selected
        if row is not None:
            self.app.push_screen(ReportScreen(row))

    def action_mark(self) -> None:
        row = self.selected
        if row is None:
            return
        if row.run_id in self.marked:
            self.marked.remove(row.run_id)
        else:
            # Two at a time: a third mark drops the oldest, so the pair is always the last
            # two the owner touched and there is no separate clear step.
            self.marked = [*self.marked, row.run_id][-2:]
        self.action_refresh()

    def action_compare(self) -> None:
        pair = [row for row in self.rows if row.run_id in self.marked]
        if len(pair) != 2:
            self._say("mark two runs with space first")
            return
        self.app.push_screen(CompareScreen(pair[0], pair[1]))

    def action_promote(self) -> None:
        row = self.selected
        if row is None:
            return
        spec = promote(row, reports_dir=self.reports_dir, data_dir=self.data_dir)
        if spec is None:
            self._say(
                "[red]this run has no lab.json, so which config produced it is not "
                "recorded; launch it from the Run tab[/red]"
            )
            return
        job = self.queue.submit(spec)
        self.queue.poll()
        self._say(f"queued {job.id}: {spec.label}")

    def action_focus_filter(self) -> None:
        self.query_one("#results-filter", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the filter box hands the keyboard back to the list it filtered."""
        self.query_one("#results-table", DataTable).focus()

    def action_sort(self) -> None:
        self.sort_key = SORTS[(SORTS.index(self.sort_key) + 1) % len(SORTS)]
        self.action_refresh()

    def action_invert(self) -> None:
        self.reverse = not self.reverse
        self.action_refresh()
