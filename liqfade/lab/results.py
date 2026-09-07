"""Finished runs, read out of `reports/<stamp>_<hash>/`.

`summary.json` is the machine-readable sidecar `write_report` already writes, so the list
is a read of what the engine said rather than a second opinion about it. Two things the
owner wants are not in it, and neither is worth changing the engine for:

  * which config file produced the run. The lab writes `lab.json` beside the report when it
    launches one; a run started by hand has no name and falls back to the config hash.
  * the lagged rerun's net. `liqfade backtest` prints it, so it is in `run.log`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson

from liqfade.lab.runs import THE_YEAR, RunSpec

__all__ = [
    "RunRow",
    "compare_metrics",
    "config_diff",
    "filter_runs",
    "lagged_net",
    "list_runs",
    "promote",
    "sort_runs",
]

LAGGED_RE = re.compile(r"lagged rerun at \d+ ms: \d+ trades, net (-?[\d,.]+) USD")
# What the side by side shows, in the order the owner reads them.
COMPARED = (
    ("trades", "trades"),
    ("net", "net"),
    ("hit rate", "hit_rate"),
    ("worst drawdown", "max_drawdown_frac"),
    ("holdout net", "holdout_net"),
    ("lagged net", "lagged_net"),
    ("intents", "intents"),
    ("wall clock s", "wall_clock_s"),
)


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d") if ms else "?"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded = orjson.loads(path.read_bytes())
    except (OSError, orjson.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def lagged_net(log: Path) -> float | None:
    """The lagged rerun's net, out of the line `liqfade backtest` printed. None when absent."""
    try:
        text = log.read_text()
    except OSError:
        return None
    found = LAGGED_RE.search(text)
    return float(found.group(1).replace(",", "")) if found else None


@dataclass(frozen=True)
class RunRow:
    """One report directory, as the Results list shows it."""

    directory: Path
    run_id: str
    finished: bool
    config: str
    config_path: Path | None
    overrides: dict[str, Any] | None
    trigger: str
    coins: tuple[str, ...]
    start: str
    end: str
    equity: float
    trades: int
    net: float
    hit_rate: float
    max_drawdown_frac: float
    holdout_net: float
    intents: int
    lagged_ms: int | None
    lagged_net: float | None
    latency_fragile: bool | None
    wall_clock_s: float | None
    rules: dict[str, dict[str, Any]]

    @property
    def window(self) -> str:
        return f"{self.start}..{self.end}"

    @property
    def haystack(self) -> str:
        return " ".join([self.run_id, self.config, self.trigger, *self.coins, self.window]).lower()


def _row(directory: Path) -> RunRow:
    summary = _read_json(directory / "summary.json") or {}
    lab = _read_json(directory / "lab.json") or {}
    metrics = summary.get("metrics") or {}
    started, finished_at = lab.get("started_at"), lab.get("finished_at")
    config_path = lab.get("config_path")
    return RunRow(
        directory=directory,
        run_id=str(summary.get("run_id") or directory.name),
        finished=bool(summary),
        config=str(lab.get("config_name") or str(summary.get("config_hash", ""))[:8] or "?"),
        config_path=Path(config_path) if config_path else None,
        overrides=lab.get("overrides"),
        trigger=str(summary.get("trigger", "?")),
        coins=tuple(summary.get("coins") or ()),
        start=_day(int(summary.get("start_ms") or 0)),
        # end_ms is exclusive, as everywhere else in this CLI, so the day shown is the
        # last one the run actually read. `watch.py` does the same subtraction.
        end=_day(max(int(summary.get("end_ms") or 0) - 1, 0)),
        equity=float(summary.get("starting_equity") or 0.0),
        trades=int(metrics.get("trades") or 0),
        net=float(metrics.get("net_pnl") or 0.0),
        hit_rate=float(metrics.get("hit_rate") or 0.0),
        max_drawdown_frac=float(metrics.get("max_drawdown_frac") or 0.0),
        holdout_net=float((summary.get("holdout") or {}).get("net_pnl") or 0.0),
        intents=int((summary.get("funnel") or {}).get("intents") or 0),
        lagged_ms=summary.get("lagged_ms"),
        lagged_net=lagged_net(directory / "run.log"),
        latency_fragile=summary.get("latency_fragile"),
        wall_clock_s=(finished_at - started) if started and finished_at else None,
        rules=summary.get("rules") or {},
    )


def list_runs(reports_dir: Path) -> list[RunRow]:
    """Every run directory under `reports/`, newest last. Dot directories are the lab's own."""
    root = Path(reports_dir)
    if not root.is_dir():
        return []
    return [
        _row(path)
        for path in sorted(root.iterdir())
        if path.is_dir() and not path.name.startswith(".")
    ]


def sort_runs(rows: list[RunRow], key: str, *, reverse: bool = False) -> list[RunRow]:
    """Sort by any `RunRow` field. Rows missing the field keep their place at the bottom."""

    def value(row: RunRow) -> tuple[int, Any]:
        got = getattr(row, key, None)
        return (1, "") if got is None else (0, got)

    return sorted(rows, key=value, reverse=reverse)


def filter_runs(rows: list[RunRow], text: str) -> list[RunRow]:
    """Rows whose id, config, trigger, coins or window contain `text`. Empty keeps all."""
    needle = text.strip().lower()
    return rows if not needle else [row for row in rows if needle in row.haystack]


def compare_metrics(left: RunRow, right: RunRow) -> list[tuple[str, Any, Any]]:
    """The two runs' headline numbers side by side, in reading order."""
    return [(label, getattr(left, field), getattr(right, field)) for label, field in COMPARED]


def config_diff(left: RunRow, right: RunRow) -> list[tuple[str, Any, Any]]:
    """Keys where the two configs differ. Empty when the lab did not launch both runs."""
    if left.overrides is None or right.overrides is None:
        return []
    keys = sorted(set(left.overrides) | set(right.overrides))
    return [
        (key, left.overrides.get(key), right.overrides.get(key))
        for key in keys
        if left.overrides.get(key) != right.overrides.get(key)
    ]


def promote(row: RunRow, *, reports_dir: Path, data_dir: Path | None) -> RunSpec | None:
    """The same config and coins over the backtested year. None when the config is unknown."""
    if row.config_path is None:
        return None
    return RunSpec(
        config=row.config_path,
        coins=row.coins,
        start=THE_YEAR[0],
        end=THE_YEAR[1],
        equity=row.equity or 10000.0,
        latency_ms=None,
        lagged_ms=row.lagged_ms,
        cache=True,
        data_dir=data_dir,
        reports_dir=Path(reports_dir),
    )
