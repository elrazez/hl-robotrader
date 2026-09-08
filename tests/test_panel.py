"""Panel auth, the state endpoint's staleness rule and the loopback bind guard."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from liqfade.config import load_settings
from liqfade.config.models import Settings
from liqfade.control.backend import NoopBackend
from liqfade.control.protocol import (
    LatencyState,
    OrderState,
    PositionState,
    RiskState,
    StateSnapshot,
    now_ms,
)
from liqfade.control.server import ControlServer
from liqfade.control.state import write_state
from liqfade.panel.app import (
    create_app,
    is_loopback,
    liquidations_by_venue,
    live_socket_path,
    live_state_path,
    serve,
)
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


class _Clock:
    """The clock itsdangerous stamps tokens with and compares them against.

    One `time.time()` call in `itsdangerous.timed` is its whole surface, so standing in
    for the module there is the fake clock. It used to be a real 2.05s sleep, and
    itsdangerous stamps whole seconds and compares `age > max_age`, so no shorter wait
    was certainly expired.
    """

    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


def test_session_token_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock(1_788_000_000.0)
    monkeypatch.setattr("itsdangerous.timed.time", clock)
    auth = SessionAuth(password_hash=hash_password(PASSWORD), secret="s", ttl_s=1)
    token = auth.login(PASSWORD)
    assert token is not None
    assert auth.valid(token) is True
    clock.now += 2.05
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


def test_config_endpoint_reports_sources_and_the_live_trading_gate(client: TestClient) -> None:
    client.post("/api/login", json={"password": PASSWORD})
    body = client.get("/api/config").json()
    # Writable since the settings page landed; TestSettingsWrites owns what that means.
    assert body["writable"] is True
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


# --- latency samples ------------------------------------------------------------


def _with_latency(ts_ms: int) -> StateSnapshot:
    snapshot = _snapshot(ts_ms)
    return snapshot.model_copy(
        update={
            "latency": LatencyState(
                ws_ping_p50=8.0,
                ws_ping_p95=21.0,
                rest_p50=140.0,
                rest_p95=210.0,
                samples=3,
                ws_ping_samples=[(ts_ms - 20_000, 8.0), (ts_ms, 9.5)],
                rest_samples=[(ts_ms - 3_600_000, 140.0)],
            )
        }
    )


def test_latency_endpoint_serves_the_retained_samples(
    client: TestClient, settings: Settings
) -> None:
    """The Phase 1 DoD asks for WS ping and REST RTT as time series, and LatencyState
    only ever exposed p50 and p95 scalars."""
    at = now_ms()
    write_state(settings.state_path, _with_latency(at))
    client.post("/api/login", json={"password": PASSWORD})

    body = client.get("/api/recorder/latency").json()
    assert body["ws_ping"] == [[at - 20_000, 8.0], [at, 9.5]]
    assert body["rest"] == [[at - 3_600_000, 140.0]]
    assert body["ws_ping_p95"] == 21.0
    assert body["rest_p95"] == 210.0  # rendered nowhere before this


def test_latency_needs_a_session(client: TestClient) -> None:
    assert client.get("/api/recorder/latency").status_code == 401


def test_latency_without_a_recorder_running_is_an_empty_chart_not_an_error(
    client: TestClient,
) -> None:
    client.post("/api/login", json={"password": PASSWORD})
    body = client.get("/api/recorder/latency").json()
    # submit_ack is in the shape both latency endpoints share and is always None here:
    # the recorder places no orders. One shape, so the two charts read the same fields.
    assert body == {
        "ws_ping": [],
        "rest": [],
        "ws_ping_p50": None,
        "ws_ping_p95": None,
        "rest_p50": None,
        "rest_p95": None,
        "submit_ack_p50": None,
        "submit_ack_p95": None,
    }


def test_the_state_poll_does_not_carry_the_sample_series(
    client: TestClient, settings: Settings
) -> None:
    """/api/state is polled once a second; the samples are for the 30s chart poll."""
    write_state(settings.state_path, _with_latency(now_ms()))
    client.post("/api/login", json={"password": PASSWORD})
    latency = client.get("/api/state").json()["latency"]
    assert latency["ws_ping_p50"] == 8.0
    assert "ws_ping_samples" not in latency


def test_a_row_timestamped_in_the_future_does_not_break_the_chart(settings: Settings) -> None:
    """A venue clock ahead of ours, or a bad parse: the row has no bucket in the grid,
    and looking one up used to raise a KeyError and 500 the panel's main chart."""
    root = settings.paths.data_dir / "parquet"
    _write_liquidations(
        root, [_liq("bybit", FIXED_HOUR, 2.0), _liq("okx", FIXED_HOUR + 48 * 3_600_000, 9.0)]
    )
    assert liquidations_by_venue(root, 24, now=FIXED_HOUR + 60_000)["totals"] == {"bybit": 2.0}


