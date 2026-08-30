"""Typed commands, responses and the state snapshot schema.

Wire format: newline delimited JSON over a Unix domain socket, one request object per
line, one response object per line, serialized with orjson. Chosen over a length prefixed
framing because it stays debuggable with `nc -U data/control.sock` when something is wrong
at 3am, and every message here is small enough that framing efficiency is irrelevant.

There is exactly one command set. Telegram, the panel and the CLI are all clients of it,
so pause, flat, kill and reload_config have one implementation each.
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1

CommandName = Literal[
    "status",
    "pause",
    "resume",
    "flat",
    "kill",
    "reload_config",
    "get_metrics",
]


def now_ms() -> int:
    """Current wall clock in integer milliseconds UTC. The only time source in control/."""
    return int(time.time() * 1000)


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Requests -----------------------------------------------------------------


class Request(Base):
    cmd: CommandName
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, description="echoed in the response")
    args: dict[str, Any] = Field(default_factory=dict, description="command specific arguments")


# --- Command results ----------------------------------------------------------


class FeedState(Base):
    """One recorder stream: how far behind it is and whether it has gapped recently."""

    name: str = Field(..., description="venue.stream, e.g. hyperliquid.trades")
    connected: bool
    last_msg_ms: int = Field(..., description="ms UTC of the last message, 0 if never")
    lag_ms: int = Field(..., description="ms since the last message at snapshot time")
    msgs_1m: int = Field(0, ge=0, description="count of messages in the trailing minute")
    gaps_1h: int = Field(0, ge=0, description="count of gaps over recorder.gap_alert_s in 1h")
    reconnects_1h: int = Field(0, ge=0, description="count of reconnects in the trailing hour")


class PositionState(Base):
    coin: str
    side: Literal["long", "short"]
    size: float = Field(..., description="signed size in coin units")
    entry_px: float = Field(..., description="average entry price")
    mark_px: float = Field(..., description="current mark price")
    notional_usd: float
    unrealized_pnl: float = Field(..., description="USD, net of fees already paid")
    opened_ms: int = Field(..., description="ms UTC the position was opened")


class OrderState(Base):
    cloid: str = Field(..., description="our client order id, the key for everything")
    oid: int | None = Field(None, description="the exchange order id once acked")
    coin: str
    side: Literal["buy", "sell"]
    px: float
    size: float
    filled: float = Field(0.0, ge=0.0)
    tif: str = Field(..., description="Alo | Gtc | Ioc, or the trigger type")
    reduce_only: bool
    status: str = Field(..., description="NEW|SUBMITTED|ACKED|PARTIAL|FILLED|CANCELED|REJECTED")
    submitted_ms: int


class RiskState(Base):
    paused: bool = Field(..., description="true when new entries are blocked")
    halt_reason: str | None = Field(None, description="why entries are halted, None when clear")
    halt_until_ms: int | None = Field(None, description="ms UTC the halt lifts, None if manual")
    killed: bool = Field(False, description="true once the kill switch has fired")
    equity: float = Field(0.0, description="USD account equity")
    day_start_equity: float = Field(0.0, description="USD equity at the UTC day boundary")
    peak_equity: float = Field(0.0, description="USD high water mark")
    daily_realized_pnl: float = Field(0.0, description="USD realized since the UTC day boundary")
    consecutive_losses: int = Field(0, ge=0)
    open_positions: int = Field(0, ge=0)
    coin_cooldowns: dict[str, int] = Field(
        default_factory=dict, description="coin -> ms UTC the cooldown expires"
    )
    actions_remaining: int | None = Field(
        None, description="estimated address action budget left, see docs/HYPERLIQUID.md"
    )


class LatencyState(Base):
    """Every value is milliseconds. None means not measured yet in this process."""

    ws_ping_p50: float | None = None
    ws_ping_p95: float | None = None
    rest_p50: float | None = None
    rest_p95: float | None = None
    submit_ack_p50: float | None = None
    submit_ack_p95: float | None = None
    samples: int = Field(0, ge=0)


class StateSnapshot(Base):
    """What `status` returns and what StateWriter writes to data/state.json.

    Readers (panel, CLI, Telegram) only ever touch this snapshot, never the trading loop,
    so a slow reader can never block a decision tick.
    """

    schema_version: int = Field(SCHEMA_VERSION, description="bump when a field changes meaning")
    proc: str = Field(..., description="which process wrote it: live, paper, recorder")
    ts_ms: int = Field(default_factory=now_ms, description="ms UTC the snapshot was taken")
    env: str = Field(..., description="paper | testnet | mainnet")
    live_trading: bool = Field(False, description="whether real orders may be placed right now")
    uptime_s: float = Field(0.0, ge=0.0)
    feeds: list[FeedState] = Field(default_factory=list)
    positions: list[PositionState] = Field(default_factory=list)
    orders: list[OrderState] = Field(default_factory=list)
    risk: RiskState
    latency: LatencyState = Field(default_factory=LatencyState)
    universe: list[str] = Field(default_factory=list, description="HL coins currently subscribed")


class PauseResult(Base):
    paused: bool
    reason: str | None = None


class ResumeResult(Base):
    paused: bool
    halt_cleared: bool = Field(..., description="false when a halt reason blocked the resume")
    reason: str | None = Field(None, description="why the resume was refused, if it was")


class FlatResult(Base):
    closing: list[str] = Field(default_factory=list, description="coins being flattened")
    canceled_orders: int = Field(0, ge=0)


class KillResult(Base):
    killed: bool
    canceled_orders: int = Field(0, ge=0)
    closed_positions: int = Field(0, ge=0)
    reason: str | None = None


class ReloadResult(Base):
    changed: list[str] = Field(default_factory=list, description="dotted keys whose value changed")
    applied_ts_ms: int = Field(default_factory=now_ms)
    error: str | None = Field(None, description="set when the new config failed validation")


class MetricsResult(Base):
    feeds: list[FeedState] = Field(default_factory=list)
    latency: LatencyState = Field(default_factory=LatencyState)
    counters: dict[str, int] = Field(
        default_factory=dict, description="monotonic process counters: intents, fills, rejects"
    )


Result = Annotated[
    StateSnapshot
    | PauseResult
    | ResumeResult
    | FlatResult
    | KillResult
    | ReloadResult
    | MetricsResult,
    Field(description="the command specific payload"),
]

# The result model each command returns. Clients use this to parse a response.
RESULT_MODELS: dict[str, type[BaseModel]] = {
    "status": StateSnapshot,
    "pause": PauseResult,
    "resume": ResumeResult,
    "flat": FlatResult,
    "kill": KillResult,
    "reload_config": ReloadResult,
    "get_metrics": MetricsResult,
}


class Response(Base):
    id: str = Field(..., description="the request id this answers")
    ok: bool
    result: dict[str, Any] | None = None
    error: str | None = None

    @classmethod
    def success(cls, request_id: str, result: BaseModel) -> Response:
        return cls(id=request_id, ok=True, result=result.model_dump(mode="json"))

    @classmethod
    def failure(cls, request_id: str, error: str) -> Response:
        return cls(id=request_id, ok=False, error=error)


class ControlError(RuntimeError):
    """Raised by the client when the socket is unreachable or the server returned ok=false."""
