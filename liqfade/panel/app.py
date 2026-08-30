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

from liqfade.config import flatten, resolve
from liqfade.config.models import Settings
from liqfade.control.protocol import now_ms
from liqfade.control.state import read_state
from liqfade.log import get_logger, setup_logging
from liqfade.panel.auth import COOKIE_NAME, LoginLimiter, SessionAuth

log = get_logger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


class LoginBody(BaseModel):
    password: str = Field(..., min_length=1, max_length=1024)


def is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


HOUR_MS = 3_600_000


def liquidations_by_venue(parquet_root: Path, hours: int) -> dict[str, Any]:
    """Aggregate the `liquidations` table into hourly notional per venue.

    Returns uplot shaped data: the complete hour grid for the window and one aligned list
    of values per venue, with quiet hours as nulls. The grid is always complete so the
    chart's x axis spans the window the caller asked for, rather than collapsing onto
    whichever two hours happened to have a liquidation in them.
    """
    import duckdb

    end_hour = (now_ms() // HOUR_MS) * HOUR_MS
    since_ms = end_hour - (hours - 1) * HOUR_MS
    hour_list = [since_ms + step * HOUR_MS for step in range(hours)]
    empty: dict[str, Any] = {
        "hours": hours,
        "hour_ms": hour_list,
        "venues": {},
        "totals": {},
        "events": {},
    }
    partitions = sorted((parquet_root / "liquidations").glob("date=*/*.parquet"))
    if not partitions:
        return empty

    rows = (
        duckdb.connect()
        .execute(
            """
            select venue, (ts_ms // ?) * ? as hour_ms,
                   sum(notional_usd) as notional, count(*) as events
            from read_parquet(?, union_by_name = true)
            where ts_ms >= ?
            group by 1, 2
            """,
            [HOUR_MS, HOUR_MS, [str(path) for path in partitions], since_ms],
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

    @api.get("/recorder/liquidations", dependencies=[Depends(require_session)])
    def recorder_liquidations(hours: int = 24) -> dict[str, Any]:
        """Hourly liquidation notional per venue, straight off the Parquet lake.

        duckdb reads the partition files directly, so this needs no running recorder and
        no database: the lake is the database. Bounded to a week because the panel is a
        monitor, not a research tool; research reads the same files from a notebook.
        """
        hours = max(1, min(hours, 168))
        return liquidations_by_venue(settings.paths.data_dir / "parquet", hours)

    @api.get("/config", dependencies=[Depends(require_session)])
    def config() -> dict[str, Any]:
        """Read only for now. Phase 5 adds validated writes to config/overrides.yaml."""
        merged, sources = resolve(settings.env)
        flat = flatten(merged)
        return {
            "env": settings.env,
            "live_trading_enabled": settings.live_trading_enabled,
            "live_trading_block_reason": settings.live_trading_block_reason(),
            "values": [
                {"key": k, "value": flat[k], "source": sources.get(k)} for k in sorted(flat)
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


def serve(settings: Settings, *, reload: bool = False) -> None:
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
    uvicorn.run(app, host=host, port=settings.panel.port, reload=reload, log_config=None)
