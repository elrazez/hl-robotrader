"""The Configs screen: the strategy configs under `config/backtest/`, and the form.

The form is built by walking the `Settings` model, so it is never a list of knobs kept in
step by hand: a field added to `config/models.py` shows up here on the next start. Saving
validates through the same model a YAML file meets and writes only the keys whose value
differs from the resolved default, which is what makes the file a layer rather than a copy.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Collapsible, DataTable, Input, Label, Static

from liqfade.lab.configs import (
    LabConfig,
    LabConfigError,
    delete_config,
    duplicate_config,
    list_configs,
    read_config,
    save_config,
    slug,
)
from liqfade.lab.form import LabField, LabValueError, lab_fields, parse_value, validate


# Textual widget ids may not contain a dot, and every field key does.
def field_id(key: str) -> str:
    return "field-" + key.replace(".", "-")


def _ago(mtime: float) -> str:
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")


class ConfirmScreen(ModalScreen[bool]):
    """One yes/no question. `y` and `n` are the answers; escape is no."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("y", "yes", "yes"),
        ("n", "no", "no"),
        ("escape", "no", "cancel"),
    ]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="confirm-box"):
            yield Static(self.question, id="confirm-question")
            yield Static("[b]y[/b] yes    [b]n[/b] no", id="confirm-keys")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class ConfigViewScreen(ModalScreen[None]):
    """What this config overrides, against the resolved defaults, with the descriptions."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape,q", "dismiss_view", "close")]

    def __init__(self, config: LabConfig) -> None:
        super().__init__()
        self.config = config

    def compose(self) -> ComposeResult:
        fields = {field.key: field for field in lab_fields()}
        text = Text()
        text.append(f"{self.config.name}\n", style="bold")
        if self.config.notes:
            text.append(f"{self.config.notes}\n", style="dim")
        text.append(f"{self.config.path}\n\n", style="dim")
        if not self.config.overrides:
            text.append("overrides nothing: this run is config/default.yaml\n", style="dim")
        for key in self.config.keys:
            value = self.config.overrides[key]
            field = fields.get(key)
            default = field.default if field else None
            text.append(f"{key}\n", style="bold cyan")
            text.append(f"    {value!r}", style="green")
            text.append(f"    (default {default!r})\n", style="dim")
            if field and field.description:
                text.append(f"    {field.description}\n", style="dim italic")
        with VerticalScroll(id="config-view-box"):
            yield Static(text, id="config-view")

    def action_dismiss_view(self) -> None:
        self.dismiss(None)


class ConfigEditScreen(ModalScreen[bool]):
    """A form generated from the model. ctrl+s saves, escape leaves the file alone."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("ctrl+s", "save", "save"),
        ("escape", "cancel", "cancel"),
    ]

    def __init__(self, config: LabConfig) -> None:
        super().__init__()
        self.config = config
        self.fields = lab_fields()
        # Keys the file sets that the form does not cover (`env`, say) are carried through
        # untouched rather than dropped, so editing a config never silently loses a line.
        covered = {field.key for field in self.fields}
        self.passthrough = {
            key: value for key, value in config.overrides.items() if key not in covered
        }

    def field(self, key: str) -> Input:
        return self.query_one(f"#{field_id(key)}", Input)

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="edit-box"):
            yield Static(f"editing [b]{self.config.name}[/b]  {self.config.path.name}")
            yield Static("", id="edit-errors")
            sections: dict[str, list[LabField]] = {}
            for field in self.fields:
                sections.setdefault(field.section, []).append(field)
            for section, fields in sections.items():
                changed = sum(1 for f in fields if f.key in self.config.overrides)
                title = f"{section}  ({changed} overridden)" if changed else section
                with Collapsible(title=title, collapsed=not changed):
                    for field in fields:
                        yield from self._row(field)

    def _row(self, field: LabField) -> ComposeResult:
        value = self.config.overrides.get(field.key, field.default)
        with Horizontal(classes="field-row"):
            yield Label(field.key.split(".", 1)[1], classes="field-key")
            yield Input(value=_as_text(value), id=field_id(field.key), classes="field-input")
        hint = field.description
        if field.choices:
            hint = f"{hint}  one of: {', '.join(field.choices)}"
        yield Static(hint, classes="field-help")

    def _collect(self) -> tuple[dict[str, Any], dict[str, str]]:
        """The overrides the form describes, and any box that would not parse."""
        overrides = dict(self.passthrough)
        errors: dict[str, str] = {}
        for field in self.fields:
            raw = self.field(field.key).value.strip()
            if raw == "":
                # An empty box means the key is not in this layer at all, and clearing a
                # box is how a key is removed from the file. A null override is spelled
                # `null`, which is what `cascade.liq_z_min: null` needs.
                continue
            try:
                value = parse_value(field, raw)
            except LabValueError as exc:
                errors[field.key] = str(exc)
                continue
            # A key the file already had stays in the file, even where it now matches the
            # resolved default. Dropping those was silently rewriting a config to a
            # fraction of itself: sixteen of v1_confluence.yaml's keys sit on a default
            # today, and the file exists to record what the owner chose, not to be a diff
            # against whatever `config/default.yaml` happens to say next month.
            if value != field.default or field.key in self.config.overrides:
                overrides[field.key] = value
        return overrides, errors

    def action_save(self) -> None:
        overrides, errors = self._collect()
        errors = {**errors, **validate(overrides)}
        if errors:
            self._show(errors)
            return
        try:
            save_config(self.config.path, self.config.name, self.config.notes, overrides)
        except LabConfigError as exc:
            self._show({self.config.path.name: str(exc)})
            return
        self.dismiss(True)

    def _show(self, errors: dict[str, str]) -> None:
        text = Text()
        for key, message in errors.items():
            text.append(f"{key}: ", style="bold red")
            text.append(f"{message}\n", style="red")
        self.query_one("#edit-errors", Static).update(text)

    def action_cancel(self) -> None:
        self.dismiss(False)


