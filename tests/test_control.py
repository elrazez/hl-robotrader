"""Control socket round trip for every command, and StateWriter atomicity."""

from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import AsyncIterator
from pathlib import Path

import orjson
import pytest

from liqfade.control import (
    RESULT_MODELS,
    ControlClient,
    ControlError,
    ControlServer,
    FlatResult,
    KillResult,
    MetricsResult,
    NoopBackend,
    PauseResult,
    ReloadResult,
    ResumeResult,
    StateSnapshot,
    StateWriter,
    read_state,
    write_state,
)
from liqfade.control.protocol import SCHEMA_VERSION, RiskState


@pytest.fixture
async def server(sock_dir: Path) -> AsyncIterator[tuple[ControlServer, ControlClient]]:
    backend = NoopBackend(env="paper", proc="test")
    srv = ControlServer(backend, sock_dir / "control.sock")
    await srv.start()
    client = ControlClient(srv.socket_path, connect_timeout_s=2.0, request_timeout_s=5.0)
    try:
        yield srv, client
    finally:
        await srv.stop()


async def test_every_command_round_trips_with_a_typed_response(server) -> None:
    _, client = server
    assert isinstance(await client.status(), StateSnapshot)
    assert isinstance(await client.pause("manual"), PauseResult)
    assert isinstance(await client.resume(), ResumeResult)
    assert isinstance(await client.flat("BTC"), FlatResult)
    assert isinstance(await client.reload_config(), ReloadResult)
    assert isinstance(await client.get_metrics(), MetricsResult)
    assert isinstance(await client.kill("test"), KillResult)


async def test_the_command_set_and_the_result_models_stay_in_step(server) -> None:
    _, client = server
    for cmd, model in RESULT_MODELS.items():
        result = await client.send(cmd)  # type: ignore[arg-type]
        assert isinstance(result, model), cmd


async def test_arguments_reach_the_backend(server) -> None:
    srv, client = server
    await client.pause("daily loss")
    await client.flat("ETH")
    await client.kill("panic")
    calls = dict(srv.backend.calls)  # type: ignore[attr-defined]
    assert calls["pause"] == {"reason": "daily loss"}
    assert calls["flat"] == {"coin": "ETH"}
    assert calls["kill"] == {"reason": "panic"}


async def test_state_reflects_pause_and_kill(server) -> None:
    _, client = server
    assert (await client.status()).risk.paused is False
    await client.pause("manual")
    assert (await client.status()).risk.paused is True
    await client.resume()
    assert (await client.status()).risk.paused is False
    await client.kill("stop")
    state = await client.status()
    assert state.risk.killed is True
    assert state.risk.paused is True
    # A killed process may not be resumed back into trading without a restart.
    resumed = await client.resume()
    assert resumed.halt_cleared is False


async def test_socket_is_created_mode_0600(server) -> None:
    srv, _ = server
    mode = stat.S_IMODE(os.stat(srv.socket_path).st_mode)
    assert mode == 0o600


async def test_socket_is_removed_on_stop(sock_dir: Path) -> None:
    srv = ControlServer(NoopBackend(), sock_dir / "control.sock")
    await srv.start()
    assert srv.socket_path.exists()
    await srv.stop()
    assert not srv.socket_path.exists()


async def test_stale_socket_file_does_not_block_startup(sock_dir: Path) -> None:
    path = sock_dir / "control.sock"
    path.write_text("leftover from a crashed process")
    srv = ControlServer(NoopBackend(), path)
    await srv.start()
    try:
        assert await ControlClient(path).status()
    finally:
        await srv.stop()


async def test_one_connection_serves_several_commands(server) -> None:
    srv, _ = server
    reader, writer = await asyncio.open_unix_connection(str(srv.socket_path))
    try:
        for cmd in ("status", "get_metrics", "status"):
            writer.write(orjson.dumps({"cmd": cmd, "id": cmd, "args": {}}) + b"\n")
            await writer.drain()
            response = orjson.loads(await reader.readuntil(b"\n"))
            assert response["ok"] is True
            assert response["id"] == cmd
    finally:
        writer.close()
        await writer.wait_closed()


