"""Local control plane: one typed command set shared by Telegram, the panel and the CLI.

A Unix socket server inside each long running process dispatches commands to a
ControlBackend, and a StateWriter publishes a snapshot of the same state to
data/state.json every second for readers that must never block the trading loop.
Governed by CLAUDE.md section 3 and section 5.
"""

from liqfade.control.backend import (
    ControlBackend,
    NoopBackend,
    kill_not_persisted,
    write_kill_file,
)
from liqfade.control.client import ControlClient
from liqfade.control.protocol import (
    RESULT_MODELS,
    SCHEMA_VERSION,
    CommandName,
    ControlError,
    FeedState,
    FlatResult,
    KillResult,
    LatencyState,
    MetricsResult,
    OrderState,
    PauseResult,
    PositionState,
    ReloadResult,
    Request,
    Response,
    ResumeResult,
    RiskState,
    StateSnapshot,
    now_ms,
)
from liqfade.control.server import ControlServer
from liqfade.control.state import StateWriter, read_state, write_state

__all__ = [
    "RESULT_MODELS",
    "SCHEMA_VERSION",
    "CommandName",
    "ControlBackend",
    "ControlClient",
    "ControlError",
    "ControlServer",
    "FeedState",
    "FlatResult",
    "KillResult",
    "LatencyState",
    "MetricsResult",
    "NoopBackend",
    "OrderState",
    "PauseResult",
    "PositionState",
    "ReloadResult",
    "Request",
    "Response",
    "ResumeResult",
    "RiskState",
    "StateSnapshot",
    "StateWriter",
    "kill_not_persisted",
    "now_ms",
    "read_state",
    "write_kill_file",
    "write_state",
]
