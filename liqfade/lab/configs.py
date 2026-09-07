"""Strategy configs: the files under `config/backtest/`, read and written by the lab.

A config is a YAML layer `liqfade backtest --config` merges over `config/default.yaml`, so
the lab only ever stores the keys the owner changed. Two header comments carry what the
filename cannot: `# name:` is what the run list shows, `# notes:` is why the file exists.
Neither is config; the loader ignores comments.

The writer emits each key with the model's own description above it, so a file the lab
saves reads like the two the owner wrote by hand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from liqfade.config import flatten
from liqfade.lab.form import _tree, descriptions

__all__ = [
    "LabConfig",
    "LabConfigError",
    "delete_config",
    "duplicate_config",
    "list_configs",
    "read_config",
    "render_config",
    "save_config",
]

# The layer files the loader owns. The lab writes strategy configs and nothing else, so a
# save aimed at one of these is refused by name rather than by directory: CLAUDE.md rule
# on mainnet risk limits does not depend on where the lab happens to be pointed.
RESERVED_STEMS = frozenset({"default", "paper", "testnet", "mainnet", "overrides"})
_PLAIN = re.compile(r"[A-Za-z_][A-Za-z0-9_./-]*")
_YAML_KEYWORDS = frozenset({"null", "true", "false", "yes", "no", "on", "off", "none", "~"})


class LabConfigError(RuntimeError):
    """A config the lab refuses to read or write."""


@dataclass(frozen=True)
class LabConfig:
    """One file under `config/backtest/`, as the configs screen shows it."""

    path: Path
    name: str
    notes: str
    mtime: float
    overrides: dict[str, Any]

    @property
    def keys(self) -> list[str]:
        return sorted(self.overrides)


def _header(text: str) -> tuple[str, str]:
    """`# name:` and `# notes:` from the leading comment block, empty when absent."""
    name = notes = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("#"):
            break
        body = stripped.lstrip("#").strip()
        lowered = body.lower()
        if lowered.startswith("name:"):
            name = body[len("name:") :].strip()
        elif lowered.startswith("notes:"):
            notes = body[len("notes:") :].strip()
    return name, notes


def read_config(path: Path) -> LabConfig:
    """One config file. Raises `LabConfigError` when the YAML will not parse."""
    path = Path(path)
    text = path.read_text()
    try:
        loaded = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise LabConfigError(f"{path.name}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise LabConfigError(f"{path.name}: top level must be a mapping")
    name, notes = _header(text)
    return LabConfig(
        path=path,
        name=name or path.stem,
        notes=notes,
        mtime=path.stat().st_mtime,
        overrides=flatten(loaded),
    )


def list_configs(directory: Path) -> list[LabConfig]:
    """Every readable `*.yaml` under `directory`, by name. Unreadable files are skipped."""
    configs = []
    for path in sorted(Path(directory).glob("*.yaml")):
        try:
            configs.append(read_config(path))
        except (LabConfigError, OSError):
            continue
    return sorted(configs, key=lambda config: config.name.lower())


def _scalar_text(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_scalar_text(item) for item in value) + "]"
    text = str(value)
    if _PLAIN.fullmatch(text) and text.lower() not in _YAML_KEYWORDS:
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _render_tree(node: dict[str, Any], descs: dict[str, str], prefix: str, depth: int) -> list[str]:
    lines: list[str] = []
    pad = "  " * depth
    for key, value in node.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            lines.append(f"{pad}{key}:")
            lines.extend(_render_tree(value, descs, f"{dotted}.", depth + 1))
            continue
        description = descs.get(dotted)
        if description:
            lines.append(f"{pad}# {description}")
        lines.append(f"{pad}{key}: {_scalar_text(value)}")
    return lines


def render_config(name: str, notes: str, overrides: dict[str, Any]) -> str:
    """The YAML text for a config: the header, then only the keys that were overridden."""
    lines = [f"# name: {name}"]
    if notes:
        lines.append(f"# notes: {notes}")
    lines.append("")
    lines.extend(_render_tree(_tree(overrides), descriptions(), "", 0))
    return "\n".join(lines) + "\n"


def save_config(path: Path, name: str, notes: str, overrides: dict[str, Any]) -> Path:
    """Write a config and return its path. Refuses the loader's own layer files."""
    path = Path(path).resolve()
    if path.suffix != ".yaml":
        raise LabConfigError(f"{path.name}: a strategy config must be a .yaml file")
    _refuse_reserved(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_config(name, notes, overrides))
    return path


def slug(name: str) -> str:
    """A filename stem from a display name: lowercase, one underscore per run of junk."""
    stem = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return stem or "config"


def duplicate_config(path: Path, new_name: str) -> Path:
    """Copy a config's overrides under a new name. Never overwrites."""
    source = read_config(Path(path))
    target = source.path.with_name(f"{slug(new_name)}.yaml")
    if target.exists():
        raise FileExistsError(f"{target.name} already exists")
    provenance = f"copied from {source.path.name}"
    notes = f"{source.notes}; {provenance}" if source.notes else provenance
    return save_config(target, new_name, notes, source.overrides)


def _refuse_reserved(path: Path) -> None:
    """Raise if this is one of the loader's own layer files, by name.

    By name and not by directory, because `liqfade lab --config-dir config` is a legitimate
    way to run the lab and it puts `mainnet.yaml` one keystroke from the cursor. CLAUDE.md's
    rule about mainnet risk limits cannot depend on where the lab happens to be pointed.
    """
    if path.stem in RESERVED_STEMS:
        raise LabConfigError(
            f"{path.stem}.yaml is a config layer the loader owns; the lab does not touch it"
        )


def delete_config(path: Path) -> None:
    """Remove a config. The screen asks twice before calling this, and so does `save_config`
    about the same list of names: a file the lab may not write is a file it may not
    unlink."""
    path = Path(path).resolve()
    _refuse_reserved(path)
    path.unlink()
