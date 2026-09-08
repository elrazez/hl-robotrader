"""The panel's audit log: every control action and every settings change it made.

One JSON object per line under `data_dir`, appended and never rewritten. JSONL rather
than a table because this is a record, not a query surface: the page reads the tail, and
so does anyone with `tail -f` at 3am. It lives beside the state files rather than in the
code directory, for the same reason `overrides.yaml` does; see `panel/overrides.py`.

Nothing here is a substitute for the process's own structured log. The loop logs what it
did; this logs what the panel asked for, who confirmed it and what came back, which is
the only place those three sit on one line.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import orjson

from liqfade.control.protocol import now_ms

AUDIT_FILE = "panel_audit.jsonl"
# Entries the page shows. The file is the record; this is the window onto it, and the
# page is a monitor rather than a log viewer.
DEFAULT_TAIL = 100


def audit_path(data_dir: Path) -> Path:
    return Path(data_dir) / AUDIT_FILE


def record(
    data_dir: Path,
    *,
    action: str,
    args: dict[str, Any] | None = None,
    ok: bool,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Append one entry and return it, so a route can log and answer with one object.

    A failure to write is not a failure of the action: the action already happened, and
    raising here would tell the caller it did not. The entry comes back either way.
    """
    entry = {
        "ts_ms": now_ms(),
        "action": action,
        "args": args or {},
        "ok": ok,
        "result": result,
        "error": error,
    }
    path = audit_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 0600 like the state files: the entries carry coins, sizes and risk limits.
        # Opened with O_APPEND so two writes can never interleave a partial line.
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(handle, orjson.dumps(entry) + b"\n")
        finally:
            os.close(handle)
    except OSError as exc:
        entry["audit_write_error"] = str(exc)
    return entry


def tail(data_dir: Path, limit: int = DEFAULT_TAIL) -> list[dict[str, Any]]:
    """The most recent entries, newest first. A missing or half written line is skipped."""
    path = audit_path(data_dir)
    try:
        lines = path.read_bytes().splitlines()
    except (FileNotFoundError, OSError):
        return []
    entries: list[dict[str, Any]] = []
    for line in reversed(lines):
        if len(entries) >= max(1, limit):
            break
        if not line.strip():
            continue
        try:
            entries.append(orjson.loads(line))
        except orjson.JSONDecodeError:
            # A line torn by a crash mid-write. Skipping it keeps the rest readable,
            # which is the whole point of one object per line.
            continue
    return entries
