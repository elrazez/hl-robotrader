"""The terminal frame `liqfade watch` draws from a `ProgressState`.

Pure: `render` takes the state and the wall clock and returns text, so the frame can be
tested without a terminal. Colour is plain ANSI, no dependency; a dumb terminal shows the
escape codes and nothing worse.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from liqfade.backtest.progress import ProgressState, process_alive

BOLD, DIM, GREEN, RED, CYAN, RESET = (
    "\x1b[1m",
    "\x1b[2m",
    "\x1b[32m",
    "\x1b[31m",
    "\x1b[36m",
    "\x1b[0m",
)
STAGE_TEXT = {
    "starting": "starting",
    "loading": "loading the lake",
    "features": "features, one day",
    "replaying": "replaying",
    "report": "writing the report",
    "done": "done",
    "failed": "FAILED",
}


def _hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d") if ms else "?"


def _money(value: float, signed: bool = False) -> str:
    text = f"{value:+,.2f}" if signed else f"{value:,.2f}"
    return f"${text}"


def bar(fraction: float, width: int) -> str:
    filled = round(max(0.0, min(1.0, fraction)) * width)
    return "█" * filled + "░" * (width - filled)


def render(state: ProgressState | None, now: float, *, width: int = 100, path: Path) -> str:
    """Three lines: the run, the bar, the numbers. Everything fits in `width` columns."""
    if state is None:
        return f"{DIM}no backtest running: nothing at {path}{RESET}"

    coins = ",".join(state.coins) if state.coins else "core universe"
    head = (
        f"{BOLD}liqfade backtest{RESET}  {coins}  {_day(state.window_start_ms)} to "
        f"{_day(state.window_end_ms - 1)}  {_money(state.starting_equity)}"
    )
    if state.lagged_ms:
        head += f"  {DIM}then again at {state.lagged_ms} ms{RESET}"

    phase_elapsed = now - state.phase_started_at
    # `features` now runs once per UTC day rather than once per run, and carries that
    # day's clock, so it moves the bar like `replaying` does instead of resetting it to
    # zero three hundred and sixty times over a year.
    on_the_clock = ("features", "replaying", "report", "done")
    fraction = state.fraction if state.stage in on_the_clock else 0.0
    if state.stage == "done":
        fraction = 1.0
    bar_width = max(10, min(50, width - 46))
    pct = f"{100 * fraction:5.1f}%"
    where = (
        _day(state.sim_ms)
        if state.stage in ("features", "replaying")
        else STAGE_TEXT.get(state.stage, "")
    )
    stage = STAGE_TEXT.get(state.stage, state.stage)
    colour = RED if state.stage == "failed" else GREEN if state.stage == "done" else CYAN
    middle = (
        f"{colour}{state.phase:6s} {stage:19s}{RESET} [{bar(fraction, bar_width)}] {pct}  {where}"
    )

    if state.stage in ("features", "replaying") and 0 < fraction < 1:
        eta = _hms(phase_elapsed / fraction - phase_elapsed)
    elif state.stage in ("done", "failed"):
        eta = "-"
    else:
        eta = "…"
    net_colour = GREEN if state.net > 0 else RED if state.net < 0 else ""
    total_elapsed = _hms(now - state.started_at)
    numbers = (
        f"elapsed {total_elapsed}  eta {eta}  trades {state.trades}  "
        f"net {net_colour}{_money(state.net, signed=True)}{RESET}  "
        f"equity {_money(state.equity)}  intents {state.intents}"
    )
    lines = [head, middle, numbers]
    if state.stage == "done" and state.report_dir:
        lines.append(f"{GREEN}report: {state.report_dir}{RESET}")
    if state.stage == "failed":
        lines.append(f"{RED}{state.error}{RESET}")
    if state.stage not in ("done", "failed"):
        alive = process_alive(state.pid)
        stale = now - state.updated_at
        if alive is False:
            lines.append(f"{RED}the run is gone (pid {state.pid} no longer exists){RESET}")
        elif state.stage == "loading":
            lines.append(f"{DIM}this stage reports no progress; the process is alive{RESET}")
        elif stale > 30:
            lines.append(f"{DIM}no update for {_hms(stale)}, the process is alive{RESET}")
    return "\n".join(lines)
