"""The Status tab. Read-only: it opens a file, and that is all it does.

The same tool over SSH on the VPS therefore shows what the box is doing without being able
to change it, which is the point: nothing in the lab may touch a running trading process.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import VerticalScroll
from textual.widgets import Static

from liqfade.control.protocol import StateSnapshot
from liqfade.lab.status import StatusView, read_status


class StatusPane(VerticalScroll):
    """`data/state.json`, redrawn once a second."""

    BINDINGS: ClassVar[list[BindingType]] = [("r", "refresh", "refresh")]

    def __init__(self, state_path: Path) -> None:
        super().__init__(id="status-pane")
        self.state_path = state_path

    def compose(self) -> ComposeResult:
        yield Static("", id="status-body")

    def on_mount(self) -> None:
        self.action_refresh()
        self.set_interval(1.0, self.action_refresh)

    def action_refresh(self) -> None:
        self.query_one("#status-body", Static).update(_frame(read_status(self.state_path)))


def _frame(view: StatusView) -> Text:
    text = Text()
    text.append(f"{view.message}\n\n", style="bold" if view.running else "yellow")
    if view.state is None:
        return text
    _process(text, view.state)
    _feeds(text, view.state)
    _positions(text, view.state)
    _latency(text, view.state)
    return text


def _process(text: Text, state: StateSnapshot) -> None:
    text.append(f"proc {state.proc}   env {state.env}   uptime {state.uptime_s:,.0f}s\n")
    text.append(
        "live trading ",
        style="bold",
    )
    text.append(
        "ENABLED\n" if state.live_trading else "off\n",
        style="red bold" if state.live_trading else "green",
    )
    risk = state.risk
    text.append(
        f"risk  paused={risk.paused} killed={risk.killed} halt={risk.halt_reason or '-'}  "
        f"equity={risk.equity:,.2f}  daily={risk.daily_realized_pnl:,.2f}  "
        f"streak={risk.consecutive_losses}\n\n"
    )


def _feeds(text: Text, state: StateSnapshot) -> None:
    if not state.feeds:
        return
    text.append("feeds\n", style="bold")
    for feed in state.feeds:
        text.append(f"  {feed.name:<28} ")
        text.append(
            f"{'ok' if feed.connected else 'DOWN':<5}",
            style="green" if feed.connected else "red bold",
        )
        text.append(
            f" lag={feed.lag_ms:>8}ms  msgs_1m={feed.msgs_1m:<7} gaps_1h={feed.gaps_1h}"
            f"  parse_errors_1h={feed.parse_errors_1h}\n"
        )
    text.append("\n")


def _positions(text: Text, state: StateSnapshot) -> None:
    if state.positions:
        text.append("positions\n", style="bold")
        for position in state.positions:
            text.append(
                f"  {position.coin:<8} {position.side:<5} size={position.size:<12.4f} "
                f"notional={position.notional_usd:>10.2f} upnl={position.unrealized_pnl:>9.2f}\n"
            )
        text.append("\n")
    if state.orders:
        text.append("orders\n", style="bold")
        for order in state.orders:
            text.append(
                f"  {order.cloid[:12]:<14} {order.coin:<8} {order.side:<4} {order.px:<12} "
                f"{order.filled}/{order.size} {order.status}\n"
            )
        text.append("\n")


def _latency(text: Text, state: StateSnapshot) -> None:
    lat = state.latency
    if lat.samples:
        text.append(
            f"latency ms  ws_ping p50={lat.ws_ping_p50} p95={lat.ws_ping_p95}   "
            f"rest p50={lat.rest_p50} p95={lat.rest_p95}   "
            f"submit_ack p50={lat.submit_ack_p50} p95={lat.submit_ack_p95}\n"
        )
    if state.universe:
        text.append(f"universe ({len(state.universe)}) {' '.join(state.universe)}\n")
