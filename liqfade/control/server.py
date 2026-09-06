"""Unix domain socket server that dispatches control commands to a ControlBackend.

One line of JSON in, one line of JSON out, connections may be reused. The socket is
created with mode 0600 and is owned by the service user, which together with the file
system is the only access control: there is no authentication on the wire because
anything that can open the socket already runs as the trading user. Created 0600, not
chmodded to it a line later: the difference is an instant during which `/kill` is open
to anything on the box.

One process per socket, enforced by an exclusive `flock` on `<socket>.lock` rather than
asserted in a comment. Recorder and live both run 24/7 under systemd, so without it
whichever started last unlinked the other's socket and silently took over: `/kill` and
`/flat` would then land on the process with nothing at risk while the one holding a
position kept trading. Two processes that both need a control plane get two paths, set
per unit with `LIQFADE_CONTROL__SOCKET_PATH` and `LIQFADE_CONTROL__STATE_FILE`; the lock
is what makes forgetting that loud instead of silent.

The server never blocks the caller's event loop for long; backends are required to
return promptly and start slow work in the background.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
from pathlib import Path
from typing import Any

import orjson
from pydantic import ValidationError

from liqfade.control.backend import ControlBackend
from liqfade.control.protocol import ControlError, Request, Response
from liqfade.log import get_logger

log = get_logger(__name__)

# A request is a command name and a few arguments, never more. The cap is enforced by
# asyncio's StreamReader limit, which is 64 KiB by default and raises LimitOverrunError
# out of `readuntil`; a length check after the read could never fire.
# sockaddr_un.sun_path is 104 bytes on macOS and 108 on Linux. A socket_path from config
# that is too long otherwise fails with a bare "AF_UNIX path too long" at bind time.
MAX_SOCKET_PATH_BYTES = 100
LOCK_SUFFIX = ".lock"


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
            case "inject_intent":
                result = await backend.inject_intent(**args)
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
        self._lock_fd: int | None = None

    @property
    def lock_path(self) -> Path:
        return self.socket_path.with_name(self.socket_path.name + LOCK_SUFFIX)

    def _take_lock(self) -> None:
        """Claim the socket, or refuse to start. Enforces what the comment used to assert.

        An exclusive `flock` on a sibling lock file, held for the life of the process and
        released by the kernel however it dies, so a crash never leaves the socket
        unclaimable. The lock file itself is never unlinked: another process could then
        create a fresh inode and lock that, and the two would not see each other.
        """
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise ControlError(
                f"another liqfade process is already serving {self.socket_path}. "
                "Two processes on one control socket means /kill reaches the wrong one; "
                "give this one its own path with LIQFADE_CONTROL__SOCKET_PATH."
            ) from None
        self._lock_fd = fd

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)  # closing releases the flock
            self._lock_fd = None

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)

        encoded = str(self.socket_path).encode()
        if len(encoded) > MAX_SOCKET_PATH_BYTES:
            raise OSError(
                f"control socket path is {len(encoded)} bytes, over the "
                f"{MAX_SOCKET_PATH_BYTES} byte unix socket limit: {self.socket_path}"
            )

        # The lock comes before the unlink. Removing a socket a crashed process left
        # behind is necessary, and removing one a *running* process is serving is how
        # `/kill` and `/flat` end up on the wrong side of a live position.
        self._take_lock()
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()

        # start_unix_server creates the socket at 0777 & ~umask and the chmod below only
        # lands afterwards, so under the box's default umask it exists world readable for
        # an instant. Narrow the umask across the bind instead; it is process wide, so it
        # is restored immediately, and the chmod stays as the assertion of what we meant.
        previous_umask = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(self._handle, path=str(self.socket_path))
        finally:
            os.umask(previous_umask)
        os.chmod(self.socket_path, 0o600)
        log.info("control_listening", socket=str(self.socket_path))

    async def stop(self) -> None:
        """Close the server and remove only a socket this instance actually bound.

        A server that never got the lock must not unlink the path on its way out: that is
        the running process's socket, and taking it away is the same failure the lock
        exists to prevent, just later.
        """
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            with contextlib.suppress(FileNotFoundError):
                self.socket_path.unlink()
            log.info("control_stopped", socket=str(self.socket_path))
        self._release_lock()

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