async def test_unknown_command_is_rejected_not_crashed(server) -> None:
    srv, _ = server
    reader, writer = await asyncio.open_unix_connection(str(srv.socket_path))
    try:
        writer.write(orjson.dumps({"cmd": "drop_tables", "id": "x", "args": {}}) + b"\n")
        await writer.drain()
        response = orjson.loads(await reader.readuntil(b"\n"))
        assert response["ok"] is False
        assert "invalid request" in response["error"]
    finally:
        writer.close()
        await writer.wait_closed()


async def test_malformed_json_is_rejected_not_crashed(server) -> None:
    srv, client = server
    reader, writer = await asyncio.open_unix_connection(str(srv.socket_path))
    try:
        writer.write(b"{not json at all\n")
        await writer.drain()
        response = orjson.loads(await reader.readuntil(b"\n"))
        assert response["ok"] is False
        assert response["error"] == "malformed json"
    finally:
        writer.close()
        await writer.wait_closed()
    # And the server is still healthy afterwards.
    assert await client.status()


async def test_client_reports_a_missing_socket_clearly(sock_dir: Path) -> None:
    client = ControlClient(sock_dir / "nope.sock", connect_timeout_s=0.5)
    with pytest.raises(ControlError, match="control socket unavailable"):
        await client.status()


async def test_backend_error_becomes_an_error_response(sock_dir: Path) -> None:
    class Broken(NoopBackend):
        async def status(self) -> StateSnapshot:
            raise RuntimeError("feed handler exploded")

    srv = ControlServer(Broken(), sock_dir / "control.sock")
    await srv.start()
    try:
        with pytest.raises(ControlError, match="feed handler exploded"):
            await ControlClient(srv.socket_path).status()
        # The server survived the exception and still answers.
        assert await ControlClient(srv.socket_path).get_metrics()
    finally:
        await srv.stop()


# --- StateWriter --------------------------------------------------------------


def _snapshot(ts_ms: int = 1) -> StateSnapshot:
    return StateSnapshot(proc="test", env="paper", ts_ms=ts_ms, risk=RiskState(paused=False))


async def test_state_writer_writes_a_valid_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    writer = StateWriter(NoopBackend().status, path, interval_s=0.01)
    await writer.write_once()
    state = read_state(path)
    assert state.schema_version == SCHEMA_VERSION
    assert state.proc == "noop"


