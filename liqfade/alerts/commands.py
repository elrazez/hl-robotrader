"""The inbound half of Telegram: `/status`, `/pause`, `/resume`, `/flat`, `/kill`, `/risk`.

Every command goes through the control plane, so there is exactly one implementation of
each and Telegram, the panel and the CLI cannot drift apart. This module formats and
dispatches; it decides nothing.

**The allowlist is the only authentication there is.** Anyone who learns the bot's name
can message it, so `TELEGRAM_ALLOWED_CHAT_IDS` is what stands between a stranger and
`/kill`. An empty or missing allowlist refuses everyone rather than allowing everyone,
which is the one default worth being loud about: the fail-open version of this reads as
working right up until it does not.

**Long polling, and the offset.** `getUpdates` replays every update that has not been
acknowledged, and it is acknowledged by asking for the next offset. `handle_update`
therefore tracks the highest update id it has seen and ignores anything at or below it, so
a reconnect in the middle of a `/kill` cannot fire the kill a second time.

Raw Bot API over httpx, like `alerts/telegram.py`, for the same reason: one POST and no
dependency in a path that must never raise into the caller.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any

import httpx
import orjson

from liqfade.alerts.redact import redact
from liqfade.control.backend import ControlBackend
from liqfade.control.protocol import StateSnapshot
from liqfade.log import get_logger

log = get_logger(__name__)

__all__ = ["ALLOWLIST_ENV", "CommandBot", "allowed_chat_ids"]

ALLOWLIST_ENV = "TELEGRAM_ALLOWED_CHAT_IDS"
TOKEN_ENV = "TELEGRAM_BOT_TOKEN"  # noqa: S105 - the name of the variable, not a token
API = "https://api.telegram.org"
POLL_TIMEOUT_S = 25  # long poll; Telegram holds the request open until an update arrives

HELP = (
    "Commands: /status, /pause, /resume, /flat [COIN], /kill, /risk.\n"
    "/kill cancels everything, flattens and writes the kill flag."
)


def allowed_chat_ids() -> set[int]:
    """Parse `TELEGRAM_ALLOWED_CHAT_IDS`. Anything unparseable is dropped, not guessed."""
    ids: set[int] = set()
    for part in os.environ.get(ALLOWLIST_ENV, "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            log.warning("telegram_allowlist_entry_ignored", entry=part)
    return ids


class CommandBot:
    """Turns one Telegram update into one control command and one reply."""

    def __init__(
        self,
        backend: ControlBackend,
        *,
        allowed_chat_ids: set[int],
        env: str,
        offset: int = 0,
    ) -> None:
        """
        Args:
            backend: the process's `ControlBackend`. Every command lands here.
            allowed_chat_ids: chat ids permitted to command. Empty refuses everyone.
            env: paper, testnet or mainnet. Shown in every reply, because knowing which
                bot answered matters most when two are running.
            offset: the update id to resume from, normally 0 on a fresh start.
        """
        self.backend = backend
        self.allowed = set(allowed_chat_ids)
        self.env = env
        self.offset = offset
        if not self.allowed:
            log.warning("telegram_allowlist_empty", detail="every inbound command refused")

    async def handle_update(self, update: dict[str, Any]) -> str | None:
        """Handle one update. Returns the reply to send, or None when there is nothing.

        None covers three different things on purpose: an update that is not a command, an
        update already handled, and a chat that is not allowed. None of the three should
        produce a reply, and a stranger in particular learns nothing from silence.
        """
        update_id = int(update.get("update_id", 0))
        if update_id and update_id < self.offset:
            return None
        message = update.get("message")
        if not isinstance(message, dict):
            self.offset = max(self.offset, update_id + 1)
            return None
        chat_id = int(message.get("chat", {}).get("id", 0))
        text = str(message.get("text", "")).strip()
        self.offset = max(self.offset, update_id + 1)

        if chat_id not in self.allowed:
            log.warning("telegram_chat_not_allowed", chat_id=chat_id, text=text[:64])
            return None
        if not text.startswith("/"):
            return None

        command, _, argument = text.partition(" ")
        # In a group Telegram sends /status@the_bot_name. The suffix is addressing, not part
        # of the command.
        name = command.split("@", 1)[0].lower()
        argument = argument.strip()
        log.info("telegram_command", chat_id=chat_id, command=name, argument=argument or None)

        try:
            return await self._dispatch(name, argument)
        except Exception as exc:
            log.exception("telegram_command_failed", command=name)
            return f"{name} failed: {type(exc).__name__}: {exc}"

    async def _dispatch(self, name: str, argument: str) -> str:
        match name:
            case "/status":
                return _format_status(await self.backend.status())
            case "/pause":
                paused = await self.backend.pause(reason=argument or "telegram /pause")
                return f"[{self.env}] paused={paused.paused}" + (
                    f" ({paused.reason})" if paused.reason else ""
                )
            case "/resume":
                resumed = await self.backend.resume()
                if not resumed.halt_cleared:
                    return f"[{self.env}] resume refused: {resumed.reason}"
                return f"[{self.env}] resumed, paused={resumed.paused}"
            case "/flat":
                coin = argument.upper() or None
                flat = await self.backend.flat(coin=coin)
                closing = ", ".join(flat.closing) or "nothing"
                return (
                    f"[{self.env}] flattening {closing}, cancelled {flat.canceled_orders} order(s)"
                )
            case "/kill":
                killed = await self.backend.kill(reason=argument or "telegram /kill")
                head = "KILL confirmed" if killed.killed else "KILL INCOMPLETE"
                return (
                    f"[{self.env}] {head}: cancelled {killed.canceled_orders}, "
                    f"closed {killed.closed_positions}"
                    + (f"\n{killed.reason}" if killed.reason else "")
                )
            case "/risk":
                return _format_risk(await self.backend.status())
            case "/help" | "/start":
                return f"[{self.env}] {HELP}"
            case _:
                return f"[{self.env}] unknown command {name}. {HELP}"

    # --- the poller -----------------------------------------------------------

    async def run(self, stop: asyncio.Event, *, timeout_s: int = POLL_TIMEOUT_S) -> None:
        """Long poll `getUpdates` until `stop`. Never raises; a failure backs off and retries."""
        token = os.environ.get(TOKEN_ENV, "")
        if not token:
            log.warning("telegram_bot_disabled", reason=f"{TOKEN_ENV} is not set")
            return
        async with httpx.AsyncClient(timeout=timeout_s + 10) as client:
            while not stop.is_set():
                try:
                    updates = await self._poll(client, token, timeout_s)
                except Exception as exc:
                    log.warning(
                        "telegram_poll_failed", error=redact(f"{type(exc).__name__}: {exc}")
                    )
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=5.0)
                    continue
                for update in updates:
                    reply = await self.handle_update(update)
                    if reply is not None:
                        chat_id = int(update["message"]["chat"]["id"])
                        await self._send(client, token, chat_id, reply)

    async def _poll(
        self, client: httpx.AsyncClient, token: str, timeout_s: int
    ) -> list[dict[str, Any]]:
        response = await client.get(
            f"{API}/bot{token}/getUpdates",
            params={"timeout": timeout_s, "offset": self.offset},
        )
        response.raise_for_status()
        payload = orjson.loads(response.content)
        return list(payload.get("result", []))

    async def _send(self, client: httpx.AsyncClient, token: str, chat_id: int, text: str) -> None:
        with contextlib.suppress(Exception):
            # A reply that cannot be delivered must not take the poller down: the command
            # it answers has already been executed.
            await client.post(
                f"{API}/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            )


def _format_status(state: StateSnapshot) -> str:
    lines = [
        f"[{state.env}] {state.proc} up {state.uptime_s / 3600:.1f}h, "
        f"live_trading={state.live_trading}",
        f"paused={state.risk.paused} killed={state.risk.killed} equity={state.risk.equity:.2f}",
    ]
    if state.risk.halt_reason:
        lines.append(f"HALT: {state.risk.halt_reason}")
    if state.positions:
        for position in state.positions:
            lines.append(
                f"{position.coin} {position.side} {position.size:g} @ {position.entry_px:g} "
                f"pnl {position.unrealized_pnl:+.2f}"
            )
    else:
        lines.append("no open positions")
    lines.append(f"{len(state.orders)} open order(s)")
    stale = [feed for feed in state.feeds if feed.lag_ms > 60_000 or not feed.connected]
    if stale:
        lines.append("stale feeds: " + ", ".join(f"{feed.name} {feed.lag_ms}ms" for feed in stale))
    return "\n".join(lines)


def _format_risk(state: StateSnapshot) -> str:
    risk = state.risk
    lines = [
        f"[{state.env}] equity {risk.equity:.2f}, day start {risk.day_start_equity:.2f}, "
        f"peak {risk.peak_equity:.2f}",
        f"daily pnl {risk.daily_realized_pnl:+.2f}, consecutive losses "
        f"{risk.consecutive_losses}, open {risk.open_positions}",
        f"paused={risk.paused} killed={risk.killed}",
    ]
    if risk.halt_reason:
        lines.append(f"halt: {risk.halt_reason} until {risk.halt_until_ms}")
    if risk.coin_cooldowns:
        lines.append("cooldowns: " + ", ".join(sorted(risk.coin_cooldowns)))
    if risk.actions_remaining is not None:
        lines.append(f"action budget left: {risk.actions_remaining}")
    return "\n".join(lines)