class ConfigNameScreen(ModalScreen[str | None]):
    """Ask for the new name a duplicate gets. Enter accepts, escape backs out."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "cancel", "cancel")]

    def __init__(self, prompt: str, value: str = "") -> None:
        super().__init__()
        self.prompt = prompt
        self.start = value

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="name-box"):
            yield Static(self.prompt)
            yield Input(value=self.start, id="name-input")

    def on_mount(self) -> None:
        self.query_one("#name-input", Input).focus()

    @on(Input.Submitted)
    def submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfigsPane(VerticalScroll):
    """The list, and the keys that act on the row under the cursor."""

    FOCUS_FIRST = "#configs-table"
    BINDINGS: ClassVar[list[BindingType]] = [
        ("v", "view", "view"),
        ("e", "edit", "edit"),
        ("d", "duplicate", "duplicate"),
        ("x", "delete", "delete"),
        ("r", "refresh", "refresh"),
    ]

    def __init__(self, config_dir: Path) -> None:
        super().__init__(id="configs-pane")
        self.config_dir = config_dir
        self.configs: list[LabConfig] = []

    def compose(self) -> ComposeResult:
        table: DataTable[Any] = DataTable(id="configs-table", cursor_type="row")
        table.add_columns("name", "notes", "modified", "overrides")
        yield table
        yield Static("", id="configs-status")

    def on_mount(self) -> None:
        self.action_refresh()
        self.query_one("#configs-table", DataTable).focus()

    def action_refresh(self) -> None:
        table = self.query_one("#configs-table", DataTable)
        cursor = table.cursor_row
        table.clear()
        self.configs = list_configs(self.config_dir)
        for config in self.configs:
            table.add_row(
                config.name,
                config.notes[:48],
                _ago(config.mtime),
                ", ".join(config.keys[:4]) + (" ..." if len(config.keys) > 4 else ""),
            )
        if self.configs:
            table.move_cursor(row=min(cursor, len(self.configs) - 1))
        self._say(f"{len(self.configs)} configs in {self.config_dir}")

    def _say(self, message: str) -> None:
        self.query_one("#configs-status", Static).update(message)

    @property
    def selected(self) -> LabConfig | None:
        table = self.query_one("#configs-table", DataTable)
        if not self.configs:
            return None
        return self.configs[min(table.cursor_row, len(self.configs) - 1)]

    def action_view(self) -> None:
        config = self.selected
        if config is not None:
            self.app.push_screen(ConfigViewScreen(config))

    def action_edit(self) -> None:
        config = self.selected
        if config is None:
            return

        def done(saved: bool | None) -> None:
            self.action_refresh()
            if saved:
                self._say(f"saved {config.path.name}")

        self.app.push_screen(ConfigEditScreen(config), done)

    def action_duplicate(self) -> None:
        config = self.selected
        if config is None:
            return

        def named(name: str | None) -> None:
            if not name:
                return
            try:
                path = duplicate_config(config.path, name)
            except (FileExistsError, LabConfigError, OSError) as exc:
                self._say(f"[red]{exc}[/red]")
                return
            self.action_refresh()
            self._say(f"wrote {path.name}")

        self.app.push_screen(ConfigNameScreen("New name:", f"{config.name} copy"), named)

    def action_delete(self) -> None:
        config = self.selected
        if config is None:
            return

        def second(yes: bool | None) -> None:
            if not yes:
                self._say("kept")
                return
            try:
                delete_config(config.path)
            except (LabConfigError, OSError) as exc:
                self._say(f"[red]{exc}[/red]")
                return
            self.action_refresh()
            self._say(f"deleted {config.path.name}")

        def first(yes: bool | None) -> None:
            if not yes:
                self._say("kept")
                return
            self.app.push_screen(
                ConfirmScreen(
                    f"Really delete [b]{config.path.name}[/b]?\n"
                    "This removes the file. Reports already written keep their own copy.",
                ),
                second,
            )

        self.app.push_screen(ConfirmScreen(f"Delete config [b]{config.name}[/b]?"), first)


def _as_text(value: Any) -> str:
    """A value as the box shows it. None is the text `null`, never an empty box.

    An empty box means "this key is not in the file", which is a different statement from
    "this key is in the file and its value is null". `cascade.liq_z_min: null` is how v1
    says the z trigger is not applied at all, and showing it as empty made saving the file
    unchanged delete it and turn the trigger back on.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list | tuple):
        return ", ".join(str(item) for item in value)
    return str(value)


__all__ = ["ConfigsPane", "ConfirmScreen", "field_id", "read_config", "slug"]
