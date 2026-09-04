"""Panel auth, the state endpoint's staleness rule and the loopback bind guard."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from liqfade.config import load_settings
from liqfade.config.models import Settings
from liqfade.control.protocol import RiskState, StateSnapshot, now_ms
from liqfade.control.state import write_state
from liqfade.panel.app import create_app, is_loopback, liquidations_by_venue, serve
from liqfade.panel.auth import (
    AuthNotConfiguredError,
    LoginLimiter,
    SessionAuth,
    hash_password,
    verify_password,
)

PASSWORD = "a-long-enough-password"
# A fixed UTC hour, so the hour grid never depends on when the suite runs.
FIXED_HOUR = (1_788_000_000_000 // 3_600_000) * 3_600_000


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return load_settings("paper", environ={"LIQFADE_PATHS__DATA_DIR": str(tmp_path)})


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    auth = SessionAuth(password_hash=hash_password(PASSWORD), secret="test-secret", ttl_s=60)
    with TestClient(create_app(settings, auth)) as client:
        yield client


def _snapshot(ts_ms: int | None = None) -> StateSnapshot:
    return StateSnapshot(
        proc="live",
        env="paper",
        ts_ms=ts_ms if ts_ms is not None else now_ms(),
        risk=RiskState(paused=False, equity=1000.0),
    )


# --- auth ---------------------------------------------------------------------


def test_password_hash_round_trips() -> None:
    hashed = hash_password(PASSWORD)
    assert verify_password(PASSWORD, hashed)
    assert not verify_password("wrong", hashed)


def test_short_password_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 12"):
        hash_password("short")


def test_malformed_hash_reads_as_wrong_password_rather_than_crashing() -> None:
    assert verify_password(PASSWORD, "not-a-bcrypt-hash") is False


def test_session_token_expires() -> None:
    # itsdangerous stamps whole seconds and compares age > max_age, so a 1s ttl needs
    # just over 2s of wall clock to be certainly expired. This is the only sleep in the
    # suite; expiry is worth two seconds because a session that never expires is a bug
    # nothing else would catch.
    auth = SessionAuth(password_hash=hash_password(PASSWORD), secret="s", ttl_s=1)
    token = auth.login(PASSWORD)
    assert token is not None
    assert auth.valid(token) is True
    time.sleep(2.05)
    assert auth.valid(token) is False


def test_a_token_signed_with_another_secret_is_rejected() -> None:
    hashed = hash_password(PASSWORD)
    token = SessionAuth(password_hash=hashed, secret="one", ttl_s=60).login(PASSWORD)
    assert SessionAuth(password_hash=hashed, secret="two", ttl_s=60).valid(token) is False


def test_missing_password_hash_env_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIQFADE_PANEL_PASSWORD_HASH", raising=False)
    with pytest.raises(AuthNotConfiguredError, match="hash-password"):
        SessionAuth.from_env(60)


def test_login_limiter_blocks_after_the_configured_attempts() -> None:
    limiter = LoginLimiter(max_attempts=3, window_s=100.0)
    for _ in range(3):
        assert limiter.blocked("1.2.3.4") is False
        limiter.record_failure("1.2.3.4")
    assert limiter.blocked("1.2.3.4") is True
    assert limiter.blocked("5.6.7.8") is False  # per client, not global
    limiter.reset("1.2.3.4")
    assert limiter.blocked("1.2.3.4") is False


def test_login_limiter_window_expires() -> None:
    limiter = LoginLimiter(max_attempts=2, window_s=10.0)
    limiter.record_failure("x", now=0.0)
    limiter.record_failure("x", now=1.0)
    assert limiter.blocked("x", now=5.0) is True
    assert limiter.blocked("x", now=20.0) is False


# --- endpoints ----------------------------------------------------------------


def test_health_needs_no_session(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["env"] == "paper"


def test_state_and_config_require_a_session(client: TestClient) -> None:
    assert client.get("/api/state").status_code == 401
    assert client.get("/api/config").status_code == 401


def test_login_flow(client: TestClient) -> None:
    assert client.get("/api/session").json() == {"authenticated": False}
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 200
    assert client.get("/api/session").json() == {"authenticated": True}
    client.post("/api/logout")
    assert client.get("/api/session").json() == {"authenticated": False}


def test_login_is_rate_limited(settings: Settings) -> None:
    auth = SessionAuth(password_hash=hash_password(PASSWORD), secret="s", ttl_s=60)
    with TestClient(create_app(settings, auth)) as client:
        for _ in range(settings.panel.login_max_attempts):
            assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
        locked = client.post("/api/login", json={"password": PASSWORD})
        assert locked.status_code == 429  # even the right password is refused while locked


def test_state_returns_503_when_there_is_no_file(client: TestClient) -> None:
    client.post("/api/login", json={"password": PASSWORD})
    response = client.get("/api/state")
    assert response.status_code == 503
    assert "no liqfade process is running" in response.json()["detail"]


def test_state_returns_the_snapshot_when_fresh(client: TestClient, settings: Settings) -> None:
    client.post("/api/login", json={"password": PASSWORD})
    write_state(settings.state_path, _snapshot())
    body = client.get("/api/state").json()
    assert body["proc"] == "live"
    assert body["risk"]["equity"] == 1000.0
    assert body["age_s"] < settings.panel.state_stale_s


def test_state_returns_503_when_stale_but_still_carries_the_last_snapshot(
    client: TestClient, settings: Settings
) -> None:
    client.post("/api/login", json={"password": PASSWORD})
    stale_ms = now_ms() - int((settings.panel.state_stale_s + 5) * 1000)
    write_state(settings.state_path, _snapshot(stale_ms))
    response = client.get("/api/state")
    assert response.status_code == 503
    body = response.json()
    assert "over the" in body["detail"]
    assert body["state"]["proc"] == "live"


def test_state_returns_503_on_a_corrupt_file(client: TestClient, settings: Settings) -> None:
    client.post("/api/login", json={"password": PASSWORD})
    settings.state_path.parent.mkdir(parents=True, exist_ok=True)
    settings.state_path.write_text("{ half a fi")
    response = client.get("/api/state")
    assert response.status_code == 503
    assert "unreadable" in response.json()["detail"]


def test_config_endpoint_is_read_only_and_reports_sources(client: TestClient) -> None:
    client.post("/api/login", json={"password": PASSWORD})
    body = client.get("/api/config").json()
    assert body["writable"] is False
    assert body["live_trading_enabled"] is False
    keys = {row["key"]: row for row in body["values"]}
    assert keys["sizing.risk_frac"]["value"] == 0.005
    assert keys["sizing.risk_frac"]["source"] == "default.yaml"


def test_the_built_bundle_is_served(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert '<div id="root">' in response.text


# --- bind guard ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("localhost", True),
        ("::1", True),
        ("0.0.0.0", False),
        ("10.0.0.5", False),
        ("example.com", False),
        # uvicorn passes "" to socket.bind, which reads it as INADDR_ANY.
        ("", False),
    ],
)
def test_is_loopback(host: str, expected: bool) -> None:
    assert is_loopback(host) is expected


def test_serve_refuses_a_non_loopback_bind(tmp_path: Path) -> None:
    settings = load_settings(
        "paper",
        environ={"LIQFADE_PATHS__DATA_DIR": str(tmp_path), "LIQFADE_PANEL__HOST": "0.0.0.0"},
    )
    with pytest.raises(SystemExit, match="not loopback"):
        serve(settings)


def test_serve_refuses_an_empty_host(tmp_path: Path) -> None:
    """An empty host binds every interface, so it must not pass the guard.

    It arrives from a YAML layer rather than an env var: LIQFADE_PANEL__HOST="" parses
    as a YAML null and is rejected by the model before it ever reaches serve().
    """
    extra = tmp_path / "run.yaml"
    extra.write_text('panel:\n  host: ""\n')
    settings = load_settings(
        "paper",
        extra_files=[extra],
        environ={"LIQFADE_PATHS__DATA_DIR": str(tmp_path)},
    )
    assert settings.panel.host == ""
    with pytest.raises(SystemExit, match="not loopback"):
        serve(settings)


# --- recorder page (Panel v1) ---------------------------------------------------


def _write_liquidations(root: Path, rows: list[dict[str, object]]) -> None:
    """Write rows through the real writer, so the test reads what the recorder writes."""
    from liqfade.recorder.buffer import partition_of
    from liqfade.recorder.writer import write_rows

    for row in rows:
        date, hour = partition_of(int(row["ts_ms"]))  # type: ignore[arg-type]
        write_rows(root, ("liquidations", str(row["venue"]), date, hour), [row])


def _liq(venue: str, ts_ms: int, notional: float) -> dict[str, object]:
    return {
        "venue": venue,
        "coin": "BTC",
        "venue_symbol": "BTCUSDT",
        "ts_ms": ts_ms,
        "rx_ms": ts_ms,
        "side": "long",
        "px": 79000.0,
        "sz": notional / 79000.0,
        "notional_usd": notional,
        "method": "unknown",
        "complete": venue == "bybit",
    }


def test_recorder_endpoints_need_a_session(client: TestClient) -> None:
    assert client.get("/api/recorder/disk").status_code == 401
    assert client.get("/api/recorder/liquidations").status_code == 401


def test_disk_endpoint_reports_the_data_volume(client: TestClient) -> None:
    client.post("/api/login", json={"password": PASSWORD})
    body = client.get("/api/recorder/disk").json()
    assert body["total_bytes"] > 0
    assert body["free_bytes"] <= body["total_bytes"]
    assert 0.0 <= body["used_pct"] <= 100.0


def test_liquidations_with_no_lake_yet_is_an_empty_chart_rather_than_an_error(
    client: TestClient,
) -> None:
    client.post("/api/login", json={"password": PASSWORD})
    body = client.get("/api/recorder/liquidations").json()
    assert body["venues"] == body["totals"] == {}
    # The hour grid is still complete, so the chart spans 24h instead of nothing.
    assert len(body["hour_ms"]) == 24


def test_liquidations_are_bucketed_by_hour_and_venue(settings: Settings) -> None:
    """The hour is pinned rather than read from the clock.

    Reading now_ms() twice, once to write the rows and once to serve them, fails whenever
    the wall clock rolls an hour in between.
    """
    hour = FIXED_HOUR
    _write_liquidations(
        settings.paths.data_dir / "parquet",
        [
            _liq("bybit", hour + 1000, 30_000.0),
            _liq("bybit", hour + 2000, 20_000.0),
            _liq("okx", hour - 3_600_000, 5_000.0),
        ],
    )
    body = liquidations_by_venue(settings.paths.data_dir / "parquet", 24, now=hour + 60_000)

    assert body["hour_ms"][-1] == hour
    assert len(body["hour_ms"]) == 24
    assert body["venues"]["bybit"][-2:] == [None, 50_000.0]
    assert body["venues"]["okx"][-2:] == [5_000.0, None]
    assert set(body["venues"]["bybit"][:-1]) == {None}
    assert body["totals"] == {"bybit": 50_000.0, "okx": 5_000.0}
    assert body["events"] == {"bybit": 2, "okx": 1}


def test_an_appledouble_sidecar_does_not_break_the_chart(settings: Settings) -> None:
    """duckdb fails outright on one: "No magic bytes found at end of file"."""
    root = settings.paths.data_dir / "parquet"
    _write_liquidations(root, [_liq("bybit", FIXED_HOUR, 7.0)])
    partition = next((root / "liquidations").glob("date=*"))
    (partition / "._bybit_00.parquet").write_bytes(b"not parquet at all")

    body = liquidations_by_venue(root, 24, now=FIXED_HOUR + 60_000)
    assert body["totals"] == {"bybit": 7.0}


def test_liquidations_older_than_the_window_are_left_out(settings: Settings) -> None:
    hour = FIXED_HOUR
    _write_liquidations(
        settings.paths.data_dir / "parquet",
        [_liq("bybit", hour - 48 * 3_600_000, 1.0), _liq("bybit", hour, 2.0)],
    )
    body = liquidations_by_venue(settings.paths.data_dir / "parquet", 24, now=hour + 60_000)
    assert body["totals"] == {"bybit": 2.0}


def test_the_window_is_capped_so_the_panel_cannot_scan_the_whole_lake(
    client: TestClient,
) -> None:
    client.post("/api/login", json={"password": PASSWORD})
    assert client.get("/api/recorder/liquidations?hours=100000").json()["hours"] == 168
    assert client.get("/api/recorder/liquidations?hours=0").json()["hours"] == 1
