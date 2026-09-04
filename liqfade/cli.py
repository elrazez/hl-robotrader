"""The `liqfade` command line entry point.

argparse rather than typer or click: the whole CLI is a dozen subcommands with a handful
of flags, argparse is in the standard library, and one fewer dependency in the trading
process is worth more than nicer help text. See docs/DECISIONS.md.

Commands that belong to a later phase print which phase implements them and exit 2, so
`liqfade --help` can list the real surface from day one without pretending.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from liqfade.config import ConfigError, flatten, load_settings, resolve
from liqfade.config.models import Settings

# Subcommands that later phases fill in. Value is the phase number in docs/ROADMAP.md.
NOT_YET: dict[str, int] = {
    "backtest": 4,
    "paper": 5,
    "testnet": 5,
    "live": 6,
}

EXIT_NOT_IMPLEMENTED = 2
EXIT_ERROR = 1


def _settings(args: argparse.Namespace) -> Settings:
    extra = [Path(args.config)] if getattr(args, "config", None) else None
    environ = dict(os.environ)
    # --data-dir is spelled as the env override so it goes through the same layering and
    # the same validation as everything else, rather than mutating the model afterwards.
    if getattr(args, "data_dir", None):
        environ["LIQFADE_DATA_DIR"] = str(args.data_dir)
    return load_settings(getattr(args, "env", None), extra_files=extra, environ=environ)


# --- config ------------------------------------------------------------------


def cmd_config(args: argparse.Namespace) -> int:
    if args.config_action != "show":  # pragma: no cover - argparse restricts the choices
        return EXIT_ERROR

    extra = [Path(args.config)] if args.config else None
    merged, sources = resolve(args.env, extra_files=extra)
    settings = load_settings(args.env, extra_files=extra)

    if args.json:
        import orjson

        payload = {
            "settings": settings.model_dump(mode="json"),
            "sources": sources,
            "live_trading_enabled": settings.live_trading_enabled,
            "live_trading_block_reason": settings.live_trading_block_reason(),
        }
        sys.stdout.write(orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode() + "\n")
        return 0

    flat = flatten(merged)
    width = max(len(k) for k in flat)
    print(f"env: {settings.env}")
    print(f"live trading: {'ENABLED' if settings.live_trading_enabled else 'blocked'}", end="")
    reason = settings.live_trading_block_reason()
    print(f" ({reason})" if reason else "")
    print()
    for key in sorted(flat):
        print(f"{key:<{width}}  {flat[key]!r:<30}  [{sources.get(key, 'default.yaml')}]")
    return 0


# --- record -------------------------------------------------------------------


def cmd_record(args: argparse.Namespace) -> int:
    """Run the recorder, or compact one past day and exit."""
    from datetime import UTC, datetime, timedelta

    from liqfade.log import setup_logging
    from liqfade.recorder.main import Recorder
    from liqfade.recorder.writer import compact_day
    from liqfade.runtime import run_main, shutdown_event

    settings = _settings(args)
    setup_logging(proc="recorder", env=settings.env, level=settings.logging.level)
    root = settings.paths.data_dir / "parquet"

    if args.compact_only:
        date = args.date or (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
        written = compact_day(root, date)
        for table, rows in sorted(written.items()):
            print(f"{table}: {rows} rows compacted for {date}")
        if not written:
            print(f"nothing to compact for {date} under {root}")
        return 0

    recorder = Recorder(settings, minutes=args.minutes)
    run_main(lambda: recorder.run(shutdown_event()))
    return 0


# --- compact ------------------------------------------------------------------

# fills_full is the pay-once archive: 130 GB that no research query reads, so it is not
# in the default set and gets its own opt-in run, last.
COMPACT_SKIP_BY_DEFAULT = frozenset({"fills_full"})


def cmd_compact(args: argparse.Namespace) -> int:
    """Fold a range of days into one Parquet file per table per day, oldest first."""
    import time

    from liqfade.data.ingest.common import BackfillError, day_range, parse_day
    from liqfade.data.schemas import SCHEMAS
    from liqfade.log import setup_logging
    from liqfade.recorder.writer import compact_day, lake_files

    settings = _settings(args)
    setup_logging(proc="compact", env=settings.env, level=settings.logging.level)
    root = settings.paths.data_dir / "parquet"
    if not root.is_dir():
        print(f"no lake at {root}", file=sys.stderr)
        return EXIT_ERROR

    present = sorted(p.name for p in root.iterdir() if p.is_dir())
    if args.tables:
        tables = [t.strip() for t in args.tables.split(",") if t.strip()]
        unknown = [t for t in tables if t not in present]
        if unknown:
            print(f"no such table in {root}: {', '.join(unknown)}", file=sys.stderr)
            return EXIT_ERROR
        # The Tardis side tables have no canonical schema and no ts_ms to sort on, and
        # the job already writes them one file per venue per day. Say so rather than
        # failing halfway through the range with a SchemaError.
        side = [t for t in tables if t not in SCHEMAS]
        if side:
            print(
                f"{', '.join(side)}: no canonical schema, and already one file per venue "
                "per day. Nothing to compact.",
                file=sys.stderr,
            )
            return EXIT_ERROR
    else:
        tables = [t for t in present if t in SCHEMAS and t not in COMPACT_SKIP_BY_DEFAULT]

    try:
        days = day_range(parse_day(args.start), parse_day(args.end), newest_first=False)
    except BackfillError as exc:
        print(f"compact error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    files_before = files_after = 0
    total_rows = 0
    for day in days:
        date = day.strftime("%Y-%m-%d")
        for table in tables:
            directory = root / table / f"date={date}"
            inputs = len(lake_files(directory)) if directory.is_dir() else 0
            files_before += inputs
            began = time.monotonic()
            written = compact_day(root, date, tables=[table])
            if not written:  # already one file, or no data for that day
                files_after += inputs
                continue
            files_after += 1
            rows = written[table]
            total_rows += rows
            print(
                f"{date}  {table:<26} {rows:>12,} rows  {inputs:>4} -> 1 files  "
                f"{time.monotonic() - began:7.1f}s",
                flush=True,
            )
    print(f"\ntotal: {total_rows:,} rows, {files_before} files -> {files_after} files")
    return 0


# --- backfill -----------------------------------------------------------------

BACKFILL_JOBS = ("s3-fills", "rederive", "l2", "candles", "tardis", "hydromancer-check")


def _dates(args: argparse.Namespace) -> tuple[Any, Any]:
    """--start and --end as dates. Both are required for every job but the check."""
    from liqfade.data.ingest.common import BackfillError, parse_day

    if not args.start or not args.end:
        raise BackfillError(f"{args.job} needs --start and --end (YYYY-MM-DD, inclusive, UTC)")
    return parse_day(args.start), parse_day(args.end)


def cmd_backfill(args: argparse.Namespace) -> int:
    """Historical ingestion. Every S3 job estimates before it spends. See docs/DATA_SOURCES.md."""
    import httpx
    from botocore.exceptions import (
        BotoCoreError,
        ClientError,
        NoCredentialsError,
        PartialCredentialsError,
    )

    from liqfade.data.ingest import hl_archive_l2, hl_candles, hl_s3, hydromancer, tardis_free
    from liqfade.data.ingest.common import BackfillError, s3_client
    from liqfade.log import setup_logging

    settings = _settings(args)
    setup_logging(proc="backfill", env=settings.env, level=settings.logging.level)
    data_dir = settings.paths.data_dir
    coins = [coin.strip().upper() for coin in args.coins.split(",") if coin.strip()]

    try:
        if args.job == "hydromancer-check":
            if not args.check_day:
                raise BackfillError("hydromancer-check needs --check-day YYYY-MM-DD")
            from liqfade.data.ingest.common import parse_day

            return hydromancer.run(data_dir, settings.paths.reports_dir, parse_day(args.check_day))

        start, end = _dates(args)

        if args.job == "s3-fills":
            return hl_s3.run(
                s3_client(),
                data_dir,
                start,
                end,
                estimate_only=args.estimate_only,
                yes=args.yes,
                keep_raw=args.keep_raw,
                workers=args.workers,
            )
        if args.job == "rederive":
            return hl_s3.rederive(data_dir, start, end)
        if args.job == "l2":
            # Checked before the S3 client is built, so a missing --coins is a one line
            # error rather than an AWS configuration error on top of it.
            hl_archive_l2.check_coins(coins)
            return hl_archive_l2.run(
                s3_client(),
                data_dir,
                start,
                end,
                coins,
                estimate_only=args.estimate_only,
                yes=args.yes,
                keep_raw=args.keep_raw,
                workers=args.workers,
            )
        if args.job == "candles":
            with httpx.Client() as client:
                return hl_candles.run(
                    client,
                    data_dir,
                    settings.venues.hyperliquid.api_url,
                    start,
                    end,
                    coins=coins or None,
                    estimate_only=args.estimate_only,
                )
        # tardis: the free days need a coin list, and the core coins are the sane default.
        with httpx.Client() as client:
            return tardis_free.run(
                client,
                data_dir,
                start,
                end,
                coins or list(settings.universe.core_coins),
                estimate_only=args.estimate_only,
            )
    except BackfillError as exc:
        print(f"backfill error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except (NoCredentialsError, PartialCredentialsError):
        print(
            "backfill error: no AWS credentials. Set AWS_ACCESS_KEY_ID and "
            "AWS_SECRET_ACCESS_KEY in .env (see .env.example) for an IAM user with "
            "s3:GetObject and s3:ListBucket on the Hyperliquid buckets, and set a "
            "billing alarm first. See docs/DATA_SOURCES.md.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    except BotoCoreError as exc:
        # ProfileNotFound, EndpointConnectionError and friends: report the message
        # rather than a twenty frame botocore traceback.
        print(f"backfill error: AWS client setup failed: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        hint = (
            " These buckets are requester-pays: a 403 usually means the IAM user lacks "
            "s3:ListBucket or s3:GetObject on the bucket and its /* resource."
            if code in ("AccessDenied", "403")
            else ""
        )
        print(f"backfill error: S3 refused the request ({code}).{hint}", file=sys.stderr)
        return EXIT_ERROR


# --- status -------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    """Ask the control socket, and fall back to data/state.json when it is not there."""
    from liqfade.control import ControlClient, ControlError, read_state

    settings = _settings(args)

    async def fetch() -> tuple[Any, str]:
        client = ControlClient(
            settings.socket_path,
            connect_timeout_s=settings.control.connect_timeout_s,
            request_timeout_s=settings.control.request_timeout_s,
        )
        return await client.status(), "control socket"

    try:
        state, origin = asyncio.run(fetch())
    except ControlError as exc:
        try:
            state, origin = read_state(settings.state_path), f"{settings.state_path} (stale)"
        except (FileNotFoundError, ValueError):
            print(f"no live process and no state file: {exc}", file=sys.stderr)
            return EXIT_ERROR

    if args.json:
        import orjson

        # One compact line, so `liqfade status --json | jq` works and so a caller can
        # pick the payload out of a log stream.
        sys.stdout.write(orjson.dumps(state.model_dump(mode="json")).decode() + "\n")
        return 0

    _print_state(state, origin, settings)
    return 0


def _print_state(state: Any, origin: str, settings: Settings) -> None:
    from liqfade.control.protocol import now_ms

    age_s = (now_ms() - state.ts_ms) / 1000.0
    print(f"source: {origin}  age: {age_s:.1f}s")
    print(f"proc: {state.proc}  env: {state.env}  uptime: {state.uptime_s:.0f}s")
    print(f"live trading: {'ENABLED' if state.live_trading else 'off'}")
    risk = state.risk
    halt = risk.halt_reason or "-"
    print(
        f"risk: paused={risk.paused} killed={risk.killed} halt={halt} "
        f"equity={risk.equity:.2f} daily_pnl={risk.daily_realized_pnl:.2f} "
        f"streak={risk.consecutive_losses}"
    )
    print(f"kill file: {'PRESENT' if settings.kill_file.exists() else 'absent'}")

    if state.feeds:
        print("\nfeeds:")
        for feed in state.feeds:
            mark = "ok" if feed.connected else "DOWN"
            print(f"  {feed.name:<28} {mark:<5} lag={feed.lag_ms:>7}ms  gaps_1h={feed.gaps_1h}")
    if state.positions:
        print("\npositions:")
        for pos in state.positions:
            print(
                f"  {pos.coin:<8} {pos.side:<5} size={pos.size:<12.4f} "
                f"notional={pos.notional_usd:>10.2f} upnl={pos.unrealized_pnl:>9.2f}"
            )
    if state.orders:
        print("\norders:")
        for order in state.orders:
            print(
                f"  {order.cloid[:12]:<14} {order.coin:<8} {order.side:<4} "
                f"{order.px:<12} {order.filled}/{order.size} {order.status}"
            )
    lat = state.latency
    if lat.samples:
        print(
            f"\nlatency (ms): ws_ping p50={lat.ws_ping_p50} p95={lat.ws_ping_p95}  "
            f"submit_ack p50={lat.submit_ack_p50} p95={lat.submit_ack_p95}"
        )
    if state.universe:
        print(f"\nuniverse ({len(state.universe)}): {' '.join(state.universe)}")


# --- sync ---------------------------------------------------------------------


def build_rsync_commands(
    settings: Settings, *, dry_run: bool, host: str | None = None
) -> list[list[str]]:
    """One rsync invocation per configured remote path. Pure, so it is testable offline."""
    target = host or settings.sync.host
    if not target:
        raise ConfigError("sync.host is not set (config sync.host or LIQFADE_SYNC_HOST)")

    commands: list[list[str]] = []
    for rel in settings.sync.paths:
        remote = f"{target}:{settings.sync.remote_root}/{rel}"
        local = Path(rel)
        # rsync needs the parent to exist; a trailing slash means the directory contents.
        local_target = str(local) + ("/" if rel.endswith("/") else "")
        cmd = ["rsync", *settings.sync.rsync_flags]
        if dry_run:
            cmd.append("--dry-run")
        cmd += [remote, local_target]
        commands.append(cmd)
    return commands


def cmd_sync(args: argparse.Namespace) -> int:
    settings = _settings(args)
    if shutil.which("rsync") is None:
        print("rsync is not installed", file=sys.stderr)
        return EXIT_ERROR

    try:
        commands = build_rsync_commands(settings, dry_run=args.dry_run, host=args.host)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    for cmd in commands:
        # The local parent must exist before rsync writes into it.
        Path(cmd[-1]).parent.mkdir(parents=True, exist_ok=True)
        print(" ".join(cmd))
        result = subprocess.run(cmd, check=False)  # noqa: S603 - argv built from typed config
        if result.returncode != 0:
            print(f"rsync failed with {result.returncode}", file=sys.stderr)
            return result.returncode
    return 0


# --- panel --------------------------------------------------------------------


def cmd_panel(args: argparse.Namespace) -> int:
    settings = _settings(args)

    if args.panel_action == "hash-password":
        from liqfade.panel.auth import hash_password_interactive

        print(hash_password_interactive())
        return 0

    from liqfade.panel.app import serve

    serve(settings, reload=args.reload)
    return 0


# --- placeholders --------------------------------------------------------------


def cmd_not_yet(args: argparse.Namespace) -> int:
    phase = NOT_YET[args.command]
    print(f"{args.command}: not implemented until Phase {phase}", file=sys.stderr)
    return EXIT_NOT_IMPLEMENTED


# --- parser -------------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    default = argparse.SUPPRESS if suppress else None
    parser.add_argument(
        "--env",
        choices=("paper", "testnet", "mainnet"),
        default=default,
        help="environment config to load (default: LIQFADE_ENV, then config/default.yaml)",
    )
    parser.add_argument(
        "--config",
        default=default,
        help="extra YAML layered between the environment file and overrides.yaml",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="liqfade",
        description="Hyperliquid liquidation fade bot. See CLAUDE.md and docs/ROADMAP.md.",
    )
    _add_common(parser, suppress=False)

    # The same two options are attached to every subparser with SUPPRESS defaults, so
    # `liqfade --env paper config show` and `liqfade config show --env paper` both work
    # and an absent subcommand flag does not clobber the top level value.
    common = argparse.ArgumentParser(add_help=False)
    _add_common(common, suppress=True)

    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    record = sub.add_parser("record", parents=[common], help="run the market data recorder")
    record.add_argument(
        "--minutes",
        type=float,
        default=None,
        help="stop after N minutes; used for fixture capture and smoke tests",
    )
    record.add_argument("--data-dir", default=None, help="overrides paths.data_dir")
    record.add_argument(
        "--compact-only",
        action="store_true",
        help="compact one day's hourly Parquet files into one file per table and exit",
    )
    record.add_argument(
        "--date", default=None, help="YYYY-MM-DD for --compact-only (default: yesterday)"
    )
    backfill = sub.add_parser(
        "backfill", parents=[common], help="pull historical data into the lake"
    )
    backfill.add_argument("job", choices=BACKFILL_JOBS, help="which source to pull")
    backfill.add_argument("--start", default=None, help="YYYY-MM-DD, inclusive, UTC")
    backfill.add_argument("--end", default=None, help="YYYY-MM-DD, inclusive, UTC")
    backfill.add_argument(
        "--coins",
        default="",
        help="comma separated HL coins. Required for l2, defaults to the core coins for "
        "tardis, and to every live perp for candles",
    )
    backfill.add_argument("--check-day", default=None, help="YYYY-MM-DD for hydromancer-check")
    backfill.add_argument("--estimate-only", action="store_true", help="print the cost and exit 0")
    backfill.add_argument(
        "--yes", action="store_true", help="proceed with a pull estimated over $5"
    )
    backfill.add_argument(
        "--keep-raw", action="store_true", help="keep downloads under data/raw after parsing"
    )
    backfill.add_argument(
        "--workers",
        type=int,
        default=6,
        help="parallel download workers for the S3 jobs; parsing is staged on the local "
        "disk and moved to the lake serially, so more workers cost RAM, not disk seeks",
    )
    backfill.add_argument("--data-dir", default=None, help="overrides paths.data_dir")
    sub.add_parser("backtest", parents=[common], help="replay the strategy (Phase 4)")

    compact = sub.add_parser(
        "compact", parents=[common], help="fold a range of days into one file per table"
    )
    compact.add_argument("--start", required=True, help="YYYY-MM-DD, inclusive, UTC")
    compact.add_argument("--end", required=True, help="YYYY-MM-DD, inclusive, UTC")
    compact.add_argument(
        "--tables",
        default="",
        help="comma separated table names; default is every table but fills_full",
    )
    compact.add_argument("--data-dir", default=None, help="overrides paths.data_dir")
    sub.add_parser("paper", parents=[common], help="live feeds, simulated fills (Phase 5)")
    sub.add_parser("testnet", parents=[common], help="live loop against testnet (Phase 5)")
    sub.add_parser("live", parents=[common], help="live loop against mainnet (Phase 6)")

    panel = sub.add_parser("panel", parents=[common], help="serve the web panel on 127.0.0.1")
    panel.add_argument(
        "panel_action",
        nargs="?",
        default="serve",
        choices=("serve", "hash-password"),
        help="serve the panel, or print a bcrypt hash for LIQFADE_PANEL_PASSWORD_HASH",
    )
    panel.add_argument("--reload", action="store_true", help="uvicorn autoreload, development only")

    status = sub.add_parser(
        "status", parents=[common], help="print positions, orders, risk, feeds and latency"
    )
    status.add_argument("--json", action="store_true", help="emit the raw state snapshot")

    sync = sub.add_parser(
        "sync", parents=[common], help="rsync the data lake and reports from the VPS"
    )
    sync.add_argument("--dry-run", action="store_true", help="show what rsync would transfer")
    sync.add_argument("--host", default=None, help="ssh target, overrides config sync.host")

    config = sub.add_parser("config", parents=[common], help="inspect the resolved configuration")
    config.add_argument("config_action", nargs="?", default="show", choices=("show",))
    config.add_argument("--json", action="store_true", help="emit settings and sources as JSON")

    return parser


HANDLERS = {
    "config": cmd_config,
    "backfill": cmd_backfill,
    "record": cmd_record,
    "compact": cmd_compact,
    "status": cmd_status,
    "sync": cmd_sync,
    "panel": cmd_panel,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command in NOT_YET:
        return cmd_not_yet(args)

    handler = HANDLERS[args.command]
    try:
        return handler(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    os.environ.setdefault("LIQFADE_ENV", "paper")
    raise SystemExit(main())
