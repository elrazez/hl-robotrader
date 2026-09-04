"""Layered config loading with per-key provenance.

Layer order, later wins:

    config/default.yaml -> config/{env}.yaml -> config/overrides.yaml -> LIQFADE_ env vars

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
from pathlib import Path
from typing import Any

import yaml

from liqfade.config.models import Settings

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
ENV_PREFIX = "LIQFADE_"
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
        try:
            value = yaml.safe_load(raw)
        except yaml.YAMLError:
            value = raw
        _set_dotted(layer, dotted, value)
    return layer


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

    # overrides.yaml is written by the panel and may legitimately not exist.
    _merge(merged, _read_yaml(directory / "overrides.yaml"), sources, "overrides.yaml")

    _merge(merged, _env_layer(environ), sources, "envvar")

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