async def test_state_writer_runs_on_its_interval_and_stops(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    stop = asyncio.Event()
    writer = StateWriter(NoopBackend().status, path, interval_s=0.01)
    task = writer.start(stop)
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    first = read_state(path)
    assert first.ts_ms > 0
    # Nothing more is written once the stop event is set.
    mtime = path.stat().st_mtime_ns
    await asyncio.sleep(0.05)
    assert path.stat().st_mtime_ns == mtime


async def test_state_writer_never_leaves_a_partial_file(tmp_path: Path) -> None:
    """Write in a tight loop while another task reads, and assert every read parses.

    A non-atomic writer (open, truncate, write) fails this within a few iterations
    because the reader catches the file mid write.
    """
    path = tmp_path / "state.json"
    write_state(path, _snapshot(0))
    stop = asyncio.Event()
    reads = 0

    async def writer_loop() -> None:
        i = 0
        while not stop.is_set():
            i += 1
            write_state(path, _snapshot(i))
            await asyncio.sleep(0)

    async def reader_loop() -> None:
        nonlocal reads
        for _ in range(400):
            state = read_state(path)  # raises if the bytes are partial or invalid
            assert state.proc == "test"
            reads += 1
            await asyncio.sleep(0)

    writer_task = asyncio.create_task(writer_loop())
    await reader_loop()
    stop.set()
    await writer_task
    assert reads == 400
    # No temp files were left behind.
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


async def test_socket_path_over_the_unix_limit_fails_with_a_clear_message(
    tmp_path: Path,
) -> None:
    deep = tmp_path / ("d" * 60) / ("e" * 60) / "control.sock"
    with pytest.raises(OSError, match="unix socket limit"):
        await ControlServer(NoopBackend(), deep).start()


async def test_state_writer_survives_a_failing_source(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    calls = 0

    async def flaky() -> StateSnapshot:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("no state yet")
        return _snapshot(calls)

    stop = asyncio.Event()
    task = StateWriter(flaky, path, interval_s=0.01).start(stop)
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert calls > 1
    assert read_state(path).ts_ms > 1


async def test_kill_writes_the_flag_before_it_reports_killed(tmp_path: Path) -> None:
    """Written first, so a crash midway through flattening still leaves the box halted."""
    kill_file = tmp_path / "KILL"
    backend = NoopBackend(kill_file=kill_file)
    result = await backend.kill("manual")
    assert result.killed is True
    assert kill_file.exists()
    assert "manual" in kill_file.read_text()


async def test_a_kill_that_cannot_write_the_flag_does_not_claim_success(tmp_path: Path) -> None:
    """The in-process half still runs, but the result says the kill did not fully take.

    The live unit is Restart=always and gates on the file, so a kill reported as done
    while the flag never reached disk comes back trading three seconds later. Kill switch
    wins means never confirming a success it did not get, and the reason is what the
    Telegram confirmation renders.
    """
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("occupied")
    backend = NoopBackend(kill_file=blocked / "KILL")
    result = await backend.kill("manual")
    assert result.killed is False
    assert result.reason is not None
    assert "manual" in result.reason
    assert "NOT written" in result.reason
    # The in-process half ran regardless.
    assert (await backend.status()).risk.killed is True


async def test_binding_the_socket_leaves_the_process_umask_alone(sock_dir: Path) -> None:
    """The bind narrows the umask so the socket is never created world readable, even for
    the instant before the chmod. The narrowing is process wide, so it has to be put back:
    that half is what a test can see, and forgetting it would silently harden every file
    the recorder writes afterwards."""
    previous = os.umask(0o022)
    try:
        srv = ControlServer(NoopBackend(env="paper", proc="test"), sock_dir / "control.sock")
        await srv.start()
        try:
            assert stat.S_IMODE(os.stat(srv.socket_path).st_mode) == 0o600
        finally:
            await srv.stop()
        assert os.umask(0o022) == 0o022
    finally:
        os.umask(previous)


def test_state_json_is_not_world_readable(tmp_path: Path) -> None:
    """It carries equity, positions and open orders, and lands in a shared data dir."""
    path = tmp_path / "state.json"
    previous = os.umask(0)
    try:
        write_state(path, StateSnapshot(proc="recorder", env="paper", risk=RiskState(paused=False)))
    finally:
        os.umask(previous)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


async def test_a_second_process_cannot_steal_a_live_control_socket(sock_dir: Path) -> None:
    """`start()` unlinked whatever was there and the comment said a second process was
    "already forbidden", but nothing enforced it. Recorder and live both run 24/7 under
    systemd, so whichever started last took the socket and `/kill` landed on the wrong
    process while the other kept trading."""
    path = sock_dir / "control.sock"
    first = ControlServer(NoopBackend(env="paper", proc="recorder"), path)
    await first.start()
    try:
        second = ControlServer(NoopBackend(env="paper", proc="live"), path)
        with pytest.raises(ControlError, match="already serving"):
            await second.start()

        # The loser must not take the winner's socket down on its way out.
        await second.stop()
        assert path.exists()
        client = ControlClient(path, connect_timeout_s=2.0, request_timeout_s=5.0)
        assert (await client.status()).proc == "recorder"
    finally:
        await first.stop()


async def test_the_socket_is_free_again_once_the_holder_stops(sock_dir: Path) -> None:
    """Otherwise a restart under systemd would never come back up."""
    path = sock_dir / "control.sock"
    first = ControlServer(NoopBackend(env="paper", proc="recorder"), path)
    await first.start()
    await first.stop()
    second = ControlServer(NoopBackend(env="paper", proc="recorder"), path)
    await second.start()
    await second.stop()