class TestReportsApi:
    """Panel v2: the backtest report browser, over a real run written to a temp dir."""

    @staticmethod
    def _write_run(reports_dir: Path) -> str:
        """Produce one genuine report by running the engine, not by faking files."""
        import sys

        sys.path.insert(0, str(Path(__file__).parent))
        from test_strategy_cascade_consistency import build_stream

        from liqfade.backtest.engine import RunInputs, run_backtest
        from liqfade.backtest.report import write_report
        from liqfade.config import resolve
        from liqfade.config.models import Settings

        merged, _ = resolve("paper")
        merged["features"]["z_lookback_h"] = 1.0
        settings = Settings.model_validate(merged)
        trades, liquidations, start_ms, end_ms = build_stream()
        result = run_backtest(
            RunInputs(
                trades=trades,
                liquidations=liquidations,
                start_ms=start_ms,
                end_ms=end_ms,
                starting_equity=100_000.0,
            ),
            settings,
        )
        write_report(
            result,
            settings,
            reports_dir=reports_dir,
            run_id="20260906T000000Z_deadbeef",
            n_trials=1,
        )
        return "20260906T000000Z_deadbeef"

    def test_it_lists_runs_with_their_headline_metrics(self, tmp_path: Path) -> None:
        from liqfade.panel.app import list_reports

        run_id = self._write_run(tmp_path)
        (runs,) = (list_reports(tmp_path),)
        assert [run["run_id"] for run in runs] == [run_id]
        assert runs[0]["trades"] == 6
        assert runs[0]["trigger"] == "exact"
        assert runs[0]["error"] is None

    def test_a_run_with_a_broken_sidecar_is_listed_with_an_error(self, tmp_path: Path) -> None:
        """A report the owner can see is broken beats one that silently is not there."""
        from liqfade.panel.app import list_reports

        (tmp_path / "20260101T000000Z_bad").mkdir()
        (tmp_path / "20260101T000000Z_bad" / "summary.json").write_text("{not json")
        (runs,) = (list_reports(tmp_path),)
        assert runs[0]["error"] is not None
        assert runs[0]["trades"] is None

    def test_runs_come_back_newest_first(self, tmp_path: Path) -> None:
        from liqfade.panel.app import list_reports

        for name in ("20260101T000000Z_aaaa", "20260301T000000Z_bbbb", "20260201T000000Z_cccc"):
            (tmp_path / name).mkdir()
        assert [run["run_id"][:8] for run in list_reports(tmp_path)] == [
            "20260301",
            "20260201",
            "20260101",
        ]

    def test_an_absent_reports_directory_is_an_empty_list(self, tmp_path: Path) -> None:
        from liqfade.panel.app import list_reports

        assert list_reports(tmp_path / "nope") == []

    def test_trades_page(self, tmp_path: Path) -> None:
        from liqfade.panel.app import read_trades

        run_id = self._write_run(tmp_path)
        page = read_trades(tmp_path / run_id, limit=2, offset=0)
        assert page["total"] == 6
        assert len(page["trades"]) == 2
        assert page["trades"][0]["index"] == 0

        second = read_trades(tmp_path / run_id, limit=2, offset=2)
        assert second["trades"][0]["index"] == 2
        assert second["trades"][0]["entry_ms"] > page["trades"][0]["entry_ms"]

    def test_the_equity_curve_is_cumulative_net_pnl(self, tmp_path: Path) -> None:
        from liqfade.panel.app import equity_curve

        run_id = self._write_run(tmp_path)
        curve = equity_curve(tmp_path / run_id)
        assert len(curve["ts_ms"]) == 6
        assert curve["equity"] == sorted(curve["equity"]), "this fixture only wins"
        assert all(value <= 0 for value in curve["drawdown"])

    def test_the_trade_window_carries_candles_and_the_marks(self, tmp_path: Path) -> None:
        """The chart needs the flow that triggered the trade, not only the fill."""
        from liqfade.panel.app import trade_window

        run_id = self._write_run(tmp_path)
        # The lake this fixture came from is synthetic and not on disk, so the candle
        # lookup finds nothing. The window must still describe the trade.
        window = trade_window(tmp_path / run_id, tmp_path / "parquet", index=0, before_min=10)
        assert window["trade"]["coin"] == "BTC"
        assert window["start_ms"] == window["trade"]["entry_ms"] - 10 * 60_000
        assert window["end_ms"] > window["trade"]["exit_ms"]
        assert window["candles"] == []

    def test_the_trade_window_reads_candles_off_a_real_lake(self, tmp_path: Path) -> None:
        """Derived from the tape when candles_1m has nothing, so a window always renders."""
        from liqfade.panel.app import _candles

        lake = Path(__file__).parent / "fixtures" / "parquet"
        start_ms = 1_788_100_624_120
        rows = _candles(lake, "BTC", start_ms, start_ms + 300_000)
        assert rows, "the fixture lake has five minutes of BTC tape"
        assert {"ts_ms", "o", "h", "l", "c", "v"} <= set(rows[0])

    def test_an_out_of_range_trade_index_is_a_404(self, tmp_path: Path) -> None:
        from fastapi import HTTPException

        from liqfade.panel.app import trade_window

        run_id = self._write_run(tmp_path)
        with pytest.raises(HTTPException) as caught:
            trade_window(tmp_path / run_id, tmp_path / "parquet", index=99, before_min=10)
        assert caught.value.status_code == 404


class TestReportPathTraversal:
    """`run_id` comes from the URL, so it is untrusted input used to build a path."""

    def test_a_run_id_that_escapes_the_reports_directory_is_refused(self, tmp_path: Path) -> None:
        from fastapi import HTTPException

        from liqfade.panel.app import _run_dir

        (tmp_path / "reports").mkdir()
        (tmp_path / "secrets").mkdir()
        for attempt in ("../secrets", "../../etc", "..%2Fsecrets"):
            with pytest.raises(HTTPException) as caught:
                _run_dir(tmp_path / "reports", attempt)
            assert caught.value.status_code == 404

    def test_a_real_run_id_resolves(self, tmp_path: Path) -> None:
        from liqfade.panel.app import _run_dir

        (tmp_path / "ok").mkdir()
        assert _run_dir(tmp_path, "ok").name == "ok"


# --- the panel's TypeScript mirrors the control protocol ------------------------


