"""What the box is doing, read out of `data/state.json`.

Read-only, and only ever the snapshot: the control socket is the trading loop's own
front door and a workbench has no business knocking on it. The snapshot is written every
second by whichever process is up, so its age is the answer to "is anything running", and
a stale file still gets shown because the last thing a process said before it stopped is
usually the interesting part.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from liqfade.control.protocol import StateSnapshot, now_ms
from liqfade.control.state import read_state

__all__ = ["StatusView", "read_status"]

# Seconds of silence after which the writer is presumed gone. The writer's interval is one
# second and `panel.state_stale_s` is the panel's equivalent of this number.
STALE_S = 10.0


def _hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


@dataclass(frozen=True)
class StatusView:
    """The snapshot if there is one, how old it is, and what to say about it."""

    path: Path
    state: StateSnapshot | None
    age_s: float | None
    running: bool
    message: str


def read_status(state_path: Path, stale_s: float = STALE_S) -> StatusView:
    """The snapshot at `state_path`. Never raises: every failure is a message."""
    path = Path(state_path)
    try:
        state = read_state(path)
    except FileNotFoundError:
        return StatusView(
            path=path,
            state=None,
            age_s=None,
            running=False,
            message=(
                f"no live or recorder process here: nothing has written {path}. "
                "On the VPS this tab shows what the box is doing; on the laptop it is "
                "usually empty and that is correct."
            ),
        )
    except (OSError, ValueError) as exc:
        return StatusView(
            path=path,
            state=None,
            age_s=None,
            running=False,
            message=f"{path} could not be read: {type(exc).__name__}",
        )

    age_s = max((now_ms() - state.ts_ms) / 1000.0, 0.0)
    if age_s > stale_s:
        return StatusView(
            path=path,
            state=state,
            age_s=age_s,
            running=False,
            message=(
                f"the last snapshot is {_hms(age_s)} old, so nothing is writing it now. "
                "What follows is what the process said before it stopped."
            ),
        )
    return StatusView(
        path=path,
        state=state,
        age_s=age_s,
        running=True,
        message=f"{state.proc} on {state.env}, snapshot {age_s:.1f}s old",
    )
