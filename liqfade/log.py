"""Structured logging: one orjson line per event on stdout.

Every line carries `event`, `ts_ms`, `env`, `proc` and `level`. systemd captures stdout,
so there is no file handling here; logrotate on the VPS deals with the journal export.

Call `setup_logging` once per process, before anything else logs.
"""

from __future__ import annotations

import logging
import sys
import time
import warnings
from typing import TYPE_CHECKING, Any, cast

import orjson
import structlog

if TYPE_CHECKING:
    from typing import TextIO

# Substrings that make a field name a secret. CLAUDE.md rule 6: the agent private key is
# read once in execution/signer.py and never travels further, and nothing else with a
# credential in it reaches a log line either.
#
# Substrings rather than exact names, because the exact list only covered the eight
# spellings someone thought of: `secret`, `signature`, `private` and every future
# variation went straight through. A bare "key" is deliberately not here: the backfill
# logs `key=` for an S3 object key, which is a path and the thing you need when a pull
# resumes. `_key` catches `api_key`, `secret_key` and `agent_private_key` without it.
_REDACT = (
    "private",
    "secret",
    "_key",
    "password",
    "token",
    "authorization",
    "cookie",
    "signature",
    "credential",
    "passphrase",
)
# Nested structures are walked too, because a redacted top level key said nothing about
# the dict underneath it. Bounded, so a logging path can never recurse away on a cycle.
_MAX_REDACT_DEPTH = 6


class _Stdout:
    """A stand-in for stdout that resolves `sys.stdout` at write time.

    structlog's PrintLogger and logging.StreamHandler both hold on to the stream object
    they are given, so binding `sys.stdout` at setup_logging() time means anything that
    replaces the stream afterwards (a `contextlib.redirect_stdout`, pytest's capture)
    leaves every later log line writing to a file that has since been closed. Under
    systemd the stream never moves and this changes nothing; the point is that the
    process logs to whatever stdout is now rather than to whatever it was at startup.
    """

    def write(self, text: str) -> int:
        return sys.stdout.write(text)

    def flush(self) -> None:
        sys.stdout.flush()


_STDOUT = _Stdout()


def _is_secret(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in _REDACT)


def _scrub(value: Any, depth: int = 0) -> Any:
    """Replace every secret-looking field inside `value`, however deep it sits."""
    if depth >= _MAX_REDACT_DEPTH:
        return value
    if isinstance(value, dict):
        return {
            key: "***" if isinstance(key, str) and _is_secret(key) else _scrub(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_scrub(item, depth + 1) for item in value]
    return value


def _redact(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key in list(event_dict):
        event_dict[key] = "***" if _is_secret(key) else _scrub(event_dict[key])
    return event_dict


def _stamp(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    event_dict["ts_ms"] = int(time.time() * 1000)
    return event_dict


def _dumps(obj: Any, default: Any = None, **_kw: Any) -> str:
    """orjson returns bytes; structlog's PrintLogger wants str."""
    return orjson.dumps(obj, default=default or repr).decode()


def _silence_polars_asof_noise() -> None:
    """Drop the join_asof sortedness warning, which polars emits on every `by=` join.

    It fires whether the frame is sorted or not, so it carries no signal at all, and the
    feature pipeline makes dozens of these joins per run. A hundred copies of a warning
    that means nothing is how the one that means something gets missed. `pyproject.toml`
    filters the same string for pytest; this is the runtime half.
    """
    warnings.filterwarnings(
        "ignore",
        message="Sortedness of columns cannot be checked when 'by' groups provided",
        category=UserWarning,
    )


def setup_logging(*, proc: str, env: str, level: str = "INFO") -> None:
    """Configure structlog and the stdlib root logger to emit the same JSON lines.

    `proc` names the process (recorder, live, panel, watchdog, cli) and `env` is the
    liqfade environment. Both are stamped on every line so a single journal can be split
    by process and environment after the fact.
    """
    numeric = getattr(logging, level.upper(), logging.INFO)
    _silence_polars_asof_noise()

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
        # cast: PrintLoggerFactory is typed for a full TextIO, but PrintLogger only
        # ever calls write() and flush(), which is all _Stdout provides.
        logger_factory=structlog.PrintLoggerFactory(file=cast("TextIO", _STDOUT)),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (uvicorn, websockets, httpx) through the same renderer.
    handler = logging.StreamHandler(_STDOUT)
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

    # httpx logs `HTTP Request: POST <full url>` at INFO, and the Telegram bot token sits
    # in that URL path. _REDACT is keyed on field names and cannot see a secret embedded
    # in a message string, so the only fix is to not emit the line. Nothing below WARNING
    # from the HTTP stack is worth the risk of leaking a credential into the journal.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(proc=proc, env=env)


def get_logger(name: str | None = None) -> Any:
    """A bound logger. Always log with an `event` name as the first positional argument."""
    return structlog.get_logger(name)