class TestTheFrontendTypesMirrorTheProtocol:
    """types.ts says "Mirrors liqfade/control/protocol.py" and nothing checked it, so the
    protocol grew cadence_ms, delivery_coin and delivery_lag_ms and the panel never saw
    them. The panel reads state.json as JSON, so the field names are the contract."""

    # The one deliberate divergence: `/api/state` strips the retained latency windows,
    # which are the only large thing in the snapshot, and `/api/recorder/latency` serves
    # them in a shape of their own because the page polls state every second and the
    # chart every thirty. See liqfade/panel/app.py.
    STRIPPED: ClassVar[dict[str, set[str]]] = {"LatencyState": {"ws_ping_samples", "rest_samples"}}
    # And what the endpoint adds on top of the model: `/api/state` stamps how old the
    # snapshot is, because the page greys itself out when nothing is writing state.json.
    ADDED: ClassVar[dict[str, set[str]]] = {"StateSnapshot": {"age_s"}}

    @staticmethod
    def _interfaces() -> dict[str, set[str]]:
        import re

        source = (
            Path(__file__).resolve().parents[1] / "liqfade/panel/frontend/src/types.ts"
        ).read_text()
        found = {}
        for name, body in re.findall(r"export interface (\w+) \{(.*?)\n\}", source, re.S):
            found[name] = set(re.findall(r"^  (\w+)\??:", body, re.M))
        return found

    def test_every_mirrored_interface_has_the_models_fields(self) -> None:
        from liqfade.control import protocol

        interfaces = self._interfaces()
        assert "FeedState" in interfaces, "types.ts did not parse"
        for name, fields in interfaces.items():
            model = getattr(protocol, name, None)
            if model is None or not hasattr(model, "model_fields"):
                continue  # a type the panel invented for itself, not a mirror
            expected = (set(model.model_fields) - self.STRIPPED.get(name, set())) | self.ADDED.get(
                name, set()
            )
            assert fields == expected, f"{name} has drifted from protocol.py"

    def test_the_schema_version_matches(self) -> None:
        import re

        from liqfade.control.protocol import SCHEMA_VERSION

        source = (
            Path(__file__).resolve().parents[1] / "liqfade/panel/frontend/src/types.ts"
        ).read_text()
        found = re.search(r"export const SCHEMA_VERSION = (\d+)", source)
        assert found is not None and int(found.group(1)) == SCHEMA_VERSION


def test_config_endpoint_reports_the_layers_not_only_the_winner(client: TestClient) -> None:
    """A settings page has to say what it is changing a number from, and back to what.

    `flatten(resolve(...))` gives the winning value and the label of the layer that set
    it, which cannot answer either question. The layers answer both: what each one set,
    in merge order, so removing an override has a visible consequence.
    """
    client.post("/api/login", json={"password": PASSWORD})

    body = client.get("/api/config").json()

    assert [layer["name"] for layer in body["layers"]] == [
        "default.yaml",
        "paper.yaml",
        "overrides.yaml",
        "envvar",
    ]
    default = next(layer for layer in body["layers"] if layer["name"] == "default.yaml")
    assert default["values"]["sizing.risk_frac"] == 0.005
    assert default["exists"] is True
    overrides = next(layer for layer in body["layers"] if layer["name"] == "overrides.yaml")
    assert overrides["values"] == {}
    # The resolved view stays, because that is what the rest of the page reads.
    keys = {row["key"]: row for row in body["values"]}
    assert keys["sizing.risk_frac"]["source"] == "default.yaml"


# --- the live page reads the trading loop, not the recorder ---------------------


class TestTheLiveStateEndpoint:
    """`/api/live/state` is the paper or live loop's snapshot, `/api/state` the recorder's.

    Two processes write state under one data dir on the box: the recorder writes
    `state.json` and the paper loop writes `state-paper.json`, named in
    `scripts/systemd/liqfade-paper.service`. The Live page needs the loop's positions,
    orders and risk, and the recorder's snapshot carries none of them.
    """

    @staticmethod
    def _paper(ts_ms: int | None = None) -> StateSnapshot:
        return StateSnapshot(
            proc="paper",
            env="paper",
            ts_ms=ts_ms if ts_ms is not None else now_ms(),
            uptime_s=126.9,
            positions=[
                PositionState(
                    coin="BTC",
                    side="long",
                    size=0.03,
                    entry_px=79000.0,
                    mark_px=79100.0,
                    notional_usd=2373.0,
                    unrealized_pnl=3.0,
                    opened_ms=1_788_784_000_000,
                )
            ],
            orders=[
                OrderState(
                    cloid="0xabc",
                    oid=7,
                    coin="ETH",
                    side="sell",
                    px=3100.0,
                    size=0.5,
                    tif="Alo",
                    reduce_only=True,
                    status="ACKED",
                    submitted_ms=1_788_784_100_000,
                )
            ],
            risk=RiskState(paused=False, equity=10_000.0, consecutive_losses=2),
        )

    def test_it_needs_a_session(self, client: TestClient) -> None:
        assert client.get("/api/live/state").status_code == 401

    def test_it_reads_the_paper_state_file_not_the_recorders(
        self, client: TestClient, settings: Settings
    ) -> None:
        client.post("/api/login", json={"password": PASSWORD})
        write_state(settings.state_path, _snapshot())  # the recorder's
        write_state(live_state_path(settings), self._paper())

        body = client.get("/api/live/state").json()

        assert body["proc"] == "paper"
        assert body["positions"][0]["coin"] == "BTC"
        assert body["orders"][0]["cloid"] == "0xabc"
        assert body["risk"]["consecutive_losses"] == 2
        assert body["uptime_s"] == 126.9
        assert body["live_trading"] is False
        # The recorder's own snapshot is untouched and still served where it was.
        assert client.get("/api/state").json()["proc"] == "live"

    def test_no_loop_running_is_a_503_naming_the_file(
        self, client: TestClient, settings: Settings
    ) -> None:
        client.post("/api/login", json={"password": PASSWORD})
        response = client.get("/api/live/state")
        assert response.status_code == 503
        assert "state-paper.json" in response.json()["detail"]

    def test_a_stale_snapshot_is_a_503_that_still_carries_the_last_values(
        self, client: TestClient, settings: Settings
    ) -> None:
        """The page greys its controls out behind the banner rather than going blank.

        A stale loop snapshot is the case the read-only banner exists for: the numbers
        on screen are the last known ones and no control may be sent against them.
        """
        client.post("/api/login", json={"password": PASSWORD})
        stale_ms = now_ms() - int((settings.panel.state_stale_s + 5) * 1000)
        write_state(live_state_path(settings), self._paper(stale_ms))

        response = client.get("/api/live/state")

        assert response.status_code == 503
        body = response.json()
        assert "over the" in body["detail"]
        assert body["state"]["positions"][0]["coin"] == "BTC"

    def test_the_poll_does_not_carry_the_sample_series(
        self, client: TestClient, settings: Settings
    ) -> None:
        """Same rule as `/api/state`: this is polled every second over an SSH tunnel."""
        client.post("/api/login", json={"password": PASSWORD})
        snapshot = self._paper()
        snapshot.latency.ws_ping_samples = [(1, 2.0)] * 200
        write_state(live_state_path(settings), snapshot)

        body = client.get("/api/live/state").json()

        assert "ws_ping_samples" not in body["latency"]
        assert "submit_ack_p50" in body["latency"]


