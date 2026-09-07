"""Panel auth, the state endpoint's staleness rule and the loopback bind guard."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from liqfade.config import load_settings
from liqfade.config.models import Settings
from liqfade.control.protocol import LatencyState, RiskState, StateSnapshot, now_ms
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
    assert body == {
        "ws_ping": [],
        "rest": [],
        "ws_ping_p50": None,
        "ws_ping_p95": None,
        "rest_p50": None,
        "rest_p95": None,
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
