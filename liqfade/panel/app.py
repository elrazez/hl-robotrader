"""FastAPI backend for the panel.

Runs as its own process. The trading loop never serves HTTP; the panel only reads
data/state.json, and in later phases the sqlite order store and Parquet through duckdb.
It deliberately does not open the control socket for /api/state: a slow HTTP client must
never be able to make the trading loop wait.

Binding: loopback only unless panel.allow_non_loopback is explicitly true. The panel is
reached over Tailscale or an SSH tunnel, never exposed directly.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from liqfade.config import config_layers, flatten, resolve
from liqfade.config.models import Settings
from liqfade.control.protocol import LatencyState, now_ms
from liqfade.control.state import read_state
from liqfade.log import get_logger, setup_logging
from liqfade.panel.auth import COOKIE_NAME, LoginLimiter, SessionAuth

log = get_logger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


class LoginBody(BaseModel):
    password: str = Field(..., min_length=1, max_length=1024)


def is_loopback(host: str) -> bool:
    """True only for a literal loopback address or `localhost`.

    The empty string is not loopback: uvicorn hands it straight to socket.bind, which
    reads it as INADDR_ANY, so `panel.host: ""` used to pass this guard and then listen
    on every interface. The panel is reached over Tailscale or an SSH tunnel and must
    never bind a public address by accident.
    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


HOUR_MS = 3_600_000
MINUTE_MS = 60_000
# Trades returned in one page. The owner scrolls a table; a year of runs is not a dataset
# to hand a browser in one response.
MAX_TRADES_PAGE = 2000


def _run_dir(reports_dir: Path, run_id: str) -> Path:
    """Resolve a run id to its directory, refusing anything that escapes `reports/`.

    `run_id` arrives from the URL, so it is untrusted input used to build a path. A id of
    `../../etc` would otherwise read files outside the reports directory entirely. The
    check is on the resolved path rather than on the string, because that is the only form
    that cannot be tricked by encoding.
    """
    root = Path(reports_dir).resolve()
    directory = (root / run_id).resolve()
    if not directory.is_relative_to(root) or not directory.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no run {run_id!r}")
    return directory


def _read_summary(directory: Path) -> dict[str, Any]:
    """One run's sidecar, or an `error` field saying why it could not be read."""
    import orjson

    path = directory / "summary.json"
    try:
        loaded: dict[str, Any] = orjson.loads(path.read_bytes())
    except FileNotFoundError:
        return {"error": "summary.json missing"}
    except (ValueError, OSError) as exc:
        return {"error": f"summary.json unreadable: {exc}"}
    return loaded


