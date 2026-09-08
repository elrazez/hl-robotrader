"""Validated writes to the overrides layer, which is the only config the panel changes.

Four rules, all enforced here rather than in the browser, because a page can be reloaded
mid-flow and a curl has no browser at all:

1. **An allowlist of sections.** Not a blocklist. CLAUDE.md says nothing in the panel may
   enable live trading, and the way to be certain of that is that the endpoint cannot
   write anything it was not named for, rather than that someone remembered every key
   that would do it. `env` is refused here and refused again by the loader.
2. **A typed confirmation for anything that decides how much money is at risk.** The
   owner types the field's own name back. `sizing` counts: it decides position size,
   which is a risk limit whatever the section is called.
3. **Validation through the same model as the YAML**, on the whole resolved config rather
   than field by field, because the rules that matter most here are cross-field ones
   (`kill_budget_s < kill_deadline_s`, the stale ladder) and a per-field check cannot see
   them. A config that only fails at the next process start is a config written at 2am
   and discovered at 8.
4. **A key an env var already wins is refused.** The env layer merges last, so the write
   would land, the file would say one thing and the process would keep doing another.

The file itself lives under `data_dir`; `config/loader.py` says why.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import yaml

from liqfade.config import flatten, resolve
from liqfade.config.models import Settings

# The sections a settings page may write: sizing fractions, risk limits, the trigger
# thresholds (cascade, exhaustion, intent, strategy, filters) and the universe. Anything
# outside this set is a deploy, not a setting.
WRITABLE_SECTIONS = frozenset(
    {"cascade", "exhaustion", "filters", "intent", "risk", "sizing", "strategy", "universe"}
)
# Sections whose fields need their own name typed back before they are written.
CONFIRM_SECTIONS = frozenset({"risk", "sizing"})


class OverridesError(ValueError):
    """A change the panel refuses to write, with the reason the owner should read."""


def section_of(key: str) -> str:
    return key.split(".", 1)[0]


def needs_confirmation(key: str) -> bool:
    """True for a field that decides how much money is at risk."""
    return section_of(key) in CONFIRM_SECTIONS


def read_overrides(path: Path) -> dict[str, Any]:
    """The current overrides tree, or an empty one. A malformed file is an error here.

    The panel is this file's only writer, so anything unparseable in it means a hand
    edit or a torn write, and merging a change into a tree we could not read would
    silently drop whatever was already there.
    """
    try:
        loaded = yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError:
        return {}
    except (OSError, yaml.YAMLError) as exc:
        raise OverridesError(f"{path} could not be read: {exc}") from None
    if not isinstance(loaded, dict):
        raise OverridesError(f"{path} must be a mapping, got {type(loaded).__name__}")
    return loaded


def set_dotted(tree: dict[str, Any], dotted: str, value: Any) -> None:
    """Set one dotted key in a nested tree, creating the levels it needs."""
    parts = dotted.split(".")
    node = tree
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def diff(
    changes: dict[str, Any], flat: dict[str, Any], sources: dict[str, str]
) -> list[dict[str, Any]]:
    """Before, after and which layer set the before, one row per key, in key order.

    This is the preview the page shows and the record the audit entry keeps, built once
    so the two cannot disagree and neither can disagree with what is about to be written.
    """
    return [
        {
            "key": key,
            "before": flat.get(key),
            "after": changes[key],
            "source": sources.get(key),
        }
        for key in sorted(changes)
    ]


def check(changes: dict[str, Any], confirm: dict[str, str], sources: dict[str, str]) -> None:
    """Every refusal that can be decided without building a candidate config."""
    if not changes:
        raise OverridesError("no changes to write")

    unwritable = sorted(key for key in changes if section_of(key) not in WRITABLE_SECTIONS)
    if unwritable:
        allowed = ", ".join(sorted(WRITABLE_SECTIONS))
        raise OverridesError(
            f"not writable from the panel: {', '.join(unwritable)}. "
            f"Only these sections are: {allowed}. Everything else is a deploy."
        )

    unconfirmed = sorted(
        key for key in changes if needs_confirmation(key) and confirm.get(key) != key
    )
    if unconfirmed:
        raise OverridesError(
            "type the field name to confirm a change to a risk field: " + ", ".join(unconfirmed)
        )

    shadowed = sorted(key for key in changes if sources.get(key) == "envvar")
    if shadowed:
        raise OverridesError(
            f"an env var already sets {', '.join(shadowed)}, and the env layer wins over "
            "this file: the override would be written and never take effect. Change it "
            "in the box's env file instead."
        )


def validate(env: str, changes: dict[str, Any], merged: dict[str, Any]) -> None:
    """Build the config this write would produce and run it through the same model.

    Whole-model rather than field by field: the constraints that matter most here relate
    two fields to each other, and only validating the built tree can see those.
    """
    candidate = _deep_copy(merged)
    for key, value in changes.items():
        set_dotted(candidate, key, value)
    try:
        Settings.model_validate(candidate)
    except Exception as exc:
        raise OverridesError(f"the change does not validate: {exc}") from None
    if candidate.get("env") != env:
        # Unreachable while `env` is outside WRITABLE_SECTIONS, and kept because it is
        # the rule that matters most: nothing here may move the environment, which is
        # half of the live trading gate.
        raise OverridesError("the panel may not change the environment")


def _deep_copy(tree: dict[str, Any]) -> dict[str, Any]:
    return {k: _deep_copy(v) if isinstance(v, dict) else v for k, v in tree.items()}


def write(path: Path, changes: dict[str, Any], reason: str) -> dict[str, Any]:
    """Merge the changes into the overrides file and return the tree that was written.

    The timestamp and the reason go in as a header comment rather than as config keys:
    the file is loaded by a model that forbids unknown keys, so a `_meta` block would
    make the config the panel writes one the loader refuses. Written whole rather than
    appended, because it is a YAML document and not a log; the log is the audit file.
    """
    tree = read_overrides(path)
    for key, value in changes.items():
        set_dotted(tree, key, value)
    stamp = dt.datetime.now(tz=dt.UTC).isoformat(timespec="seconds")
    header = (
        "# Written by the liqfade panel. Edit through the panel, not by hand: every\n"
        "# change here is meant to have a timestamp, a reason and an audit entry.\n"
        f"# {stamp}  {reason}\n"
    )
    body = yaml.safe_dump(tree, sort_keys=True, default_flow_style=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    # Written to a sibling and renamed: a process reloading its config must see the old
    # file or the new one, never the half of a new one that has reached the disk.
    tmp.write_text(header + body)
    tmp.replace(path)
    return tree


def current(env: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    """The resolved config as (tree, flattened, per key source label)."""
    merged, sources = resolve(env)
    return merged, flatten(merged), sources
