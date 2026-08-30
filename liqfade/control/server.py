"""Unix domain socket server that dispatches control commands to a ControlBackend.

One line of JSON in, one line of JSON out, connections may be reused. The socket is
created with mode 0600 and is owned by the service user, which together with the file
system is the only access control: there is no authentication on the wire because
anything that can open the socket already runs as the trading user.

The server never blocks the caller's event loop for long; backends are required to
return promptly and start slow work in the background.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from typing import Any

import orjson
from pydantic import ValidationError

from liqfade.control.backend import ControlBackend
from liqfade.control.protocol import Request, Response
from liqfade.log import get_logger

log = get_logger(__name__)

MAX_LINE_BYTES = 64 * 1024  # a request is a command name and a few arguments, never more
# sockaddr_un.sun_path is 104 bytes on macOS and 108 on Linux. A socket_path from config
# that is too long otherwise fails with a bare "AF_UNIX path too long" at bind time.
MAX_SOCKET_PATH_BYTES = 100


async def dispatch(backend: ControlBackend, request: Request) -> Response:
    """Route one parsed request to the backend and wrap the result or the error."""
    args = request.args
    try:
        match request.cmd:
            case "status":
                result: Any = await backend.status()
            case "pause":
                result = await backend.pause(reason=args.get("reason"))
            case "resume":
                result = await backend.resume()
            case "flat":
                result = await backend.flat(coin=args.get("coin"))
            case "kill":
                result = await backend.kill(reason=args.get("reason"))
            case "reload_config":
                result = await backend.reload_config()
            case "get_metrics":
                result = await backend.get_metrics()
            case _:  # pragma: no cover - Request validation rejects unknown names first
                return Response.failure(request.id, f"unknown command {request.cmd!r}")
    except Exception as exc:
        log.exception("control_command_failed", cmd=request.cmd)
        return Response.failure(request.id, f"{type(exc).__name__}: {exc}")
    return Response.success(request.id, result)


class ControlServer:
    """Serves control commands on a Unix socket for the lifetime of a process."""

    def __init__(self, backend: ControlBackend, socket_path: Path) -> None:
        self.backend = backend
        self.socket_path = Path(socket_path)
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        # A socket left behind by a crashed process would make bind fail. Removing it is
        # safe because a second live process on the same socket is already forbidden.
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()

        encoded = str(self.socket_path).encode()
        if len(encoded) > MAX_SOCKET_PATH_BYTES:
            raise OSError(
                f"control socket path is {len(encoded)} bytes, over the "
                f"{MAX_SOCKET_PATH_BYTES} byte unix socket limit: {self.socket_path}"
            )

        self._server = await asyncio.start_unix_server(self._handle, path=str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        log.info("control_listening", socket=str(self.socket_path))

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()
        log.info("control_stopped", socket=str(self.socket_path))

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def __aenter__(self) -> ControlServer:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                try:
                    line = await reader.readuntil(b"\n")
                except (asyncio.IncompleteReadError, ConnectionResetError):
                    return
                except asyncio.LimitOverrunError:
                    await self._write(writer, Response.failure("", "request too large"))
                    return
                if not line.strip():
                    continue
                if len(line) > MAX_LINE_BYTES:
                    await self._write(writer, Response.failure("", "request too large"))
                    return
                await self._write(writer, await self._respond(line))
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    async def _respond(self, line: bytes) -> Response:
        try:
            payload = orjson.loads(line)
            request = Request.model_validate(payload)
        except orjson.JSONDecodeError:
            return Response.failure("", "malformed json")
        except ValidationError as exc:
            request_id = payload.get("id", "") if isinstance(payload, dict) else ""
            return Response.failure(str(request_id), f"invalid request: {exc.error_count()} errors")
        log.info("control_command", cmd=request.cmd, request_id=request.id)
        return await dispatch(self.backend, request)

    @staticmethod
    async def _write(writer: asyncio.StreamWriter, response: Response) -> None:
        writer.write(orjson.dumps(response.model_dump(mode="json")) + b"\n")
        with contextlib.suppress(ConnectionError, OSError):
            await writer.drain()