def list_reports(reports_dir: Path, *, limit: int = 50) -> list[dict[str, Any]]:
    """Run directories with their headline metrics, newest first.

    Sorted by directory name, which begins with a UTC timestamp, so newest first is a
    reverse sort rather than a stat of every directory.
    """
    root = Path(reports_dir)
    if not root.is_dir():
        return []
    directories = sorted(
        (path for path in root.iterdir() if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    )[: max(1, limit)]
    runs = []
    for directory in directories:
        summary = _read_summary(directory)
        metrics = summary.get("metrics", {})
        runs.append(
            {
                "run_id": directory.name,
                "error": summary.get("error"),
                "trigger": summary.get("trigger"),
                "env": summary.get("env"),
                "start_ms": summary.get("start_ms"),
                "end_ms": summary.get("end_ms"),
                "coins": summary.get("coins", []),
                "latency_fragile": summary.get("latency_fragile"),
                "trades": metrics.get("trades"),
                "net_pnl": metrics.get("net_pnl"),
                "avg_net_bps": metrics.get("avg_net_bps"),
                "hit_rate": metrics.get("hit_rate"),
                "sharpe_per_trade": metrics.get("sharpe_per_trade"),
                "max_drawdown_frac": metrics.get("max_drawdown_frac"),
                "top5_pnl_share": metrics.get("top5_pnl_share"),
                "no_book_share": metrics.get("no_book_share"),
            }
        )
    return runs


def read_trades(directory: Path, *, limit: int, offset: int) -> dict[str, Any]:
    """One page of `trades.parquet`, ordered by entry time."""
    import polars as pl

    path = directory / "trades.parquet"
    if not path.exists():
        return {"total": 0, "offset": 0, "trades": []}
    frame = pl.read_parquet(path).sort("entry_ms")
    total = frame.height
    page = frame.slice(max(offset, 0), max(1, min(limit, MAX_TRADES_PAGE)))
    rows = page.to_dicts()
    for position, row in enumerate(rows):
        # The index the trade detail endpoint takes, which is the row's place in entry
        # order rather than its place in this page.
        row["index"] = max(offset, 0) + position
    return {"total": total, "offset": max(offset, 0), "trades": rows}


def equity_curve(directory: Path) -> dict[str, Any]:
    """Cumulative net PnL at each exit, as uplot shaped parallel arrays."""
    import polars as pl

    path = directory / "trades.parquet"
    if not path.exists():
        return {"ts_ms": [], "equity": [], "drawdown": []}
    frame = pl.read_parquet(path).sort("exit_ms")
    if frame.is_empty():
        return {"ts_ms": [], "equity": [], "drawdown": []}
    stamps = frame.get_column("exit_ms").to_list()
    cumulative = frame.get_column("net_pnl").cum_sum().to_list()
    peak, drawdown = float("-inf"), []
    for value in cumulative:
        peak = max(peak, value)
        drawdown.append(value - peak)
    return {"ts_ms": stamps, "equity": cumulative, "drawdown": drawdown}


def trade_window(
    directory: Path, parquet_root: Path, *, index: int, before_min: int
) -> dict[str, Any]:
    """One trade, the tape around it, and the liquidations that triggered it.

    Candles come from `candles_1m` where the lake has them and are derived from `trades`
    where it does not, so a window still renders on a day the derived table was never
    built. Both come through duckdb over the partition files, which needs no running
    process: the lake is the database.
    """
    import polars as pl

    path = directory / "trades.parquet"
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no trades.parquet for this run")
    frame = pl.read_parquet(path).sort("entry_ms")
    if not 0 <= index < frame.height:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no trade {index} in this run")

    trade = frame.row(index, named=True)
    coin = str(trade["coin"])
    # From before the cascade rather than before the entry: the flow that triggered the
    # trade is the thing worth looking at, and under a fixed delay entry it is already two
    # minutes in the past by the time the order goes in.
    start_ms = int(trade["entry_ms"]) - before_min * MINUTE_MS
    end_ms = int(trade["exit_ms"]) + MINUTE_MS
    return {
        "trade": trade,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "candles": _candles(parquet_root, coin, start_ms, end_ms),
        "liquidations": _liquidations(parquet_root, coin, start_ms, end_ms),
    }


def _window_files(parquet_root: Path, table: str, start_ms: int, end_ms: int) -> list[str]:
    from liqfade.backtest.data import day_files, utc_days

    return [
        str(path)
        for day in utc_days(start_ms, end_ms)
        for path in day_files(parquet_root.parent, table, day)
    ]


def _candles(parquet_root: Path, coin: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    """One minute OHLC over the window, from candles_1m or derived from the tape."""
    import duckdb

    files = _window_files(parquet_root, "candles_1m", start_ms, end_ms)
    connection = duckdb.connect()
    if files:
        rows = connection.execute(
            """
            select ts_ms, o, h, l, c, v from read_parquet(?, union_by_name = true)
            where coin = ? and ts_ms between ? and ? order by ts_ms
            """,
            [files, coin, start_ms, end_ms],
        ).fetchall()
        if rows:
            return [
                {"ts_ms": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5]} for r in rows
            ]

    tape = _window_files(parquet_root, "trades", start_ms, end_ms)
    if not tape:
        return []
    rows = connection.execute(
        """
        select (ts_ms // ?) * ? as minute,
               first(px order by ts_ms) as o, max(px) as h, min(px) as l,
               last(px order by ts_ms) as c, sum(sz) as v
        from read_parquet(?, union_by_name = true)
        where coin = ? and ts_ms between ? and ?
        group by 1 order by 1
        """,
        [MINUTE_MS, MINUTE_MS, tape, coin, start_ms, end_ms],
    ).fetchall()
    return [{"ts_ms": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5]} for r in rows]


def _liquidations(
    parquet_root: Path, coin: str, start_ms: int, end_ms: int
) -> list[dict[str, Any]]:
    """Every venue's liquidations in the window, as chart markers."""
    import duckdb

    files = _window_files(parquet_root, "liquidations", start_ms, end_ms)
    if not files:
        return []
    rows = (
        duckdb.connect()
        .execute(
            """
            select ts_ms, venue, side, px, notional_usd
            from read_parquet(?, union_by_name = true)
            where coin = ? and ts_ms between ? and ? order by ts_ms
            """,
            [files, coin, start_ms, end_ms],
        )
        .fetchall()
    )
    return [
        {"ts_ms": r[0], "venue": r[1], "side": r[2], "px": r[3], "notional_usd": r[4]} for r in rows
    ]


