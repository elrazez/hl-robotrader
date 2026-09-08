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
    return _a_bot(kill_file=tmp_path / "KILL")


def _a_bot(*, offset_path: Path | None = None, kill_file: Path | None = None) -> CommandBot:
    backend = NoopBackend(env="paper", proc="paper", kill_file=kill_file or Path("KILL"))
    return CommandBot(backend, allowed_chat_ids={ALLOWED}, env="paper", offset_path=offset_path)


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


# --- a command is consumed exactly once, whatever the process does next -----------


class FakeBotAPI:
    """Telegram's actual contract: an update is redelivered until the offset moves past it."""

    def __init__(self, updates: list[dict]) -> None:
        self.updates = updates
        self.deliveries = 0

    def get_updates(self, offset: int) -> list[dict]:
        self.deliveries += 1
        return [u for u in self.updates if u["update_id"] >= offset]


def a_kill_update(update_id: int = 77, chat_id: int = ALLOWED) -> dict:
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": "/kill"},
    }


async def test_a_kill_is_not_redelivered_to_the_process_that_starts_next(tmp_path) -> None:
    """Drilled on 2026-09-08: paper restarted and its own /kill killed it again, 42 times.

    Telegram redelivers an update until the offset moves past it, the offset lived only in
    memory, and the command in question is the one that stops the process. So the killed
    process never acknowledged it and the next one was handed the same /kill on the first
    poll, one second after starting.
    """
    api = FakeBotAPI([a_kill_update()])
    offset_file = tmp_path / "telegram_offset"

    first = _a_bot(offset_path=offset_file)
    for update in api.get_updates(first.offset):
        await first.handle_update(update)
    assert first.backend._killed, "the first process ran the kill"

    # The process died here. A new one starts and asks Telegram what it missed.
    second = _a_bot(offset_path=offset_file)
    redelivered = api.get_updates(second.offset)

    assert redelivered == [], "the offset on disk consumed it"
    assert second.backend._killed is False


async def test_the_offset_is_written_before_the_command_runs(tmp_path) -> None:
    """Before, not after: the command may be the one that stops this process.

    Asserted from inside the handler, because "after" would still pass a test that only
    looks at the file when the dust has settled, and the whole failure was a process that
    never reached "after".
    """
    offset_file = tmp_path / "telegram_offset"
    bot = _a_bot(offset_path=offset_file, kill_file=tmp_path / "KILL")
    seen: list[str] = []

    async def note_offset(reason: str | None = None):
        seen.append(offset_file.read_text().strip() if offset_file.exists() else "missing")
        return await NoopBackend.kill(bot.backend, reason)

    bot.backend.kill = note_offset  # type: ignore[method-assign]
    await bot.handle_update(a_kill_update())

    assert seen == ["78"], "the update was consumed on disk before the kill ran"


async def test_an_unwritable_offset_still_handles_the_command(tmp_path) -> None:
    """A read-only data dir must not silence the kill switch. It is logged, not raised."""
    bot = _a_bot(offset_path=tmp_path / "nope" / "offset")
    (tmp_path / "nope").write_text("a file where the directory should be")

    await bot.handle_update(a_kill_update())

    assert bot.backend._killed, "the command still ran"
    assert bot.offset == 78, "and the in-memory offset still moved"
