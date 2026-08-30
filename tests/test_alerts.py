"""Telegram alerts: dedupe, missing credentials, and never raising into the caller."""

from __future__ import annotations

from collections.abc import Iterator
from typing import ClassVar

import httpx
import pytest

from liqfade.alerts import telegram
from liqfade.alerts.telegram import send_alert

CREDS = {"TELEGRAM_BOT_TOKEN": "12345:fake", "TELEGRAM_ALERT_CHAT_ID": "42"}


@pytest.fixture(autouse=True)
def clean() -> Iterator[None]:
    telegram.reset_dedupe()
    yield
    telegram.reset_dedupe()


class FakeClient:
    """Stands in for httpx.AsyncClient, recording what would have been posted."""

    posted: ClassVar[list[dict[str, str]]] = []
    status = 200

    def __init__(self, **_kwargs: object) -> None:
        pass

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def post(self, url: str, json: dict[str, str]) -> httpx.Response:
        FakeClient.posted.append(json)
        return httpx.Response(FakeClient.status, text="{}")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    FakeClient.posted = []
    FakeClient.status = 200
    monkeypatch.setattr(telegram.httpx, "AsyncClient", FakeClient)
    for key, value in CREDS.items():
        monkeypatch.setenv(key, value)
    return FakeClient


async def test_an_alert_is_posted_once(fake: type[FakeClient]) -> None:
    assert await send_alert("recorder: bybit silent for 90s") is True
    assert fake.posted == [
        {
            "chat_id": "42",
            "text": "recorder: bybit silent for 90s",
            "disable_web_page_preview": True,
        }
    ]


async def test_the_same_text_is_not_repeated_within_the_dedupe_window(
    fake: type[FakeClient],
) -> None:
    """Health checks run every few seconds; one outage must not be one alert per check."""
    assert await send_alert("same") is True
    assert await send_alert("same") is False
    assert await send_alert("different") is True
    assert len(fake.posted) == 2


async def test_a_non_200_reads_as_failure_rather_than_raising(fake: type[FakeClient]) -> None:
    fake.status = 403
    assert await send_alert("blocked") is False


async def test_a_transport_error_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    class Exploding(FakeClient):
        async def post(self, url: str, json: dict[str, str]) -> httpx.Response:
            raise httpx.ConnectError("no route to telegram")

    monkeypatch.setattr(telegram.httpx, "AsyncClient", Exploding)
    for key, value in CREDS.items():
        monkeypatch.setenv(key, value)
    assert await send_alert("unreachable") is False


async def test_without_credentials_the_alert_is_logged_and_not_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ALERT_CHAT_ID", raising=False)
    assert await send_alert("nobody is listening") is False