# --- fills history and realized PnL --------------------------------------------


def _order_db(path: Path) -> None:
    """A small orders.sqlite with the columns `execution/orders.py` writes.

    Built with sqlite3 rather than by driving OrderManager: the panel reads this file
    with duckdb and never through the manager, so the schema is the contract under test.
    """
    import sqlite3

    from liqfade.execution.orders import SCHEMA

    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    orders = [
        ("0x01", "BTC", 1, 79000.0, 0.03, "Alo", 0, "entry", "FILLED", 1_788_784_000_000),
        ("0x02", "BTC", 0, 79400.0, 0.03, "Ioc", 1, "target", "FILLED", 1_788_784_600_000),
        ("0x03", "ETH", 0, 3100.0, 1.0, "Alo", 0, "entry", "FILLED", 1_788_870_000_000),
        ("0x04", "ETH", 1, 3080.0, 1.0, "Ioc", 1, "target", "FILLED", 1_788_870_600_000),
    ]
    for cloid, coin, is_buy, px, size, tif, reduce_only, tag, status_, submitted in orders:
        connection.execute(
            """INSERT INTO orders (cloid, coin, is_buy, px, size, tif, reduce_only, tag,
                                   status, submitted_ms, updated_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (cloid, coin, is_buy, px, size, tif, reduce_only, tag, status_, submitted, submitted),
        )
    fills = [
        ("0x01", 1_788_784_000_000, 79000.0, 0.03, 1.06, 0),
        ("0x02", 1_788_784_600_000, 79400.0, 0.03, 1.07, 0),
        ("0x03", 1_788_870_000_000, 3100.0, 1.0, 0.46, 1),
        ("0x04", 1_788_870_600_000, 3080.0, 1.0, 1.38, 0),
    ]
    connection.executemany(
        "INSERT INTO fills (cloid, ts_ms, px, size, fee, maker) VALUES (?,?,?,?,?,?)", fills
    )
    connection.commit()
    connection.close()


class TestFillsHistory:
    """Every fill the loop has taken, from the registry the loop already writes.

    Paper fills land in the same `orders.sqlite` as testnet and mainnet ones, so there is
    one store and the panel reads it rather than keeping a second one that could disagree.
    """

    @pytest.fixture
    def filled(self, client: TestClient, settings: Settings) -> TestClient:
        settings.order_db_path.parent.mkdir(parents=True, exist_ok=True)
        _order_db(settings.order_db_path)
        client.post("/api/login", json={"password": PASSWORD})
        return client

    def test_it_needs_a_session(self, client: TestClient) -> None:
        assert client.get("/api/live/fills").status_code == 401

    def test_no_registry_yet_is_an_empty_page_not_an_error(self, client: TestClient) -> None:
        """The loop has never run on this box. That is an empty table, not a 500."""
        client.post("/api/login", json={"password": PASSWORD})
        body = client.get("/api/live/fills").json()
        assert body == {"total": 0, "offset": 0, "fills": []}

    def test_a_fill_carries_its_orders_coin_side_and_tag(self, filled: TestClient) -> None:
        body = filled.get("/api/live/fills").json()
        assert body["total"] == 4
        # The oldest row, which is the BTC entry: the page is newest first.
        oldest = body["fills"][-1]
        assert oldest["coin"] == "BTC"
        assert oldest["side"] == "buy"
        assert oldest["tag"] == "entry"
        assert oldest["maker"] is False
        assert oldest["ts_ms"] == 1_788_784_000_000
        assert oldest["notional_usd"] == pytest.approx(2370.0)

    def test_newest_first(self, filled: TestClient) -> None:
        """The owner opens this page to see what just happened, not what happened first."""
        stamps = [f["ts_ms"] for f in filled.get("/api/live/fills").json()["fills"]]
        assert stamps == sorted(stamps, reverse=True)

    def test_filters_by_coin_and_side(self, filled: TestClient) -> None:
        body = filled.get("/api/live/fills?coin=ETH").json()
        assert body["total"] == 2
        assert {f["coin"] for f in body["fills"]} == {"ETH"}

        sells = filled.get("/api/live/fills?side=sell").json()
        assert {f["side"] for f in sells["fills"]} == {"sell"}
        assert sells["total"] == 2

    def test_filters_by_date_range(self, filled: TestClient) -> None:
        body = filled.get("/api/live/fills?start_ms=1788870000000").json()
        assert body["total"] == 2
        assert {f["coin"] for f in body["fills"]} == {"ETH"}

    def test_it_pages(self, filled: TestClient) -> None:
        page = filled.get("/api/live/fills?limit=2&offset=1").json()
        assert page["total"] == 4
        assert page["offset"] == 1
        assert len(page["fills"]) == 2

    def test_an_unknown_coin_is_an_empty_page(self, filled: TestClient) -> None:
        assert filled.get("/api/live/fills?coin=DOGE").json()["total"] == 0


class TestTheRealizedEquityCurve:
    """Cumulative realized cash flow across the fills, which is what these rows can say.

    A fill row is a cash movement: a buy pays out, a sell takes in, and the fee comes off
    either way. The registry stores no per-trade PnL, so this is the honest curve rather
    than a round trip attribution that would have to guess how legs pair up.
    """

    @pytest.fixture
    def filled(self, client: TestClient, settings: Settings) -> TestClient:
        settings.order_db_path.parent.mkdir(parents=True, exist_ok=True)
        _order_db(settings.order_db_path)
        client.post("/api/login", json={"password": PASSWORD})
        return client

    def test_no_registry_yet_is_an_empty_curve(self, client: TestClient) -> None:
        client.post("/api/login", json={"password": PASSWORD})
        assert client.get("/api/live/equity").json() == {
            "ts_ms": [],
            "equity": [],
            "drawdown": [],
        }

    def test_a_buy_pays_out_and_a_sell_takes_in_net_of_fees(self, filled: TestClient) -> None:
        body = filled.get("/api/live/equity").json()

        # BTC: bought 0.03 at 79000 (-2370.00 - 1.06), sold at 79400 (+2382.00 - 1.07).
        assert body["equity"][0] == pytest.approx(-2371.06)
        assert body["equity"][1] == pytest.approx(9.87)
        # ETH: sold 1 at 3100 (+3100.00 - 0.46), bought back at 3080 (-3080.00 - 1.38).
        assert body["equity"][2] == pytest.approx(3109.41)
        assert body["equity"][3] == pytest.approx(28.03)

    def test_the_curve_is_in_time_order_even_though_the_table_is_not(
        self, filled: TestClient
    ) -> None:
        """The fills table is newest first for reading; a curve has to run forwards."""
        stamps = filled.get("/api/live/equity").json()["ts_ms"]
        assert stamps == sorted(stamps)

    def test_drawdown_is_the_distance_below_the_running_peak(self, filled: TestClient) -> None:
        body = filled.get("/api/live/equity").json()
        assert body["drawdown"][0] == pytest.approx(0.0)  # the first point is its own peak
        assert body["drawdown"][1] == pytest.approx(9.87 - 9.87)
        assert all(value <= 0 for value in body["drawdown"])

    def test_it_honours_the_same_filters_as_the_table(self, filled: TestClient) -> None:
        """The chart under the table has to be the same rows the table is showing."""
        body = filled.get("/api/live/equity?coin=ETH").json()
        assert len(body["ts_ms"]) == 2
        assert body["equity"][0] == pytest.approx(3099.54)


# --- submit to ack latency ------------------------------------------------------


class TestTheLiveLatencyEndpoint:
    """Submit-to-ack is measured by the trading loop and by nothing else.

    The recorder pings a websocket and calls REST; it never places an order, so its
    snapshot's submit_ack percentiles are permanently None. The Live page therefore
    reads the loop's own latency, which carries all three, rather than the recorder's.
    """

    def test_it_needs_a_session(self, client: TestClient) -> None:
        assert client.get("/api/live/latency").status_code == 401

    def test_it_serves_the_loops_percentiles_and_its_rtt_series(
        self, client: TestClient, settings: Settings
    ) -> None:
        client.post("/api/login", json={"password": PASSWORD})
        snapshot = StateSnapshot(
            proc="paper",
            env="paper",
            risk=RiskState(paused=False),
            latency=LatencyState(
                ws_ping_p50=3.7,
                ws_ping_p95=14.2,
                rest_p50=76.2,
                rest_p95=88.0,
                submit_ack_p50=41.5,
                submit_ack_p95=190.0,
                samples=7,
                ws_ping_samples=[(1_788_784_000_000, 3.7), (1_788_784_020_000, 4.1)],
                rest_samples=[(1_788_784_000_000, 76.2)],
            ),
        )
        write_state(live_state_path(settings), snapshot)

        body = client.get("/api/live/latency").json()

        assert body["submit_ack_p50"] == 41.5
        assert body["submit_ack_p95"] == 190.0
        assert body["ws_ping"] == [[1_788_784_000_000, 3.7], [1_788_784_020_000, 4.1]]
        assert body["rest"] == [[1_788_784_000_000, 76.2]]

    def test_no_loop_running_is_an_empty_chart_not_an_error(self, client: TestClient) -> None:
        """Same rule as the recorder's chart: a gap is the honest picture, not a 503."""
        client.post("/api/login", json={"password": PASSWORD})
        body = client.get("/api/live/latency").json()
        assert body["ws_ping"] == []
        assert body["submit_ack_p50"] is None

    def test_a_loop_that_has_placed_nothing_reports_no_submit_ack(
        self, client: TestClient, settings: Settings
    ) -> None:
        """None is "not measured", which the page must not draw as a zero."""
        client.post("/api/login", json={"password": PASSWORD})
        write_state(
            live_state_path(settings),
            StateSnapshot(
                proc="paper",
                env="paper",
                risk=RiskState(paused=False),
                latency=LatencyState(ws_ping_p50=3.0),
            ),
        )
        body = client.get("/api/live/latency").json()
        assert body["ws_ping_p50"] == 3.0
        assert body["submit_ack_p50"] is None
        assert body["submit_ack_p95"] is None


# --- controls -------------------------------------------------------------------


@pytest.fixture
def loop_socket(settings: Settings) -> Iterator[NoopBackend]:
    """A real ControlServer on the path the panel expects, behind a NoopBackend.

    Real socket, real wire format, real ControlClient: what is worth testing is that the
    panel reaches the loop's socket and reports what came back, and a mocked client would
    test neither. NoopBackend records every call and places nothing.

    On its own thread and its own event loop, because TestClient's calls block the
    caller: served from the test's loop, the blocking POST would starve the accept and
    every request would time out against a socket that is listening. The `settings` it
    resolves is whichever one is in scope, so a class that needs a short data_dir gets
    its socket there.
    """
    backend = NoopBackend(env="paper", proc="paper")
    server = ControlServer(backend, live_socket_path(settings))
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.start())
        loop.call_soon(ready.set)
        loop.run_forever()

    thread = threading.Thread(target=run, name="fake-control", daemon=True)
    thread.start()
    assert ready.wait(timeout=5.0), "the fake control server never came up"
    try:
        yield backend
    finally:
        asyncio.run_coroutine_threadsafe(server.stop(), loop).result(timeout=5.0)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5.0)