def liquidations_by_venue(
    parquet_root: Path, hours: int, *, now: int | None = None
) -> dict[str, Any]:
    """Aggregate the `liquidations` table into hourly notional per venue.

    Returns uplot shaped data: the complete hour grid for the window and one aligned list
    of values per venue, with quiet hours as nulls. The grid is always complete so the
    chart's x axis spans the window the caller asked for, rather than collapsing onto
    whichever two hours happened to have a liquidation in them.

    `now` pins the end of the grid, in ms. It exists so tests are not racing the wall
    clock rolling over an hour between writing a row and reading it back.

    Files come from `lake_files`, never a `date=*/*.parquet` glob: on the owner's drive
    macOS leaves a 4 KiB AppleDouble `._x.parquet` beside every file our code writes, and
    `read_parquet` fails on one with "No magic bytes found at end of file".
    """
    import duckdb

    from liqfade.recorder.writer import lake_files

    end_hour = ((now_ms() if now is None else now) // HOUR_MS) * HOUR_MS
    since_ms = end_hour - (hours - 1) * HOUR_MS
    # Bounded at both ends. A row with a timestamp past the window (a venue clock ahead of
    # ours, a bad parse) has no bucket in the grid below, and looking it up used to raise
    # a KeyError and 500 the panel's main chart.
    until_ms = end_hour + HOUR_MS - 1
    hour_list = [since_ms + step * HOUR_MS for step in range(hours)]
    empty: dict[str, Any] = {
        "hours": hours,
        "hour_ms": hour_list,
        "venues": {},
        "totals": {},
        "events": {},
    }
    partitions = sorted(
        path
        for directory in (parquet_root / "liquidations").glob("date=*")
        for path in lake_files(directory)
    )
    if not partitions:
        return empty

    rows = (
        duckdb.connect()
        .execute(
            """
            select venue, (ts_ms // ?) * ? as hour_ms,
                   sum(notional_usd) as notional, count(*) as events
            from read_parquet(?, union_by_name = true)
            where ts_ms between ? and ?
            group by 1, 2
            """,
            [HOUR_MS, HOUR_MS, [str(path) for path in partitions], since_ms, until_ms],
        )
        .fetchall()
    )
    if not rows:
        return empty

    index = {hour: position for position, hour in enumerate(hour_list)}
    venues: dict[str, list[float | None]] = {}
    totals: dict[str, float] = {}
    events: dict[str, int] = {}
    for venue, hour_ms, notional, count in rows:
        series = venues.setdefault(venue, [None] * len(hour_list))
        series[index[int(hour_ms)]] = float(notional or 0.0)
        totals[venue] = totals.get(venue, 0.0) + float(notional or 0.0)
        events[venue] = events.get(venue, 0) + int(count)
    return {
        "hours": hours,
        "hour_ms": hour_list,
        "venues": dict(sorted(venues.items())),
        "totals": dict(sorted(totals.items())),
        "events": dict(sorted(events.items())),
    }


def create_app(settings: Settings, auth: SessionAuth) -> FastAPI:
    app = FastAPI(title="liqfade panel", docs_url=None, redoc_url=None, openapi_url=None)
    limiter = LoginLimiter(
        max_attempts=settings.panel.login_max_attempts,
        window_s=settings.panel.login_window_s,
    )
    api = APIRouter(prefix="/api")

    def require_session(request: Request) -> None:
        if not auth.valid(request.cookies.get(COOKIE_NAME)):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "login required")

    @api.get("/health")
    def health() -> dict[str, Any]:
        """Unauthenticated on purpose so systemd and a browser tab can both check it."""
        return {"ok": True, "env": settings.env, "ts_ms": now_ms()}

    @api.post("/login")
    def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
        key = request.client.host if request.client else "unknown"
        if limiter.blocked(key):
            log.warning("panel_login_locked_out", client=key)
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts, wait")

        token = auth.login(body.password)
        if token is None:
            limiter.record_failure(key)
            log.warning("panel_login_failed", client=key)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong password")

        limiter.reset(key)
        log.info("panel_login_ok", client=key)
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=int(settings.panel.session_ttl_s),
            httponly=True,
            samesite="strict",
            # No secure flag: the panel is served over plain http on loopback and the
            # transport is the SSH tunnel or Tailscale, not TLS.
        )
        return {"ok": True}

    @api.post("/logout")
    def logout(response: Response) -> dict[str, Any]:
        response.delete_cookie(COOKIE_NAME)
        return {"ok": True}

    @api.get("/session")
    def session(request: Request) -> dict[str, Any]:
        return {"authenticated": auth.valid(request.cookies.get(COOKIE_NAME))}

    @api.get("/state", dependencies=[Depends(require_session)])
    def state() -> JSONResponse:
        """The snapshot from data/state.json, or 503 when it is missing or stale.

        Stale is an error rather than a warning field because every number on the page is
        a trading number: showing a five second old position quietly is worse than an
        obvious outage.
        """
        try:
            snapshot = read_state(settings.state_path)
        except FileNotFoundError:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"no state file at {settings.state_path}: no liqfade process is running",
            ) from None
        except (ValueError, OSError) as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, f"state file unreadable: {exc}"
            ) from None

        age_s = (now_ms() - snapshot.ts_ms) / 1000.0
        payload = snapshot.model_dump(mode="json") | {"age_s": age_s}
        # The latency sample series belongs to /api/recorder/latency, which the page polls
        # every thirty seconds. This one is polled every second, and several hundred
        # points would be the whole payload, over an SSH tunnel, for a chart that has not
        # moved.
        payload["latency"] = {
            key: value for key, value in payload["latency"].items() if not key.endswith("_samples")
        }
        if age_s > settings.panel.state_stale_s:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "detail": (
                        f"state is {age_s:.1f}s old, over the {settings.panel.state_stale_s}s "
                        "limit: the writing process is stalled or gone"
                    ),
                    "state": payload,
                },
            )
        return JSONResponse(payload)

    @api.get("/recorder/disk", dependencies=[Depends(require_session)])
    def recorder_disk() -> dict[str, Any]:
        """How full the data volume is. Same numbers the recorder alerts on."""
        # Imported here so the panel process does not pull the recorder's websocket
        # stack in just to call shutil.disk_usage.
        from liqfade.recorder.health import disk_stats

        return disk_stats(settings.paths.data_dir)

    @api.get("/recorder/latency", dependencies=[Depends(require_session)])
    def recorder_latency() -> dict[str, Any]:
        """WS ping and REST round trips as time series, plus the percentiles over them.

        The retained window straight out of the snapshot, as `[ts_ms, ms]` pairs. Read
        from `data/state.json` like everything else the panel shows, so a slow browser
        can never make the recorder wait. No recorder running is an empty chart rather
        than a 503: the page shows a gap, which is the honest picture.
        """
        try:
            latency = read_state(settings.state_path).latency
        except (FileNotFoundError, ValueError, OSError):
            latency = LatencyState()
        return {
            "ws_ping": [list(sample) for sample in latency.ws_ping_samples],
            "rest": [list(sample) for sample in latency.rest_samples],
            "ws_ping_p50": latency.ws_ping_p50,
            "ws_ping_p95": latency.ws_ping_p95,
            "rest_p50": latency.rest_p50,
            "rest_p95": latency.rest_p95,
        }

    @api.get("/recorder/liquidations", dependencies=[Depends(require_session)])
    def recorder_liquidations(hours: int = 24) -> dict[str, Any]:
        """Hourly liquidation notional per venue, straight off the Parquet lake.

        duckdb reads the partition files directly, so this needs no running recorder and
        no database: the lake is the database. Bounded to a week because the panel is a
        monitor, not a research tool; research reads the same files from a notebook.
        """
        hours = max(1, min(hours, 168))
        return liquidations_by_venue(settings.paths.data_dir / "parquet", hours)

    @api.get("/reports", dependencies=[Depends(require_session)])
    def reports(limit: int = 50) -> dict[str, Any]:
        """Every run under `reports/`, newest first, from the `summary.json` sidecars.

        The sidecar exists precisely so this does not parse markdown. A run whose sidecar
        is missing or unreadable is listed with an `error` rather than dropped: a report
        the owner can see is broken is more useful than one that silently is not there.
        """
        return {"runs": list_reports(settings.paths.reports_dir, limit=limit)}

    @api.get("/reports/{run_id}", dependencies=[Depends(require_session)])
    def report(run_id: str) -> dict[str, Any]:
        """One run's summary plus its markdown, for the report view."""
        directory = _run_dir(settings.paths.reports_dir, run_id)
        summary = _read_summary(directory)
        markdown = (
            (directory / "report.md").read_text() if (directory / "report.md").exists() else ""
        )
        return {"run_id": run_id, "summary": summary, "markdown": markdown}

    @api.get("/reports/{run_id}/trades", dependencies=[Depends(require_session)])
    def report_trades(run_id: str, limit: int = 500, offset: int = 0) -> dict[str, Any]:
        """A page of the run's trades, straight out of `trades.parquet`."""
        directory = _run_dir(settings.paths.reports_dir, run_id)
        return read_trades(directory, limit=limit, offset=offset)

    @api.get("/reports/{run_id}/equity", dependencies=[Depends(require_session)])
    def report_equity(run_id: str) -> dict[str, Any]:
        """The equity curve, rebuilt from the trades so no extra file is needed.

        Cumulative net PnL at each exit, which is the same shape as the engine's own curve
        at the resolution a chart can use, and it comes from the file the panel already
        reads rather than from a second artifact that could disagree with it.
        """
        directory = _run_dir(settings.paths.reports_dir, run_id)
        return equity_curve(directory)

    @api.get("/reports/{run_id}/trade/{index}/window", dependencies=[Depends(require_session)])
    def report_trade_window(run_id: str, index: int, before_min: int = 10) -> dict[str, Any]:
        """Candles, liquidations and the entry and exit marks around one trade.

        The window runs from `before_min` before the cascade started to the exit, which is
        what makes a trade arguable after the fact: the chart shows the flow that triggered
        it, not just the fill.
        """
        directory = _run_dir(settings.paths.reports_dir, run_id)
        return trade_window(
            directory,
            settings.paths.data_dir / "parquet",
            index=index,
            before_min=before_min,
        )

    @api.get("/config", dependencies=[Depends(require_session)])
    def config() -> dict[str, Any]:
        """Read only for now. Phase 5 adds validated writes to config/overrides.yaml.

        Both views are returned. `values` is the resolved config with the label of the
        layer that won each key, which is what the page displays. `layers` is what each
        layer set, in merge order, which is what a settings page needs before it offers to
        change a number: the flattened resolution alone cannot say what a value is being
        changed from or what removing an override would go back to.
        """
        merged, sources = resolve(settings.env)
        flat = flatten(merged)
        return {
            "env": settings.env,
            "live_trading_enabled": settings.live_trading_enabled,
            "live_trading_block_reason": settings.live_trading_block_reason(),
            "values": [
                {"key": k, "value": flat[k], "source": sources.get(k)} for k in sorted(flat)
            ],
            "layers": [
                {
                    "name": layer.name,
                    "path": layer.path,
                    "exists": layer.exists,
                    "values": layer.values,
                }
                for layer in config_layers(settings.env)
            ],
            "writable": False,
        }

    app.include_router(api)

    index = STATIC_DIR / "index.html"
    if index.exists():
        app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

        @app.get("/{_path:path}", include_in_schema=False)
        def spa(_path: str) -> FileResponse:
            """Serve the single page app for every non /api route."""
            return FileResponse(index)
    else:  # pragma: no cover - only when the bundle was never built

        @app.get("/", include_in_schema=False)
        def missing_bundle() -> JSONResponse:
            return JSONResponse(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                content={"detail": "panel bundle not built: run `make frontend`"},
            )

    return app


def serve(settings: Settings) -> None:
    """Start uvicorn. Refuses to bind a non loopback address without an explicit opt in."""
    import uvicorn

    setup_logging(proc="panel", env=settings.env, level=settings.logging.level)

    host = settings.panel.host
    if not is_loopback(host) and not settings.panel.allow_non_loopback:
        raise SystemExit(
            f"panel.host is {host!r}, which is not loopback. The panel must be reached over "
            "Tailscale or an SSH tunnel. Set panel.allow_non_loopback=true only if you have "
            "a firewall in front of it."
        )

    auth = SessionAuth.from_env(settings.panel.session_ttl_s)
    app = create_app(settings, auth)
    log.info("panel_serving", host=host, port=settings.panel.port, env=settings.env)
    # No reload flag: uvicorn ignores it when it is handed an app object rather than an
    # import string, so the option only ever promised something it did not do.
    uvicorn.run(app, host=host, port=settings.panel.port, log_config=None)
