"""Configuration models and the layered loader.

Governed by CLAUDE.md section 4 and docs/STRATEGY_AND_RISK.md, which names every
strategy, sizing, risk and backtest parameter that appears here.
"""

from liqfade.config.loader import (
    ConfigError,
    ConfigLayer,
    config_dir,
    config_layers,
    flatten,
    load_settings,
    overrides_file,
    resolve,
)
from liqfade.config.models import Settings

__all__ = [
    "ConfigError",
    "ConfigLayer",
    "Settings",
    "config_dir",
    "config_layers",
    "flatten",
    "load_settings",
    "overrides_file",
    "resolve",
]
