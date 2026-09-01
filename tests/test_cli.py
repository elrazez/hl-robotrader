"""CLI surface: every subcommand exists, placeholders exit 2, status falls back to the file."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from liqfade.cli import NOT_YET, build_parser, build_rsync_commands, main
from liqfade.config import ConfigError, load_settings
from liqfade.control import ControlServer, NoopBackend
from liqfade.control.protocol import RiskState, StateSnapshot, now_ms
from liqfade.control.state import write_state

ALL_COMMANDS = {
    "record",
    "backfill",
    "backtest",
    "paper",
    "testnet",
    "live",
    "panel",
    "status",
    "sync",
    "config",
}


def _subcommands() -> set[str]:
    parser = build_parser()
    actions = [a for a in parser._actions if a.dest == "command"]
    return set(actions[0].choices or {})


def test_help_lists_every_subcommand() -> None:
    assert _subcommands() == ALL_COMMANDS


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "liqfade" in capsys.readouterr().out


def test_unimplemented_commands_exit_two_and_name_their_phase(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for command, phase in NOT_YET.items():
        assert main([command]) == 2
        assert f"not implemented until Phase {phase}" in capsys.readouterr().err


def test_env_flag_works_before_and_after_the_subcommand(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["config", "show", "--env", "mainnet"]) == 0
    assert "env: mainnet" in capsys.readouterr().out
    assert main(["--env", "testnet", "config", "show"]) == 0
    assert "env: testnet" in capsys.readouterr().out


def test_config_show_prints_the_source_of_every_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["config", "show", "--env", "mainnet"]) == 0
    out = capsys.readouterr().out
    assert "[default.yaml]" in out
    assert "[mainnet.yaml]" in out
    assert "sizing.risk_frac" in out
    assert "live trading: blocked" in out


def test_config_show_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config", "show", "--env", "paper", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["settings"]["env"] == "paper"
    assert payload["sources"]["sizing.risk_frac"] == "default.yaml"
    assert payload["live_trading_enabled"] is False


def test_config_error_is_reported_not_raised(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("sizing:\n  risk_frac: 9.0\n")
    assert main(["config", "show", "--config", str(bad)]) == 1
    assert "config error" in capsys.readouterr().err


# --- status -------------------------------------------------------------------


@pytest.fixture
async def running_socket(sock_dir: Path) -> AsyncIterator[Path]:
    srv = ControlServer(NoopBackend(proc="live"), sock_dir / "control.sock")
    await srv.start()
    try:
        yield sock_dir
    finally:
        await srv.stop()


def test_status_reads_the_state_file_when_there_is_no_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LIQFADE_PATHS__DATA_DIR", str(tmp_path))
    settings = load_settings("paper")
    write_state(
        settings.state_path,
        StateSnapshot(proc="recorder", env="paper", ts_ms=now_ms(), risk=RiskState(paused=True)),
    )
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "state.json (stale)" in out
    assert "proc: recorder" in out
    assert "paused=True" in out


def test_status_reports_the_kill_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LIQFADE_PATHS__DATA_DIR", str(tmp_path))
    settings = load_settings("paper")
    write_state(
        settings.state_path,
        StateSnapshot(proc="live", env="paper", ts_ms=now_ms(), risk=RiskState(paused=False)),
    )
    settings.kill_file.write_text("stop")
    assert main(["status"]) == 0
    assert "kill file: PRESENT" in capsys.readouterr().out


def test_status_fails_cleanly_with_no_socket_and_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LIQFADE_PATHS__DATA_DIR", str(tmp_path))
    assert main(["status"]) == 1
    assert "no live process and no state file" in capsys.readouterr().err


async def test_status_prefers_the_control_socket(
    running_socket: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LIQFADE_PATHS__DATA_DIR", str(running_socket))
    # main() calls asyncio.run, so it has to run off this test's loop or the server in
    # the fixture would never get a chance to accept the connection.
    assert await asyncio.to_thread(main, ["status"]) == 0
    out = capsys.readouterr().out
    assert "source: control socket" in out
    assert "proc: live" in out


async def test_status_json(
    running_socket: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LIQFADE_PATHS__DATA_DIR", str(running_socket))
    assert await asyncio.to_thread(main, ["status", "--json"]) == 0
    # The control server logs on the same stdout during the call, so take the payload line.
    line = next(x for x in capsys.readouterr().out.splitlines() if x.startswith("{"))
    payload = json.loads(line)
    assert payload["proc"] == "live"
    assert payload["schema_version"] == 1


# --- sync ---------------------------------------------------------------------


def test_sync_builds_one_rsync_command_per_configured_path() -> None:
    settings = load_settings("paper", environ={"LIQFADE_SYNC_HOST": "liqfade@vps"})
    commands = build_rsync_commands(settings, dry_run=False)
    assert len(commands) == len(settings.sync.paths)
    first = commands[0]
    assert first[0] == "rsync"
    assert "-az" in first and "--info=progress2" in first
    assert first[-2] == "liqfade@vps:/srv/liqfade/data/parquet/"
    assert first[-1] == "data/parquet/"
    assert "--dry-run" not in first


def test_sync_dry_run_adds_the_flag() -> None:
    settings = load_settings("paper", environ={"LIQFADE_SYNC_HOST": "liqfade@vps"})
    assert all("--dry-run" in cmd for cmd in build_rsync_commands(settings, dry_run=True))


def test_sync_host_argument_overrides_config() -> None:
    settings = load_settings("paper", environ={"LIQFADE_SYNC_HOST": "from-config"})
    commands = build_rsync_commands(settings, dry_run=False, host="from-argument")
    assert commands[0][-2].startswith("from-argument:")


def test_sync_without_a_host_is_a_config_error() -> None:
    settings = load_settings("paper", environ={})
    with pytest.raises(ConfigError, match=r"sync\.host is not set"):
        build_rsync_commands(settings, dry_run=False)


def test_sync_command_reports_a_missing_host(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["sync", "--dry-run"]) == 1
    assert "sync.host is not set" in capsys.readouterr().err


# --- backfill -----------------------------------------------------------------


def test_backfill_lists_every_job() -> None:
    parser = build_parser()
    backfill = parser._subparsers._group_actions[0].choices["backfill"]
    job = next(action for action in backfill._actions if action.dest == "job")
    assert set(job.choices) == {
        "s3-fills",
        "rederive",
        "l2",
        "candles",
        "tardis",
        "hydromancer-check",
    }


def test_backfill_without_dates_explains_itself_and_exits_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["backfill", "s3-fills"]) == 1
    assert "needs --start and --end" in capsys.readouterr().err


def test_hydromancer_check_without_a_day_explains_itself(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["backfill", "hydromancer-check"]) == 1
    assert "needs --check-day" in capsys.readouterr().err


def test_a_bad_date_is_reported_rather_than_traced(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["backfill", "rederive", "--start", "2026-13-01", "--end", "2026-09-01"]) == 1
    assert "bad date" in capsys.readouterr().err


def test_the_l2_job_refuses_without_coins_before_touching_aws(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """--coins is checked before the S3 client is built, so this needs no credentials."""
    code = main(
        [
            "backfill",
            "l2",
            "--start",
            "2026-09-01",
            "--end",
            "2026-09-01",
            "--data-dir",
            str(tmp_path),
        ]
    )
    assert code == 1
    assert "--coins is required" in capsys.readouterr().err


def test_rederive_runs_offline_over_an_empty_lake(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code = main(
        [
            "backfill",
            "rederive",
            "--start",
            "2026-09-01",
            "--end",
            "2026-09-01",
            "--data-dir",
            str(tmp_path),
        ]
    )
    assert code == 0
    assert "no fills_full" in capsys.readouterr().out


def test_missing_aws_credentials_are_a_message_not_a_traceback(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The owner's first run of this command will not have keys yet."""
    from botocore.exceptions import NoCredentialsError

    import liqfade.data.ingest.common as ingest_common

    def no_creds() -> None:
        raise NoCredentialsError()

    monkeypatch.setattr(ingest_common, "s3_client", no_creds)
    code = main(
        [
            "backfill",
            "s3-fills",
            "--start",
            "2026-08-01",
            "--end",
            "2026-08-02",
            "--estimate-only",
            "--data-dir",
            str(tmp_path),
        ]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "no AWS credentials" in err
    assert "Traceback" not in err


def test_an_aws_client_setup_failure_is_a_message_not_a_traceback(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from botocore.exceptions import ProfileNotFound

    import liqfade.data.ingest.common as ingest_common

    def bad_profile() -> None:
        raise ProfileNotFound(profile="nope")

    monkeypatch.setattr(ingest_common, "s3_client", bad_profile)
    code = main(
        [
            "backfill",
            "l2",
            "--start",
            "2026-08-01",
            "--end",
            "2026-08-02",
            "--coins",
            "BTC",
            "--estimate-only",
            "--data-dir",
            str(tmp_path),
        ]
    )
    assert code == 1
    assert "AWS client setup failed" in capsys.readouterr().err
