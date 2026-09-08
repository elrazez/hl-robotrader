"""Layered config loading with per-key provenance.

Layer order, later wins:

    config/default.yaml -> config/{env}.yaml -> {data_dir}/overrides.yaml -> LIQFADE_ env vars

Each layer is deep merged into one dict while a parallel dict records, per dotted key,
which layer last wrote it. That provenance is what `liqfade config show` prints. We merge
by hand rather than using pydantic-settings because pydantic-settings resolves sources
internally and cannot report which one a value came from, which the panel needs in order
to show the owner whether a number is a default or something they changed.

Env var spelling: LIQFADE_SIZING__RISK_FRAC=0.004 sets sizing.risk_frac. The double
underscore separates levels; a single underscore is part of a key name. Values are parsed
as YAML scalars, so 0.004, true and [a, b] all work. LIQFADE_ENV is special cased to the
top level `env` key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from liqfade.config.models import Settings

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
ENV_PREFIX = "LIQFADE_"
# The one layer written at runtime, so it lives with the data rather than with the code.
# The panel writes it, and its unit is ProtectSystem=strict with only the data dir
# writable; the trading loop reads it from a different checkout entirely. A file under
# config/ would be unwritable by one and invisible to the other. docs/DECISIONS.md,
# 2026-09-08.
OVERRIDES_NAME = "overrides.yaml"
# Env vars that configure something other than a config key, so the loader must not try
# to map them onto the Settings tree. Secrets live here; they are read by the modules
# that need them, never merged into the config.
ENV_NOT_CONFIG = frozenset(
    {
        "LIQFADE_LIVE_TRADING",  # a hard gate checked in Settings.live_trading_enabled
        "LIQFADE_PANEL_PASSWORD_HASH",  # secret, read by liqfade.panel.auth
        "LIQFADE_PANEL_SESSION_SECRET",  # secret, read by liqfade.panel.auth
        "LIQFADE_CONFIG_DIR",  # points at this loader's own search path
    }
)
# Convenience aliases so common single-level env vars do not need the __ spelling.
ENV_ALIASES = {
    "LIQFADE_ENV": "env",
    "LIQFADE_DATA_DIR": "paths.data_dir",
    "LIQFADE_SYNC_HOST": "sync.host",
    "LIQFADE_CONTROL_SOCKET": "control.socket_path",
    "LIQFADE_LOG_LEVEL": "logging.level",
}


def overrides_file(*trees: dict[str, Any]) -> Path:
    """`overrides.yaml` under `paths.data_dir`, resolved from the layers given.

    Trees come in merge order, later winning, and are the layers other than this one:
    the file has to be located before it can be merged, so every other layer that could
    set `paths.data_dir` is consulted first. That includes the env layer, which merges
    last of all, because LIQFADE_DATA_DIR is how the box names its data dir.
    """
    data_dir: Any = "data"
    for tree in trees:
        node = tree.get("paths")
        if isinstance(node, dict) and node.get("data_dir"):
            data_dir = node["data_dir"]
    return Path(str(data_dir)) / OVERRIDES_NAME


class ConfigError(RuntimeError):
    """Raised when a config file is missing, malformed or fails validation."""


def config_dir() -> Path:
    """Directory holding the YAML layers. LIQFADE_CONFIG_DIR overrides it, for tests."""
    override = os.environ.get("LIQFADE_CONFIG_DIR")
    return Path(override) if override else CONFIG_DIR


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - malformed yaml is an operator error
        raise ConfigError(f"{path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path}: top level must be a mapping, got {type(loaded).__name__}")
    return loaded


def _merge(
    into: dict[str, Any],
    layer: dict[str, Any],
    sources: dict[str, str],
    label: str,
    prefix: str = "",
) -> None:
    """Deep merge `layer` into `into`, recording `label` as the source of every leaf set."""
    for key, value in layer.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            # Always recurse into mappings, even into a key this layer is the first to
            # define, so provenance is recorded per leaf rather than per subtree.
            if not isinstance(into.get(key), dict):
                into[key] = {}
            _merge(into[key], value, sources, label, f"{dotted}.")
        else:
            into[key] = value
            sources[dotted] = label


def _set_dotted(tree: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = tree
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def _env_layer(environ: dict[str, str]) -> dict[str, Any]:
    """Map LIQFADE_ env vars onto a nested dict. Unknown-shaped names are ignored."""
    layer: dict[str, Any] = {}
    for name, raw in sorted(environ.items()):
        if not name.startswith(ENV_PREFIX) or name in ENV_NOT_CONFIG:
            continue
        if name in ENV_ALIASES:
            dotted = ENV_ALIASES[name]
        elif "__" in name:
            dotted = name[len(ENV_PREFIX) :].lower().replace("__", ".")
        else:
            # A single-level LIQFADE_FOO that is not an alias is not a config key.
            continue
        if raw.strip() == "":
            # `KEY=` in an env file copied from .env.example means "not set", not
            # "set to nothing": yaml would read it as None and override the YAML value.
            continue
        try:
            value = yaml.safe_load(raw)
        except yaml.YAMLError:
            value = raw
        _set_dotted(layer, dotted, value)
    return layer


@dataclass(frozen=True, slots=True)
class ConfigLayer:
    """One source of configuration, and only what that source itself sets.

    `values` is dotted-key to value for this layer alone, not the merged result, which is
    what lets a reader say what the layer underneath an override will go back to saying.
    `exists` is False for a file that is not there; `overrides.yaml` legitimately is not
    until the panel writes one, and an empty layer is a different thing from a missing one.
    `path` is the absolute path the loader looked at, which for overrides.yaml is under
    data_dir rather than beside the other layers.
    """

    name: str
    path: str | None
    exists: bool
    values: dict[str, Any] = field(default_factory=dict)


def config_layers(
    env: str | None = None,
    *,
    extra_files: list[Path] | None = None,
    environ: dict[str, str] | None = None,
) -> list[ConfigLayer]:
    """Every layer in merge order, later winning, with what each one sets.

    The same list `resolve` folds, so the view a reader gets cannot disagree with the
    config the process runs on: there is one definition of the order.
    """
    return _layers(env, extra_files, dict(os.environ if environ is None else environ))[1]


def _layers(
    env: str | None, extra_files: list[Path] | None, environ: dict[str, str]
) -> tuple[str, list[ConfigLayer]]:
    """The chosen environment and the layers to merge, in order."""
    directory = config_dir()
    default_path = directory / "default.yaml"
    if not default_path.exists():
        raise ConfigError(f"missing {default_path}")

    default_tree = _read_yaml(default_path)
    layers = [ConfigLayer("default.yaml", str(default_path), True, flatten(default_tree))]

    chosen = env or environ.get("LIQFADE_ENV") or str(default_tree.get("env", "paper"))
    env_path = directory / f"{chosen}.yaml"
    if not env_path.exists():
        raise ConfigError(f"unknown environment {chosen!r}: no {env_path}")
    env_tree = _read_yaml(env_path)
    layers.append(ConfigLayer(env_path.name, str(env_path), True, flatten(env_tree)))

    trees = [default_tree, env_tree]
    for extra in extra_files or []:
        if not extra.exists():
            raise ConfigError(f"missing config file {extra}")
        extra_tree = _read_yaml(extra)
        trees.append(extra_tree)
        layers.append(ConfigLayer(extra.name, str(extra), True, flatten(extra_tree)))

    env_tree_layer = _env_layer(environ)
    overrides_path = overrides_file(*trees, env_tree_layer)
    layers.append(
        ConfigLayer(
            OVERRIDES_NAME,
            str(overrides_path),
            overrides_path.exists(),
            flatten(_read_yaml(overrides_path)),
        )
    )
    layers.append(ConfigLayer("envvar", None, True, flatten(env_tree_layer)))
    return chosen, layers


def resolve(
    env: str | None = None,
    *,
    extra_files: list[Path] | None = None,
    environ: dict[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Merge every layer and return (raw config dict, dotted key -> source label).

    `env` picks the environment file. When it is None it comes from LIQFADE_ENV, then
    from default.yaml. `extra_files` are merged after the environment file and before
    overrides.yaml; `liqfade backtest --config` uses that.
    """
    environ = dict(os.environ if environ is None else environ)
    directory = config_dir()

    default_path = directory / "default.yaml"
    if not default_path.exists():
        raise ConfigError(f"missing {default_path}")

    merged: dict[str, Any] = {}
    sources: dict[str, str] = {}
    _merge(merged, _read_yaml(default_path), sources, "default.yaml")

    chosen = env or environ.get("LIQFADE_ENV") or str(merged.get("env", "paper"))
    env_path = directory / f"{chosen}.yaml"
    if not env_path.exists():
        raise ConfigError(f"unknown environment {chosen!r}: no {env_path}")
    _merge(merged, _read_yaml(env_path), sources, env_path.name)

    for extra in extra_files or []:
        if not extra.exists():
            raise ConfigError(f"missing config file {extra}")
        _merge(merged, _read_yaml(extra), sources, extra.name)

    # overrides.yaml is written by the panel and may legitimately not exist. It lives
    # under data_dir, which `merged` and the env layer between them already decide.
    env_tree_layer = _env_layer(environ)
    _merge(merged, _read_yaml(overrides_file(merged, env_tree_layer)), sources, OVERRIDES_NAME)

    _merge(merged, env_tree_layer, sources, "envvar")

    # An explicit --env argument beats whatever the files said about `env`.
    if env is not None:
        merged["env"] = env
        sources["env"] = "argument"

    # Only --env and LIQFADE_ENV pick the environment. `chosen` was fixed before the
    # extra_files and overrides.yaml layers merged, so a file that writes `env` would
    # otherwise be believed while its environment file was never loaded: overrides.yaml
    # saying `env: mainnet` opened the mainnet gate and left paper's looser risk limits
    # in place, which is exactly what CLAUDE.md rule 3 forbids. overrides.yaml is written
    # by the panel, and nothing in the panel may enable live trading.
    if merged.get("env") != chosen:
        raise ConfigError(
            f"{sources.get('env', 'a config layer')} sets env to {merged.get('env')!r}, "
            f"but the loaded environment is {chosen!r}. Only --env and LIQFADE_ENV may "
            f"choose the environment; remove `env` from that file."
        )

    return merged, sources


def load_settings(
    env: str | None = None,
    *,
    extra_files: list[Path] | None = None,
    environ: dict[str, str] | None = None,
) -> Settings:
    """Resolve every layer and validate. Raises ConfigError on any validation failure."""
    merged, _ = resolve(env, extra_files=extra_files, environ=environ)
    try:
        return Settings.model_validate(merged)
    except Exception as exc:
        raise ConfigError(f"invalid configuration: {exc}") from exc


def flatten(tree: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten a nested config dict to dotted keys, for display and diffing."""
    out: dict[str, Any] = {}
    for key, value in tree.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(flatten(value, f"{dotted}."))
        else:
            out[dotted] = value
    return out
