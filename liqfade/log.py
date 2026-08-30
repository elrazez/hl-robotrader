"""Structured logging: one orjson line per event on stdout.

Every line carries `event`, `ts_ms`, `env`, `proc` and `level`. systemd captures stdout,
so there is no file handling here; logrotate on the VPS deals with the journal export.

Call `setup_logging` once per process, before anything else logs.
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Any

import orjson
import structlog

# Keys that must never appear in a log line even if a caller passes them. CLAUDE.md rule
# 6: the agent private key is read once in execution/signer.py and never travels further.
_REDACT = frozenset(
    {
        "private_key",
        "secret_key",
        "agent_private_key",
        "api_key",
        "password",
        "password_hash",
        "token",
        "bot_token",
        "authorization",
        "cookie",
    }
)


def _redact(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key in list(event_dict):
        if key.lower() in _REDACT:
            event_dict[key] = "***"
    return event_dict


def _stamp(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    event_dict["ts_ms"] = int(time.time() * 1000)
    return event_dict


def _dumps(obj: Any, default: Any = None, **_kw: Any) -> str:
    """orjson returns bytes; structlog's PrintLogger wants str."""
    return orjson.dumps(obj, default=default or repr).decode()


def setup_logging(*, proc: str, env: str, level: str = "INFO") -> None:
    """Configure structlog and the stdlib root logger to emit the same JSON lines.

    `proc` names the process (recorder, live, panel, watchdog, cli) and `env` is the
    liqfade environment. Both are stamped on every line so a single journal can be split
    by process and environment after the fact.
    """
    numeric = getattr(logging, level.upper(), logging.INFO)

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        _stamp,
        _redact,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            *shared,
            structlog.processors.JSONRenderer(serializer=_dumps),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (uvicorn, websockets, httpx) through the same renderer.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(serializer=_dumps),
            ],
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(numeric)

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(proc=proc, env=env)


def get_logger(name: str | None = None) -> Any:
    """A bound logger. Always log with an `event` name as the first positional argument."""
    return structlog.get_logger(name)