class TestControls:
    """Pause, resume and flat, through the loop's own control socket.

    There is no kill button here on purpose: `/kill` stays Telegram and the flag file.
    A kill is the one action that must work when the panel is what is broken, and a
    button in a browser tab is the easiest thing in this system to click by accident.
    """

    @pytest.fixture
    def settings(self, sock_dir: Path) -> Settings:
        """data_dir on a short path: pytest's tmp_path blows the sockaddr_un limit."""
        return load_settings("paper", environ={"LIQFADE_PATHS__DATA_DIR": str(sock_dir)})

    def test_they_need_a_session(self, client: TestClient) -> None:
        assert client.post("/api/live/pause", json={"confirm": True}).status_code == 401
        assert client.post("/api/live/resume", json={"confirm": True}).status_code == 401
        assert client.post("/api/live/flat", json={"confirm": True}).status_code == 401

    def test_no_route_can_send_a_kill(self, client: TestClient, loop_socket: NoopBackend) -> None:
        """The rule is that no HTTP request reaches the loop's kill, not that one 404s.

        The SPA catch-all answers unknown paths, so the status is 405 rather than 404;
        what matters is the command the loop never received.
        """
        client.post("/api/login", json={"password": PASSWORD})

        assert client.post("/api/live/kill", json={"confirm": True}).status_code >= 400

        assert loop_socket.calls == []

    def test_pause_reaches_the_loop_and_reports_what_came_back(
        self, client: TestClient, loop_socket: NoopBackend
    ) -> None:
        client.post("/api/login", json={"password": PASSWORD})

        body = client.post("/api/live/pause", json={"confirm": True}).json()

        assert body["ok"] is True
        assert body["result"]["paused"] is True
        assert [name for name, _ in loop_socket.calls] == ["pause"]
        # Who asked, so the loop's own log says a human did this from the panel.
        assert loop_socket.calls[0][1]["reason"] == "panel"

    def test_resume_reaches_the_loop(self, client: TestClient, loop_socket: NoopBackend) -> None:
        client.post("/api/login", json={"password": PASSWORD})
        body = client.post("/api/live/resume", json={"confirm": True}).json()
        assert body["result"]["paused"] is False
        assert [name for name, _ in loop_socket.calls] == ["resume"]

    def test_flat_closes_everything_when_no_coin_is_named(
        self, client: TestClient, loop_socket: NoopBackend
    ) -> None:
        client.post("/api/login", json={"password": PASSWORD})
        body = client.post("/api/live/flat", json={"confirm": True}).json()
        assert body["ok"] is True
        assert loop_socket.calls == [("flat", {"coin": None})]

    def test_flat_takes_one_coin(self, client: TestClient, loop_socket: NoopBackend) -> None:
        client.post("/api/login", json={"password": PASSWORD})
        body = client.post("/api/live/flat", json={"confirm": True, "coin": "btc"}).json()
        assert body["result"]["closing"] == ["BTC"]  # upper cased on the way in
        assert loop_socket.calls == [("flat", {"coin": "BTC"})]

    def test_an_unconfirmed_action_is_refused_and_never_reaches_the_loop(
        self, client: TestClient, loop_socket: NoopBackend
    ) -> None:
        """The confirm step is enforced here, not only in the browser.

        A page can be reloaded mid-dialog and a curl has no dialog at all. Flattening is
        not something to do because a request arrived.
        """
        client.post("/api/login", json={"password": PASSWORD})

        response = client.post("/api/live/flat", json={"confirm": False})

        assert response.status_code == 400
        assert "confirm" in response.json()["detail"]
        assert loop_socket.calls == []

    def test_no_loop_running_is_a_503_that_says_so(self, client: TestClient) -> None:
        """Nothing is listening: the page has to say the loop is down, not "failed"."""
        client.post("/api/login", json={"password": PASSWORD})
        response = client.post("/api/live/pause", json={"confirm": True})
        assert response.status_code == 503
        assert "control socket" in response.json()["detail"]

    def test_every_action_is_logged_with_its_result(
        self, client: TestClient, loop_socket: NoopBackend, settings: Settings
    ) -> None:
        """The audit log is the record of who changed what, and it is what the page shows."""
        client.post("/api/login", json={"password": PASSWORD})

        client.post("/api/live/flat", json={"confirm": True, "coin": "ETH"})

        entries = client.get("/api/audit").json()["entries"]
        assert len(entries) == 1
        assert entries[0]["action"] == "flat"
        assert entries[0]["args"] == {"coin": "ETH"}
        assert entries[0]["ok"] is True
        assert entries[0]["result"]["closing"] == ["ETH"]
        assert entries[0]["ts_ms"] > 0


