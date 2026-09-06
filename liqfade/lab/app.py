"""The `liqfade lab` application: four tabs, keyboard first.

Each tab is a plain widget over a small model layer, so the logic is testable without a
terminal and the widgets only have to place it on the screen. Nothing here runs a backtest
in process: the Run tab spawns `liqfade backtest`.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.widgets import Footer, Header, TabbedContent, TabPane

from liqfade.lab.screen_configs import ConfigsPane

LAB_CSS = """
Screen { layout: vertical; }
#configs-table { height: 1fr; min-height: 8; }
#configs-status { color: $text-muted; padding: 0 1; }
ConfigViewScreen, ConfigEditScreen, ConfirmScreen, ConfigNameScreen {
    align: center middle;
}
#config-view-box, #edit-box, #confirm-box, #name-box {
    width: 90%;
    max-height: 90%;
    border: round $accent;
    background: $surface;
    padding: 1 2;
}
#confirm-box, #name-box { height: auto; max-height: 12; }
#edit-errors { color: $error; }
.field-row { height: 3; }
.field-key { width: 34; padding: 1 1 0 0; }
.field-input { width: 1fr; }
.field-help { color: $text-muted; padding: 0 0 1 34; }
"""


class LabApp(App[None]):
    """The workbench. `config_dir` and `reports_dir` are injected so tests get a sandbox."""

    CSS = LAB_CSS
    TITLE = "liqfade lab"
    BINDINGS: ClassVar[list[BindingType]] = [
        ("1", "tab('configs')", "configs"),
        ("2", "tab('run')", "run"),
        ("3", "tab('results')", "results"),
        ("4", "tab('status')", "status"),
        ("q", "quit", "quit"),
    ]

    def __init__(
        self,
        *,
        config_dir: Path,
        reports_dir: Path,
        data_dir: Path | None = None,
    ) -> None:
        super().__init__()
        self.config_dir = Path(config_dir)
        self.reports_dir = Path(reports_dir)
        self.data_dir = Path(data_dir) if data_dir else Path("data")

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="tabs", initial="tab-configs"), TabPane("Configs", id="tab-configs"):
            yield ConfigsPane(self.config_dir)
        yield Footer()

    def action_tab(self, name: str) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        if f"tab-{name}" in {pane.id for pane in tabs.query(TabPane)}:
            tabs.active = f"tab-{name}"
