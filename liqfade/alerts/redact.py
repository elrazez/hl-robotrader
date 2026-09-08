"""Keeping the Telegram bot token out of logs and alerts.

Telegram puts the token in the URL path, and httpx puts the URL in its exception message,
so any transport error logs the credential unless something strips it. Seen on 2026-09-08:
a 409 from two loops polling one bot wrote the token into the journal five times per unit,
where it stays until the journal rotates.

CLAUDE.md rule 6 names the agent private key. A bot token is a credential too: anyone who
can read it can post as the alert bot and read every command sent to it.
"""

from __future__ import annotations

import re

__all__ = ["redact"]

_TOKEN_IN_URL = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")


def redact(message: str) -> str:
    """Strip a Telegram bot token out of a message before it is logged or alerted."""
    return _TOKEN_IN_URL.sub("/bot***", message)
