"""Logging shape and the runtime shutdown path."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import signal
from contextlib import redirect_stdout

import pytest

from liqfade.log import get_logger, setup_logging
from liqfade.runtime import run_main, shutdown_event


def _capture(fn, *, level: str = "INFO") -> list[dict]:
    """Run `fn` with logging configured to write into a buffer, return the parsed lines.

    setup_logging happens inside the redirect on purpose: both the structlog logger and
    the stdlib handler bind the stream they are given at configuration time.
    """
    buf = io.StringIO()
    with redirect_stdout(buf):
        setup_logging(proc="unit", env="paper", level=level)
        fn()
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def test_log_line_is_json_with_the_required_keys() -> None:
    lines = _capture(lambda: get_logger().info("order_submit", coin="BTC", oid=1))
    assert len(lines) == 1
    line = lines[0]
    assert line["event"] == "order_submit"
    assert line["proc"] == "unit"
    assert line["env"] == "paper"
    assert isinstance(line["ts_ms"], int) and line["ts_ms"] > 1_600_000_000_000
    assert line["coin"] == "BTC"


def test_secrets_are_redacted() -> None:
    lines = _capture(
        lambda: get_logger().info(
            "signer_ready", agent_private_key="0xabc", bot_token="123:xyz", coin="ETH"
        )
    )
    assert lines[0]["agent_private_key"] == "***"
    assert lines[0]["bot_token"] == "***"
    assert lines[0]["coin"] == "ETH"


def test_a_secret_field_nobody_listed_is_still_redacted() -> None:
    """The list used to be exact names, so every spelling nobody thought of went through."""
    lines = _capture(
        lambda: get_logger().info(
            "probe",
            secret="s",
            signature="sig",
            privateKey="0xdead",
            aws_secret_access_key="a",
            x_api_key="k",
            passphrase="p",
        )
    )
    fields = ("secret", "signature", "privateKey", "aws_secret_access_key", "x_api_key")
    assert {lines[0][field] for field in (*fields, "passphrase")} == {"***"}


def test_a_secret_nested_under_an_innocent_field_is_redacted_too() -> None:
    lines = _capture(
        lambda: get_logger().info(
            "config_reloaded",
            values={"telegram": {"bot_token": "123:xyz", "enabled": True}},
            args=[{"api_key": "k"}],
        )
    )
    assert lines[0]["values"]["telegram"] == {"bot_token": "***", "enabled": True}
    assert lines[0]["args"] == [{"api_key": "***"}]


def test_an_s3_object_key_is_not_a_secret() -> None:
    """The backfill logs `key=` for the object it just pulled, which is how a resume is
    read. A bare "key" substring would have taken that away."""
    lines = _capture(lambda: get_logger().info("backfill_object", key="node_fills/20260101/3"))
    assert lines[0]["key"] == "node_fills/20260101/3"


def test_stdlib_logging_is_rendered_as_json_too() -> None:
    lines = _capture(lambda: logging.getLogger("uvicorn").warning("ws closed"))
    assert lines[0]["event"] == "ws closed"
    assert lines[0]["proc"] == "unit"


def test_log_level_filters() -> None:
    assert _capture(lambda: get_logger().info("ignored"), level="WARNING") == []
    assert _capture(lambda: get_logger().warning("kept"), level="WARNING")[0]["event"] == "kept"


def test_run_main_returns_the_coroutine_result() -> None:
    async def main() -> int:
        return 42

    assert run_main(main) == 42


def test_run_main_stops_a_long_task_on_sigterm() -> None:
    async def main() -> str:
        stop = shutdown_event()
        os.kill(os.getpid(), signal.SIGTERM)
        await stop.wait()
        return "clean"

    assert run_main(main, grace_s=2.0) == "clean"


def test_run_main_cancels_a_task_that_ignores_the_grace_period() -> None:
    async def main() -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(30)

    with pytest.raises(TimeoutError):
        run_main(main, grace_s=0.2)


def test_shutdown_event_outside_run_main_raises() -> None:
    with pytest.raises(RuntimeError):
        shutdown_event()
