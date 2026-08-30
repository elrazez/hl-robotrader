"""Async client for the control socket, plus a small sync wrapper for the CLI."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import orjson
from pydantic import BaseModel

from liqfade.control.protocol import (
    RESULT_MODELS,
    CommandName,
    ControlError,
    FlatResult,
    KillResult,
    MetricsResult,
    PauseResult,
    ReloadResult,
    Request,
    Response,
    ResumeResult,
    StateSnapshot,
)


class ControlClient:
    """One connection per call. Control traffic is rare, so pooling would be dead weight."""

    def __init__(
        self,
        socket_path: Path,
        *,
        connect_timeout_s: float = 2.0,
        request_timeout_s: float = 5.0,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.connect_timeout_s = connect_timeout_s
        self.request_timeout_s = request_timeout_s

    async def send(self, cmd: CommandName, **args: Any) -> BaseModel:
        """Send one command and return its typed result. Raises ControlError on failure."""
        request = Request(cmd=cmd, args={k: v for k, v in args.items() if v is not None})
        response = await self._roundtrip(request)
        if not response.ok:
            raise ControlError(response.error or "control command failed")
        model = RESULT_MODELS[cmd]
        return model.model_validate(response.result or {})

    async def _roundtrip(self, request: Request) -> Response:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self.socket_path)),
                timeout=self.connect_timeout_s,
            )
        except (OSError, TimeoutError) as exc:
            raise ControlError(f"control socket unavailable at {self.socket_path}: {exc}") from exc

        try:
            writer.write(orjson.dumps(request.model_dump(mode="json")) + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=self.request_timeout_s)
        except (OSError, TimeoutError, asyncio.IncompleteReadError) as exc:
            raise ControlError(f"control request failed: {exc}") from exc
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

        try:
            return Response.model_validate(orjson.loads(line))
        except (orjson.JSONDecodeError, ValueError) as exc:
            raise ControlError(f"malformed control response: {exc}") from exc

    # Convenience wrappers so callers get a concrete type without casting.

    async def status(self) -> StateSnapshot:
        result = await self.send("status")
        assert isinstance(result, StateSnapshot)
        return result

    async def pause(self, reason: str | None = None) -> PauseResult:
        result = await self.send("pause", reason=reason)
        assert isinstance(result, PauseResult)
        return result

    async def resume(self) -> ResumeResult:
        result = await self.send("resume")
        assert isinstance(result, ResumeResult)
        return result

    async def flat(self, coin: str | None = None) -> FlatResult:
        result = await self.send("flat", coin=coin)
        assert isinstance(result, FlatResult)
        return result

    async def kill(self, reason: str | None = None) -> KillResult:
        result = await self.send("kill", reason=reason)
        assert isinstance(result, KillResult)
        return result

    async def reload_config(self) -> ReloadResult:
        result = await self.send("reload_config")
        assert isinstance(result, ReloadResult)
        return result

    async def get_metrics(self) -> MetricsResult:
        result = await self.send("get_metrics")
        assert isinstance(result, MetricsResult)
        return result
