"""Outbound Telegram alerts. One function, no bot, no polling.

The command bot (`/status`, `/kill` and friends) is Phase 5 and lives in its own module.
This is only the outbound half, because the recorder needs to be able to say "Bybit has
been silent for ten minutes" and nothing more.

Raw Bot API over httpx rather than python-telegram-bot: one POST, no dependency, and
nothing in an alert path should be able to raise into the caller. `send_alert` returns
False on any failure and never raises.

Dedupe: the same text is sent at most once per ten minutes. Health checks run every few
seconds and would otherwise repeat the same line hundreds of times for one outage. The
timer starts when a send is attempted, not when it succeeds, so an unreachable Telegram
does not turn into a retry loop against their rate limit.

That puts a rule on the callers: an alert about an ongoing condition must read the same
every tick it is raised. A number that moves (seconds silent, percent full) makes every
repeat a new string and defeats this entirely, so those alerts carry the threshold that
was crossed rather than the current value. See `recorder/health.py`.
"""

from __future__ import annotations

import os
import time

import httpx

from liqfade.log import get_logger

log = get_logger(__name__)

TOKEN_ENV = "TELEGRAM_BOT_TOKEN"  # noqa: S105 - the name of the variable, not a token
CHAT_ENV = "TELEGRAM_ALERT_CHAT_ID"
API = "https://api.telegram.org"
DEDUPE_S = 600.0
DEFAULT_TIMEOUT_S = 10.0

_last_sent: dict[str, float] = {}


def reset_dedupe() -> None:
    """Forget every remembered alert. For tests and for a deliberate re-alert."""
    _last_sent.clear()


def _deduped(text: str, now: float) -> bool:
    for old, when in list(_last_sent.items()):
        if now - when > DEDUPE_S:
            del _last_sent[old]
    if now - _last_sent.get(text, -DEDUPE_S * 2) < DEDUPE_S:
        return True
    _last_sent[text] = now
    return False


async def send_alert(text: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> bool:
    """Post one alert. Returns True only when Telegram accepted it.

    Credentials are read from the environment at call time rather than cached, so adding
    them to .env and restarting nothing but the service is enough, and so a process that
    starts before .env is filled in starts logging alerts instead of crashing.
    """
    if _deduped(text, time.monotonic()):
        return False

    token = os.environ.get(TOKEN_ENV, "")
    chat_id = os.environ.get(CHAT_ENV, "")
    if not token or not chat_id:
        log.warning("alert_not_sent", reason=f"{TOKEN_ENV} or {CHAT_ENV} is not set", alert=text)
        return False

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.post(
                f"{API}/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            )
        if response.status_code != httpx.codes.OK:
            log.warning("alert_failed", status=response.status_code, body=response.text[:200])
            return False
    except Exception as exc:
        log.warning("alert_failed", error=f"{type(exc).__name__}: {exc}")
        return False

    log.info("alert_sent", alert=text)
    return True
