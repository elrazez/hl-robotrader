"""The edit form, derived from the `Settings` model rather than listed.

Every field the lab offers is walked out of the pydantic models in `config/models.py`,
carrying the model's own description, type and validators. A knob added to a model appears
in the lab with no change here, and an invalid value is refused by the same validation a
YAML file would meet, with the model's message.
"""

from __future__ import annotations

import types
from dataclasses import dataclass
from typing import Any, Literal, Union, get_args, get_origin

import yaml
from pydantic import BaseModel, ValidationError

from liqfade.config import flatten, resolve
from liqfade.config.loader import _set_dotted
from liqfade.config.models import Settings

__all__ = [
    "LAB_SECTIONS",
    "LabField",
    "LabValueError",
    "lab_fields",
    "parse_value",
    "validate",
    "walk",
]

# The sections a backtest reads. `venues`, `paths`, `execution`, `control`, `panel`,
# `telegram` and `sync` are deliberately absent: they configure processes the lab does not
# run, and two of them hold the addresses money moves over.
LAB_SECTIONS = (
    "features",
    "cascade",
    "exhaustion",
    "intent",
    "strategy",
    "filters",
    "sizing",
    "risk",
    "backtest",
)


class LabValueError(ValueError):
    """A typed box the owner could not have meant. Shown in red beside the field."""


@dataclass(frozen=True)
class LabField:
    """One editable leaf. `default` is the value the resolved config already has."""

    key: str
    description: str
    default: Any
    kind: str
    choices: tuple[str, ...] = ()
    optional: bool = False

    @property
    def section(self) -> str:
        return self.key.split(".", 1)[0]


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    """`X | None` -> (X, True). Anything else -> (it, False)."""
    if get_origin(annotation) in (Union, types.UnionType):
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(args) == 1:
            return args[0], True
    return annotation, False


def _kind(annotation: Any) -> tuple[str, tuple[str, ...]]:
    if get_origin(annotation) is Literal:
        return "choice", tuple(str(arg) for arg in get_args(annotation))
    if annotation is bool:
        return "bool", ("true", "false")
    if annotation is int:
        return "int", ()
    if annotation is float:
        return "float", ()
    if get_origin(annotation) in (tuple, list):
        return "list", ()
    return "str", ()


def walk(
    model: type[BaseModel], prefix: str = "", defaults: dict[str, Any] | None = None
) -> list[LabField]:
    """Every editable leaf of `model`, depth first, with dotted keys.

    `defaults` maps dotted keys to the resolved config's values; a key missing from it
    falls back to the model's own default, which is what a fresh field has.
    """
    fields: list[LabField] = []
    for name, info in model.model_fields.items():
        key = f"{prefix}{name}"
        annotation, optional = _unwrap_optional(info.annotation)
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            fields.extend(walk(annotation, f"{key}.", defaults))
            continue
        kind, choices = _kind(annotation)
        default = None if info.is_required() else info.get_default(call_default_factory=True)
        if defaults is not None and key in defaults:
            default = defaults[key]
        fields.append(
            LabField(
                key=key,
                description=info.description or "",
                default=default,
                kind=kind,
                choices=choices,
                optional=optional,
            )
        )
    return fields


def resolved_defaults(env: str = "paper") -> dict[str, Any]:
    """The config a backtest starts from, flattened to dotted keys."""
    merged, _ = resolve(env)
    return flatten(merged)


def lab_fields(env: str = "paper") -> list[LabField]:
    """Every field the lab lets the owner edit, in `LAB_SECTIONS` order."""
    defaults = resolved_defaults(env)
    return [field for field in walk(Settings, defaults=defaults) if field.section in LAB_SECTIONS]


def descriptions() -> dict[str, str]:
    """Dotted key -> the model's description, for the comments the writer emits."""
    return {field.key: field.description for field in walk(Settings) if field.description}


def _tree(overrides: dict[str, Any]) -> dict[str, Any]:
    tree: dict[str, Any] = {}
    for key, value in overrides.items():
        _set_dotted(tree, key, value)
    return tree


def parse_value(field: LabField, raw: str) -> Any:
    """Text from a box to the value a YAML file would have held.

    Parsed by `yaml.safe_load`, the same reader the config loader uses, so `true`, `null`,
    `1e-3` and a bare list all mean here what they mean in the file. Type checking is left
    to the model: `validate` reports its message rather than this function guessing one.
    """
    text = raw.strip()
    if text == "":
        raise LabValueError(
            f"{field.key} is empty; an empty box leaves the key out of the file, "
            "and a null override is spelled `null`"
        )
    if field.kind == "list":
        return [_scalar(part) for part in text.strip("[]").split(",") if part.strip()]
    return _scalar(text)


def _scalar(text: str) -> Any:
    try:
        return yaml.safe_load(text.strip())
    except yaml.YAMLError as exc:
        raise LabValueError(str(exc)) from exc


def validate(overrides: dict[str, Any], env: str = "paper") -> dict[str, str]:
    """Dotted key -> the model's complaint, empty when the config is good.

    The overrides are merged onto the same layers `liqfade backtest --config` would merge
    them onto and handed to `Settings`, so what passes here is exactly what the run will
    accept. A cross-field validator has no single field to blame, so it is reported against
    the section it guards.
    """
    merged, _ = resolve(env)
    for key, value in overrides.items():
        _set_dotted(merged, key, value)
    try:
        Settings.model_validate(merged)
    except ValidationError as exc:
        return {
            ".".join(str(part) for part in error["loc"]): error["msg"] for error in exc.errors()
        }
    return {}
