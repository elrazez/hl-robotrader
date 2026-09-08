"""The inbound Telegram bot: who may command it, what each command does, and what it says.

The seam is `CommandBot.handle_update`, which takes one raw Telegram update and returns
the reply text it would send. Every test drives it that way, so the allowlist, the parsing
and the control dispatch are all exercised without a network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from liqfade.alerts.commands import ALLOWLIST_ENV, CommandBot, allowed_chat_ids
from liqfade.control import NoopBackend

ALLOWED = 4242
STRANGER = 9999


def an_update(text: str, chat_id: int = ALLOWED, update_id: int = 1) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "is_bot": False},
            "text": text,
        },
    }


@pytest.fixture
def bot(tmp_path: Path) -> CommandBot:
    backend = NoopBackend(env="paper", proc="paper", kill_file=tmp_path / "KILL")
    return CommandBot(backend, allowed_chat_ids={ALLOWED}, env="paper")


async def test_status_reports_the_state(bot: CommandBot) -> None:
    reply = await bot.handle_update(an_update("/status"))
    assert reply is not None
    assert "paper" in reply
    assert "paused" in reply.lower()


async def test_pause_and_resume_go_through_the_control_backend(bot: CommandBot) -> None:
    await bot.handle_update(an_update("/pause", update_id=1))
    assert (await bot.backend.status()).risk.paused is True

    await bot.handle_update(an_update("/resume", update_id=2))
    assert (await bot.backend.status()).risk.paused is False


async def test_kill_writes_the_flag_and_confirms(bot: CommandBot, tmp_path: Path) -> None:
    reply = await bot.handle_update(an_update("/kill"))

    assert (tmp_path / "KILL").exists()
    assert reply is not None and "KILL" in reply


async def test_a_stranger_gets_nothing_and_is_logged(bot: CommandBot) -> None:
    """The allowlist is the only authentication there is. A chat id not on it is ignored."""
    import structlog

    with structlog.testing.capture_logs() as entries:
        reply = await bot.handle_update(an_update("/kill", chat_id=STRANGER))

    assert reply is None
    assert bot.backend.calls == []  # nothing reached the control plane
    assert any(entry["event"] == "telegram_chat_not_allowed" for entry in entries)


async def test_an_empty_allowlist_refuses_everyone(tmp_path: Path) -> None:
    """A missing TELEGRAM_ALLOWED_CHAT_IDS must not mean "anyone may kill the bot"."""
    backend = NoopBackend(env="paper", proc="paper")
    bot = CommandBot(backend, allowed_chat_ids=set(), env="paper")

    assert await bot.handle_update(an_update("/status")) is None
    assert backend.calls == []


async def test_an_unknown_command_lists_the_real_ones(bot: CommandBot) -> None:
    reply = await bot.handle_update(an_update("/moon"))
    assert reply is not None
    assert "/status" in reply and "/kill" in reply


async def test_a_command_with_the_bot_name_attached_still_parses(bot: CommandBot) -> None:
    """Telegram sends /status@example_bot in a group. The suffix is not part of the name."""
    reply = await bot.handle_update(an_update("/status@example_bot"))
    assert reply is not None and "paper" in reply


async def test_flat_takes_an_optional_coin(bot: CommandBot) -> None:
    await bot.handle_update(an_update("/flat ETH"))
    assert bot.backend.calls[-1] == ("flat", {"coin": "ETH"})


async def test_risk_reports_the_gates(bot: CommandBot) -> None:
    reply = await bot.handle_update(an_update("/risk"))
    assert reply is not None
    assert "equity" in reply.lower()


async def test_a_non_message_update_is_ignored(bot: CommandBot) -> None:
    assert await bot.handle_update({"update_id": 7, "edited_message": {}}) is None
    assert await bot.handle_update({"update_id": 8}) is None


async def test_a_backend_error_is_reported_rather_than_swallowed(tmp_path: Path) -> None:
    """The recorder refuses /pause outright. The owner has to see that, not silence."""

    class Refusing(NoopBackend):
        async def pause(self, reason: str | None = None) -> Any:
            raise RuntimeError("not supported by recorder")

    bot = CommandBot(
        Refusing(env="paper", proc="recorder"), allowed_chat_ids={ALLOWED}, env="paper"
    )
    reply = await bot.handle_update(an_update("/pause"))

    assert reply is not None and "not supported" in reply


def test_the_allowlist_is_parsed_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOWLIST_ENV, " 111, 222 ,333 ")
    assert allowed_chat_ids() == {111, 222, 333}

    monkeypatch.setenv(ALLOWLIST_ENV, "111,notanid")
    assert allowed_chat_ids() == {111}

    monkeypatch.delenv(ALLOWLIST_ENV)
    assert allowed_chat_ids() == set()


async def test_updates_are_processed_once_and_the_offset_advances(bot: CommandBot) -> None:
    """getUpdates replays anything unacknowledged. A /kill must not fire twice."""
    assert await bot.handle_update(an_update("/pause", update_id=5)) is not None
    assert bot.offset == 6
    assert await bot.handle_update(an_update("/pause", update_id=5)) is None
    assert bot.offset == 6


def test_a_telegram_error_never_carries_the_bot_token() -> None:
    """httpx puts the full request URL in its exception message, token and all.

    That is how the token reached the journal on 2026-09-08, five times per unit, from a
    409 the two loops caused between them. CLAUDE.md rule 6 is about the agent key by
    name, but a bot token is a credential and a log line is forever: anyone who can read
    the journal can drive the alert bot.
    """
    from liqfade.alerts.redact import redact

    message = (
        "HTTPStatusError: Client error '409 Conflict' for url "
        "'https://api.telegram.org/bot1234567890:AAGsecretsecretsecret/getUpdates?timeout=25'"
    )
    cleaned = redact(message)

    assert "AAGsecretsecretsecret" not in cleaned
    assert "1234567890" not in cleaned
    assert "409 Conflict" in cleaned, "the useful half must survive"
    assert "bot***" in cleaned


def test_redacting_leaves_an_ordinary_message_alone() -> None:
    from liqfade.alerts.redact import redact

    assert redact("ConnectTimeout: timed out") == "ConnectTimeout: timed out"