# --- settings writes ------------------------------------------------------------


class TestSettingsWrites:
    """The only thing in this system that changes a running config from a browser.

    Four rules, each enforced here rather than in the frontend, because a page can be
    reloaded mid-flow and a curl has no frontend at all: an allowlist of writable
    sections, a typed confirmation for anything that decides how much money is at risk,
    a validation pass through the same model the YAML goes through, and an audit entry
    whatever the outcome.
    """

    @pytest.fixture
    def settings(self, sock_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
        """data_dir on a short path, set in the environment rather than passed in.

        The endpoint resolves the config from the process environment, the way the real
        panel does, so a data_dir the test knows about and the process does not would
        have the two looking at different overrides files.
        """
        monkeypatch.setenv("LIQFADE_PATHS__DATA_DIR", str(sock_dir))
        return load_settings("paper")

    @pytest.fixture
    def signed_in(self, client: TestClient) -> TestClient:
        client.post("/api/login", json={"password": PASSWORD})
        return client

    def test_it_needs_a_session(self, client: TestClient) -> None:
        assert client.post("/api/config", json={"changes": {}}).status_code == 401

    def test_the_config_endpoint_now_says_it_is_writable_and_where(
        self, signed_in: TestClient, settings: Settings
    ) -> None:
        body = signed_in.get("/api/config").json()
        assert body["writable"] is True
        assert body["overrides_path"] == str(settings.paths.data_dir / "overrides.yaml")
        # The sections a settings page may touch, so the form is built from the answer
        # rather than from a list the frontend keeps its own copy of.
        assert set(body["writable_sections"]) == {
            "cascade",
            "exhaustion",
            "filters",
            "intent",
            "risk",
            "sizing",
            "strategy",
            "universe",
        }

    def test_a_dry_run_returns_the_diff_and_writes_nothing(
        self, signed_in: TestClient, settings: Settings
    ) -> None:
        """The preview is the write, minus the write, so they cannot disagree."""
        body = signed_in.post(
            "/api/config",
            json={"changes": {"intent.k_target": 1.4}, "reason": "wider targets", "dry_run": True},
        ).json()

        assert body["ok"] is True
        assert body["diff"] == [
            {"key": "intent.k_target", "before": 0.8, "after": 1.4, "source": "default.yaml"}
        ]
        assert not (settings.paths.data_dir / "overrides.yaml").exists()

    def test_a_write_lands_in_the_overrides_file_with_its_timestamp_and_reason(
        self, signed_in: TestClient, settings: Settings
    ) -> None:
        import yaml

        response = signed_in.post(
            "/api/config",
            json={"changes": {"intent.k_target": 1.4}, "reason": "wider targets"},
        )

        assert response.status_code == 200
        path = settings.paths.data_dir / "overrides.yaml"
        written = yaml.safe_load(path.read_text())
        assert written["intent"]["k_target"] == 1.4
        header = path.read_text().splitlines()[0]
        assert "wider targets" in path.read_text()
        assert header.startswith("#")  # the timestamp and reason are a comment, not config

    def test_a_second_write_keeps_the_first(
        self, signed_in: TestClient, settings: Settings
    ) -> None:
        """Two changes on different days are two overrides, not the second one only."""
        import yaml

        signed_in.post("/api/config", json={"changes": {"intent.k_target": 1.4}, "reason": "a"})
        signed_in.post("/api/config", json={"changes": {"intent.k_stop": 0.9}, "reason": "b"})

        written = yaml.safe_load((settings.paths.data_dir / "overrides.yaml").read_text())
        assert written["intent"] == {"k_target": 1.4, "k_stop": 0.9}

    def test_the_written_config_still_loads(
        self, signed_in: TestClient, settings: Settings
    ) -> None:
        """What the panel writes has to be a config the next process start can read."""
        signed_in.post(
            "/api/config",
            json={
                "changes": {"sizing.risk_frac": 0.004},
                "reason": "r",
                "confirm": {"sizing.risk_frac": "sizing.risk_frac"},
            },
        )

        reloaded = load_settings(
            "paper", environ={"LIQFADE_PATHS__DATA_DIR": str(settings.paths.data_dir)}
        )

        assert reloaded.sizing.risk_frac == 0.004

    # --- what it refuses ---------------------------------------------------------

    def test_a_value_the_model_refuses_is_refused_before_it_is_written(
        self, signed_in: TestClient, settings: Settings
    ) -> None:
        """Validated through the same model as the YAML, so the page cannot write a
        config that only fails at the next process start."""
        response = signed_in.post(
            "/api/config", json={"changes": {"sizing.risk_frac": 5.0}, "reason": "oops"}
        )

        assert response.status_code == 400
        assert "risk_frac" in response.json()["detail"]
        assert not (settings.paths.data_dir / "overrides.yaml").exists()

    def test_a_cross_field_rule_is_refused_too(self, signed_in: TestClient) -> None:
        """One field at a time is valid; the pair is not. Only whole-model validation
        catches that, which is why the check is a load and not a field lookup."""
        response = signed_in.post(
            "/api/config", json={"changes": {"risk.kill_budget_s": 30.0}, "reason": "oops"}
        )
        assert response.status_code == 400
        assert "kill_budget_s" in response.json()["detail"]

    @pytest.mark.parametrize(
        "key",
        [
            "env",
            "execution.entry_tif",
            "panel.allow_non_loopback",
            "paths.data_dir",
            "venues.hyperliquid.info_base",
            "telegram.enabled",
        ],
    )
    def test_only_the_allowlisted_sections_may_be_written(
        self, signed_in: TestClient, key: str
    ) -> None:
        """An allowlist, not a blocklist. Nothing on this page can enable live trading,
        and the way to be sure of that is that it cannot write anything it was not
        named for, rather than that someone remembered every key that would do it."""
        response = signed_in.post("/api/config", json={"changes": {key: "x"}, "reason": "no"})

        assert response.status_code == 400
        assert "not writable" in response.json()["detail"]

    def test_a_risk_field_needs_its_own_name_typed_back(
        self, signed_in: TestClient, settings: Settings
    ) -> None:
        response = signed_in.post(
            "/api/config",
            json={"changes": {"risk.max_daily_loss_frac": 0.05}, "reason": "tighter"},
        )

        assert response.status_code == 400
        assert "risk.max_daily_loss_frac" in response.json()["detail"]
        assert not (settings.paths.data_dir / "overrides.yaml").exists()

    def test_the_typed_confirmation_has_to_be_the_right_name(self, signed_in: TestClient) -> None:
        response = signed_in.post(
            "/api/config",
            json={
                "changes": {"risk.max_daily_loss_frac": 0.05},
                "reason": "tighter",
                "confirm": {"risk.max_daily_loss_frac": "risk.max_dd_frac"},
            },
        )
        assert response.status_code == 400

    def test_a_confirmed_risk_field_is_written(
        self, signed_in: TestClient, settings: Settings
    ) -> None:
        import yaml

        response = signed_in.post(
            "/api/config",
            json={
                "changes": {"risk.max_daily_loss_frac": 0.05},
                "reason": "tighter after the drawdown",
                "confirm": {"risk.max_daily_loss_frac": "risk.max_daily_loss_frac"},
            },
        )

        assert response.status_code == 200
        written = yaml.safe_load((settings.paths.data_dir / "overrides.yaml").read_text())
        assert written["risk"]["max_daily_loss_frac"] == 0.05

    def test_sizing_counts_as_a_risk_field(self, signed_in: TestClient) -> None:
        """Sizing decides how much is at risk, so it gets the same typed confirmation."""
        response = signed_in.post(
            "/api/config", json={"changes": {"sizing.risk_frac": 0.004}, "reason": "smaller"}
        )
        assert response.status_code == 400
        assert "sizing.risk_frac" in response.json()["detail"]

    def test_a_key_an_env_var_already_wins_is_refused(
        self, sock_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An override under an env var is a change the owner would watch do nothing.

        The env layer merges last, so writing the file would leave the page showing a
        value the process never adopts. Better to say so than to write it.
        """
        settings = load_settings(
            "paper",
            environ={
                "LIQFADE_PATHS__DATA_DIR": str(sock_dir),
                "LIQFADE_INTENT__K_TARGET": "1.9",
            },
        )
        monkeypatch.setenv("LIQFADE_INTENT__K_TARGET", "1.9")
        auth = SessionAuth(password_hash=hash_password(PASSWORD), secret="s", ttl_s=60)
        with TestClient(create_app(settings, auth)) as client:
            client.post("/api/login", json={"password": PASSWORD})
            response = client.post(
                "/api/config", json={"changes": {"intent.k_target": 1.4}, "reason": "no"}
            )

        assert response.status_code == 400
        assert "env var" in response.json()["detail"]

    def test_a_write_with_no_reason_is_refused(self, signed_in: TestClient) -> None:
        """The audit log's whole value is the why; a blank one makes it a diff."""
        response = signed_in.post("/api/config", json={"changes": {"intent.k_target": 1.4}})
        assert response.status_code == 422

    def test_an_empty_change_set_is_refused(self, signed_in: TestClient) -> None:
        response = signed_in.post("/api/config", json={"changes": {}, "reason": "nothing"})
        assert response.status_code == 400

    # --- the reload and the audit trail ------------------------------------------

    def test_a_write_reloads_the_loop_and_reports_what_it_said(
        self, signed_in: TestClient, loop_socket: NoopBackend
    ) -> None:
        body = signed_in.post(
            "/api/config", json={"changes": {"intent.k_target": 1.4}, "reason": "wider"}
        ).json()

        assert [name for name, _ in loop_socket.calls] == ["reload_config"]
        assert body["reload"]["error"] is None

    def test_a_loop_that_refuses_the_reload_does_not_undo_the_write(
        self, signed_in: TestClient, settings: Settings
    ) -> None:
        """Nothing is listening, or the loop says it cannot reload. Either way the file
        is written and the page says the running process has not picked it up: an
        override that vanishes because the loop was down would be worse than a stale one.
        """
        body = signed_in.post(
            "/api/config", json={"changes": {"intent.k_target": 1.4}, "reason": "wider"}
        ).json()

        assert body["ok"] is True
        assert (settings.paths.data_dir / "overrides.yaml").exists()
        assert "control socket" in body["reload"]["error"]

    def test_every_write_is_audited_with_its_before_and_after(self, signed_in: TestClient) -> None:
        signed_in.post(
            "/api/config", json={"changes": {"intent.k_target": 1.4}, "reason": "wider targets"}
        )

        entry = signed_in.get("/api/audit").json()["entries"][0]

        assert entry["action"] == "settings"
        assert entry["reason"] == "wider targets"
        assert entry["args"]["changed"] == {"intent.k_target": 1.4}
        assert entry["args"]["diff"] == [
            {"key": "intent.k_target", "before": 0.8, "after": 1.4, "source": "default.yaml"}
        ]

    def test_a_refused_write_is_audited_too(self, signed_in: TestClient) -> None:
        """What the owner tried is as worth reading back as what they managed."""
        signed_in.post("/api/config", json={"changes": {"env": "mainnet"}, "reason": "no"})

        entry = signed_in.get("/api/audit").json()["entries"][0]

        assert entry["action"] == "settings"
        assert entry["ok"] is False
        assert "not writable" in entry["error"]
