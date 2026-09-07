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
    "testnet": 5,
    "live": 6,
}

# Paper's opening balance, USD, when --equity is not given. The same kind of number as
# DEFAULT_BACKTEST_EQUITY below and not capital: every sizing fraction is applied to it,
# so the run reports percentages that scale to any account.
DEFAULT_PAPER_EQUITY = 10_000.0

# Default starting equity for a backtest, USD. The one number in the package that looks
# like capital, and it is not: it is the denominator every sizing fraction is applied to,
# so a run reports percentages that scale to any account. CLAUDE.md forbids hardcoding
# capital in the trading path; --equity overrides this for a run that wants to match a
# real balance.
DEFAULT_BACKTEST_EQUITY = 10_000.0

# Profile rows printed by --profile. Twenty is what fits on a screen beside the report.
PROFILE_ROWS = 20

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

# Above this many days, `compact` and `backfill rederive` want --yes. Both rewrite lake
# files and unlink the originals across whatever range they are handed, so a mistyped
# --data-dir or --start rewrites a year of a 227 GiB pull unattended. CLAUDE.md rule 8:
# ask before deleting recorded data. A few days is the size of an ordinary catch-up run.
CONFIRM_ABOVE_DAYS = 7


def needs_confirmation(days: list[Any], *, yes: bool, dry_run: bool) -> str | None:
    """The message to print instead of running, or None to go ahead."""
    if dry_run or yes or len(days) <= CONFIRM_ABOVE_DAYS:
        return None
    return (
        f"{len(days)} days ({days[0]} to {days[-1]}) is over the {CONFIRM_ABOVE_DAYS} day "
        "confirmation threshold, and this rewrites lake files and deletes the originals. "
        "Re-run with --dry-run to see what it would touch, or --yes to do it."
    )


def cmd_compact(args: argparse.Namespace) -> int:
    """Fold a range of days into one Parquet file per table per day, oldest first."""
    import time

    from liqfade.data.ingest.common import BackfillError, day_range, parse_day
    from liqfade.data.schemas import SCHEMAS
    from liqfade.log import setup_logging
    from liqfade.recorder.writer import DAY_SUFFIX, compact_day, lake_files

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

    refuse = needs_confirmation(days, yes=args.yes, dry_run=args.dry_run)
    if refuse:
        print(f"compact: {refuse}", file=sys.stderr)
        return EXIT_ERROR

    files_before = files_after = 0
    total_rows = 0
    for day in days:
        date = day.strftime("%Y-%m-%d")
        for table in tables:
            directory = root / table / f"date={date}"
            found = sorted(lake_files(directory)) if directory.is_dir() else []
            inputs = len(found)
            files_before += inputs
            if args.dry_run:
                # Bytes from `stat`, not row counts from the footers: a dry run over an
                # uncompacted year would otherwise pay for 50,000 file opens to answer a
                # question that "how much am I about to rewrite" already answers.
                if inputs > 1 or (inputs == 1 and not found[0].name.endswith(DAY_SUFFIX)):
                    mib = sum(path.stat().st_size for path in found) / 1024**2
                    files_after += 1
                    print(f"{date}  {table:<26} {inputs:>4} files, {mib:>9,.1f} MiB -> 1")
                else:
                    files_after += inputs
                continue
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
    if args.dry_run:
        print(f"\nwould fold {files_before} files into {files_after}. Nothing was changed.")
        return 0
    print(f"\ntotal: {total_rows:,} rows, {files_before} files -> {files_after} files")
    return 0


# --- backfill -----------------------------------------------------------------

BACKFILL_JOBS = ("s3-fills", "rederive", "l2", "asset-ctxs", "candles", "tardis")


def _dates(args: argparse.Namespace) -> tuple[Any, Any]:
    """--start and --end as dates. Both are required for every job."""
    from liqfade.data.ingest.common import BackfillError, parse_day

    if not args.start or not args.end:
        raise BackfillError(f"{args.job} needs --start and --end (YYYY-MM-DD, inclusive, UTC)")
    return parse_day(args.start), parse_day(args.end)


# Flags on the shared `backfill` parser that only one job acts on. Passing one to any
# other job used to do nothing at all, silently.
JOB_ONLY_FLAGS = {"force": "rederive", "dry_run": "rederive"}


def _reject_flags_the_job_ignores(args: argparse.Namespace) -> None:
    from liqfade.data.ingest.common import BackfillError

    for flag, owner in JOB_ONLY_FLAGS.items():
        if getattr(args, flag, False) and args.job != owner:
            spelled = "--" + flag.replace("_", "-")
            raise BackfillError(f"{spelled} does nothing for {args.job}; only {owner} acts on it")


def _workers(args: argparse.Namespace, *, default: int) -> int:
    """--workers, with `0` refused rather than silently becoming the default.

    `args.workers or default` read an explicit `--workers 0` as "not given", which is the
    opposite of what somebody typing it means.
    """
    from liqfade.data.ingest.common import BackfillError

    if args.workers is None:
        return default
    if args.workers < 1:
        raise BackfillError(f"--workers {args.workers} is not a worker count; pass 1 or more")
    return int(args.workers)


def cmd_backfill(args: argparse.Namespace) -> int:
    """Historical ingestion. Every S3 job estimates before it spends. See docs/DATA_SOURCES.md."""
    import httpx
    from botocore.exceptions import (
        BotoCoreError,
        ClientError,
        NoCredentialsError,
        PartialCredentialsError,
    )

    from liqfade.data.ingest import hl_archive_l2, hl_asset_ctxs, hl_candles, hl_s3, tardis_free
    from liqfade.data.ingest.common import BackfillError, s3_client
    from liqfade.log import setup_logging

    settings = _settings(args)
    setup_logging(proc="backfill", env=settings.env, level=settings.logging.level)
    data_dir = settings.paths.data_dir
    coins = [coin.strip().upper() for coin in args.coins.split(",") if coin.strip()]

    try:
        _reject_flags_the_job_ignores(args)
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
                workers=_workers(args, default=6),
            )
        if args.job == "rederive":
            from liqfade.data.ingest.common import day_range

            days = [day.isoformat() for day in day_range(start, end, newest_first=True)]
            if args.dry_run:
                return hl_s3.rederive_plan(data_dir, days)
            # It rewrites both derived partitions of every day and deletes the originals,
            # so the same confirmation `compact` asks for applies. CLAUDE.md rule 8.
            refuse = needs_confirmation(days, yes=args.yes, dry_run=False)
            if refuse:
                raise BackfillError(refuse)
            # Two workers, not six: a rederive worker holds a whole day of fills_full,
            # up to ~13M rows on a cascade day, where an S3 worker holds one hour.
            return hl_s3.rederive(
                data_dir, start, end, force=args.force, workers=_workers(args, default=2)
            )
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
                workers=_workers(args, default=6),
            )
        if args.job == "asset-ctxs":
            return hl_asset_ctxs.run(
                s3_client(),
                data_dir,
                start,
                end,
                estimate_only=args.estimate_only,
                yes=args.yes,
                keep_raw=args.keep_raw,
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


# --- derive -------------------------------------------------------------------

DERIVE_JOBS = ("candles",)


def cmd_derive(args: argparse.Namespace) -> int:
    """Rebuild one canonical table from another. Free, offline, no network at all."""
    from liqfade.data.derive import candles
    from liqfade.data.ingest.common import BackfillError
    from liqfade.log import setup_logging

    settings = _settings(args)
    setup_logging(proc="derive", env=settings.env, level=settings.logging.level)
    coins = [coin.strip().upper() for coin in args.coins.split(",") if coin.strip()]

    try:
        start, end = _dates(args)
        if args.dry_run:
            return candles.plan(settings.paths.data_dir, start, end, force=args.force)
        # It writes a day file into every candles_1m partition in the range and deletes
        # the hl_api bars it finds beside it, so the same confirmation `compact` and
        # `backfill rederive` ask for applies. CLAUDE.md rule 8.
        from liqfade.data.ingest.common import day_range

        refuse = needs_confirmation(day_range(start, end), yes=args.yes, dry_run=False)
        if refuse:
            raise BackfillError(refuse)
        return candles.run(
            settings.paths.data_dir, start, end, coins=coins or None, force=args.force
        )
    except BackfillError as exc:
        print(f"derive error: {exc}", file=sys.stderr)
        return EXIT_ERROR


# --- backtest -----------------------------------------------------------------


def cmd_backtest(args: argparse.Namespace) -> int:
    """Replay the strategy over the lake and write a report.

    Two runs of the same command produce byte-identical `trades.parquet`, which is what
    `--seed` exists to keep true if anything stochastic is ever added. `--lagged-ms` reruns
    the same window at a worse latency, and stamps the report "latency fragile" when the
    edge does not survive it, per the roadmap's Phase 4 definition of done.
    """
    from datetime import UTC, datetime

    from liqfade.backtest.progress import PROGRESS_FILE, ProgressWriter
    from liqfade.data.ingest.common import BackfillError, parse_day
    from liqfade.log import setup_logging

    settings = _settings(args)
    setup_logging(proc="backtest", env=settings.env, level=settings.logging.level)

    try:
        if not args.start or not args.end:
            raise BackfillError("backtest needs --start and --end (YYYY-MM-DD, inclusive, UTC)")
        start_day, end_day = parse_day(args.start), parse_day(args.end)
    except BackfillError as exc:
        print(f"backtest error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    start_ms = int(
        datetime(start_day.year, start_day.month, start_day.day, tzinfo=UTC).timestamp() * 1000
    )
    # --end is inclusive of the whole day, matching every other date range in this CLI.
    end_ms = (
        int(datetime(end_day.year, end_day.month, end_day.day, tzinfo=UTC).timestamp() * 1000)
        + 86_400_000
    )
    if end_ms <= start_ms:
        print("backtest error: --end is before --start", file=sys.stderr)
        return EXIT_ERROR

    coins = [coin.strip().upper() for coin in args.coins.split(",") if coin.strip()]
    if not coins:
        coins = list(settings.universe.core_coins)

    reports_dir = Path(args.out) if args.out else settings.paths.reports_dir
    progress_path = (
        None
        if args.progress_file == "-"
        else Path(args.progress_file)
        if args.progress_file
        else reports_dir / PROGRESS_FILE
    )
    progress = ProgressWriter(progress_path)
    progress.set(
        stage="loading",
        coins=coins,
        window_start_ms=start_ms,
        window_end_ms=end_ms,
        starting_equity=args.equity,
        equity=args.equity,
        lagged_ms=args.lagged_ms,
    )
    try:
        return _run_backtest_command(args, settings, coins, start_ms, end_ms, reports_dir, progress)
    except Exception as exc:
        progress.set(stage="failed", error=f"{type(exc).__name__}: {exc}")
        raise


def _run_backtest_command(
    args: argparse.Namespace,
    settings: Settings,
    coins: list[str],
    start_ms: int,
    end_ms: int,
    reports_dir: Path,
    progress: object,
) -> int:
    import cProfile
    import pstats

    from liqfade.backtest.data import day_files
    from liqfade.backtest.engine import run_backtest
    from liqfade.backtest.progress import ProgressWriter
    from liqfade.backtest.report import run_id_for, write_report
    from liqfade.backtest.source import LakeSource
    from liqfade.backtest.validate import bump_n_trials

    assert isinstance(progress, ProgressWriter)
    source = LakeSource(
        settings.paths.data_dir,
        settings=settings,
        coins=coins,
        start_ms=start_ms,
        end_ms=end_ms,
        starting_equity=args.equity,
        seed=args.seed,
        cache=not args.no_cache,
    )
    # Checked off the partition directories rather than by reading the tape: the whole
    # point of the streaming engine is that a year of trades is never loaded to find out
    # whether it is there.
    if not any(day_files(settings.paths.data_dir, "trades", day.date) for day in source.days()):
        print(
            f"backtest error: no trades in the lake for {','.join(coins)} between "
            f"{args.start} and {args.end}. Check --coins and the date range, or run "
            f"`liqfade backfill` first.",
            file=sys.stderr,
        )
        progress.set(stage="failed", error="no trades in the lake for that window")
        return EXIT_ERROR

    profiler = cProfile.Profile() if args.profile else None
    if profiler is not None:
        profiler.enable()
    result = run_backtest(source, settings, progress)
    if profiler is not None:
        profiler.disable()

    # Every run counts towards the deflated Sharpe, including this one, so the tally is
    # incremented before the report reads it.
    trials = bump_n_trials(reports_dir)

    net = sum(trade.net_pnl for trade in result.trades)
    lagged_ms: int | None = None
    latency_fragile: bool | None = None
    if args.lagged_ms is not None:
        lagged_ms = args.lagged_ms
        degraded = _with_latency(settings, args.lagged_ms)
        progress.set(phase="lagged", stage="replaying", trades=0, net=0.0, intents=0)
        lagged = run_backtest(source, degraded, progress)
        lagged_net = sum(trade.net_pnl for trade in lagged.trades)
        # Fragile means an edge that the extra latency took away. A run with no edge to
        # begin with is not fragile, it is empty, and stamping it would put a scary
        # warning on a report whose real problem is that it found nothing.
        latency_fragile = net > 0 and lagged_net <= 0
        print(
            f"lagged rerun at {args.lagged_ms} ms: {len(lagged.trades)} trades, "
            f"net {lagged_net:,.2f} USD"
        )
        if net <= 0:
            print(
                "not marked latency fragile: the baseline run had no positive expectancy "
                "to lose, so the question does not arise"
            )

    run_id = run_id_for(settings)
    progress.set(stage="report")
    directory = write_report(
        result,
        settings,
        reports_dir=reports_dir,
        run_id=run_id,
        n_trials=trials,
        lagged_ms=lagged_ms,
        latency_fragile=latency_fragile,
    )

    progress.set(stage="done", report_dir=str(directory), trades=len(result.trades), net=net)
    print(f"{directory}")
    print(
        f"trigger={settings.cascade.trigger} coins={len(result.coins)} "
        f"intents={result.intents} trades={len(result.trades)} net={net:,.2f} USD"
    )
    if latency_fragile:
        print("LATENCY FRAGILE: the lagged rerun did not keep a positive expectancy")

    if profiler is not None:
        stats = pstats.Stats(profiler)
        stats.sort_stats("cumulative").print_stats(PROFILE_ROWS)
    return 0


def _with_latency(settings: Settings, latency_ms: int) -> Settings:
    """The same config with a worse latency, for the fragility rerun.

    Rebuilt through the model rather than mutated, because `Settings` is frozen and because
    a degraded config that skipped validation would not be a config the loader would accept.
    """
    payload = settings.model_dump()
    payload["backtest"]["latency_ms"] = latency_ms
    return Settings.model_validate(payload)


# --- lab ----------------------------------------------------------------------


def cmd_lab(args: argparse.Namespace) -> int:
    """Open the terminal workbench.

    Textual is imported here rather than at module scope so that a box which installed the
    core only still gets every other subcommand. The lab reads the same config directory
    the loader does and the reports directory the settings name, so it shows what
    `liqfade backtest` would run against.
    """
    from liqfade.config.loader import config_dir

    settings = _settings(args)
    try:
        from liqfade.lab.app import LabApp
    except ImportError as exc:
        print(
            f"lab: {exc}. The terminal workbench needs the optional `lab` extra: "
            "run `make setup`, or `uv sync --extra lab`.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    LabApp(
        config_dir=Path(args.config_dir) if args.config_dir else config_dir() / "backtest",
        reports_dir=settings.paths.reports_dir,
        data_dir=settings.paths.data_dir,
    ).run()
    return 0


# --- watch --------------------------------------------------------------------


def cmd_watch(args: argparse.Namespace) -> int:
    """Draw the progress file until the run says done or failed. Ctrl-C leaves it running."""
    import shutil
    import time

    from liqfade.backtest.progress import PROGRESS_FILE, read_progress
    from liqfade.backtest.watch import render

    settings = _settings(args)
    path = Path(args.file) if args.file else settings.paths.reports_dir / PROGRESS_FILE
    while True:
        state = read_progress(path)
        width = shutil.get_terminal_size((100, 20)).columns
        frame = render(state, time.time(), width=width, path=path)
        if args.once:
            print(frame)
            return 0
        sys.stdout.write("\x1b[2J\x1b[H" + frame + "\n")
        sys.stdout.flush()
        if state is not None and state.stage in ("done", "failed"):
            return 0 if state.stage == "done" else EXIT_ERROR
        time.sleep(0.5)


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
            worst = f"  worst={feed.worst_coin} {feed.worst_lag_ms}ms" if feed.worst_coin else ""
            # The cadence is what the l2 gap threshold is measured against, so it belongs
            # next to gaps_1h: a number that has moved is why the gap count has.
            cadence = f"  cadence={feed.cadence_ms}ms" if feed.cadence_ms else ""
            # How far behind the slowest coin is delivering, which `lag_ms` cannot show:
            # the stream is never quiet while seventeen other coins are on time.
            slow = (
                f"  slowest={feed.delivery_coin} {feed.delivery_lag_ms}ms"
                if feed.delivery_coin
                else ""
            )
            print(
                f"  {feed.name:<28} {mark:<5} lag={feed.lag_ms:>7}ms  "
                f"msgs_1m={feed.msgs_1m:<7} gaps_1h={feed.gaps_1h}  "
                f"parse_errors_1h={feed.parse_errors_1h}{cadence}{worst}{slow}"
            )
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
            f"rest p50={lat.rest_p50} p95={lat.rest_p95}  "
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

    serve(settings)
    return 0


def cmd_paper(args: argparse.Namespace) -> int:
    """Run the paper loop: live feeds, simulated fills, real alerts and a real kill switch."""
    from liqfade.live.loop import PaperLoop
    from liqfade.log import setup_logging
    from liqfade.runtime import run_main, shutdown_event

    settings = _settings(args)
    setup_logging(proc="paper", env=settings.env, level=settings.logging.level)
    if settings.env == "mainnet":
        # Paper simulates every fill. Running it under the mainnet config would read the
        # mainnet risk limits and report a paper equity curve as if it were the account.
        print("paper mode refuses --env mainnet: use paper or testnet", file=sys.stderr)
        return EXIT_ERROR

    from liqfade.live.features import LiveFeatureEngine

    unsupported = LiveFeatureEngine.unsupported(settings)
    if unsupported:
        # Raised here rather than deep inside setup, so the message names the fix. The
        # usual one is cascade.trigger: `exact` reads S3 liquidations a live bot cannot
        # see, and the unit sets LIQFADE_CASCADE__TRIGGER=crossvenue for that reason.
        print("paper mode cannot run this config:", file=sys.stderr)
        for reason in unsupported:
            print(f"  - {reason}", file=sys.stderr)
        print("Set LIQFADE_CASCADE__TRIGGER=crossvenue, or disable the rule.", file=sys.stderr)
        return EXIT_ERROR

    loop = PaperLoop(settings, starting_equity=args.equity, minutes=args.minutes)
    run_main(lambda: loop.run(shutdown_event()))
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
    backfill.add_argument("--estimate-only", action="store_true", help="print the cost and exit 0")
    backfill.add_argument(
        "--dry-run",
        action="store_true",
        help="rederive: print what would be read, rewritten and deleted per day",
    )
    backfill.add_argument(
        "--yes",
        action="store_true",
        help=f"over $5 of egress, or a rederive of more than {CONFIRM_ABOVE_DAYS} days",
    )
    backfill.add_argument(
        "--keep-raw", action="store_true", help="keep downloads under data/raw after parsing"
    )
    backfill.add_argument(
        "--workers",
        type=int,
        default=None,
        help="parallel workers; default 6 for the S3 pulls, where parsing is staged on "
        "the local disk and moved to the lake serially, and 2 for rederive, where one "
        "worker holds a whole day of fills",
    )
    backfill.add_argument(
        "--force",
        action="store_true",
        help="rederive: redo days whose derived tables are already current",
    )
    backfill.add_argument("--data-dir", default=None, help="overrides paths.data_dir")
    derive = sub.add_parser(
        "derive", parents=[common], help="rebuild a table from another one, offline and free"
    )
    derive.add_argument("job", choices=DERIVE_JOBS, help="which table to derive")
    derive.add_argument("--start", default=None, help="YYYY-MM-DD, inclusive, UTC")
    derive.add_argument("--end", default=None, help="YYYY-MM-DD, inclusive, UTC")
    derive.add_argument(
        "--coins", default="", help="comma separated HL coins; default is every coin in trades"
    )
    derive.add_argument(
        "--force", action="store_true", help="rewrite days whose output file already exists"
    )
    derive.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be read, written and deleted per day",
    )
    derive.add_argument(
        "--yes", action="store_true", help=f"required over {CONFIRM_ABOVE_DAYS} days"
    )
    derive.add_argument("--data-dir", default=None, help="overrides paths.data_dir")
    backtest = sub.add_parser(
        "backtest", parents=[common], help="replay the strategy over the lake"
    )
    backtest.add_argument("--start", default=None, help="YYYY-MM-DD, inclusive, UTC")
    backtest.add_argument("--end", default=None, help="YYYY-MM-DD, inclusive, UTC")
    backtest.add_argument(
        "--coins", default="", help="comma separated HL coins; default is the core universe"
    )
    backtest.add_argument(
        "--seed", type=int, default=0, help="fixed seed; the same seed replays identically"
    )
    backtest.add_argument(
        "--equity",
        type=float,
        default=DEFAULT_BACKTEST_EQUITY,
        help="starting equity in USD; every sizing fraction is applied to it",
    )
    backtest.add_argument("--out", default=None, help="reports directory (default: reports/)")
    backtest.add_argument(
        "--lagged-ms",
        type=int,
        default=None,
        help="also rerun at this latency; the report is stamped latency fragile if the "
        "edge does not survive it",
    )
    backtest.add_argument(
        "--profile", action="store_true", help=f"print the top {PROFILE_ROWS} cumulative frames"
    )
    backtest.add_argument(
        "--no-cache",
        action="store_true",
        help="rebuild every day's feature grid instead of reading the cache under "
        "<data-dir>/cache/features/",
    )
    backtest.add_argument("--data-dir", default=None, help="overrides paths.data_dir")
    backtest.add_argument(
        "--progress-file",
        default=None,
        help="where `liqfade watch` reads progress (default: <reports>/.progress.json; "
        "'-' disables it)",
    )

    lab = sub.add_parser(
        "lab", parents=[common], help="the terminal workbench: configs, runs, results"
    )
    lab.add_argument(
        "--config-dir", default=None, help="strategy configs (default: config/backtest)"
    )
    lab.add_argument("--data-dir", default=None, help="overrides paths.data_dir")

    watch = sub.add_parser(
        "watch", parents=[common], help="live progress of a running backtest, in this terminal"
    )
    watch.add_argument(
        "--file", default=None, help="the progress file (default: <reports>/.progress.json)"
    )
    watch.add_argument("--once", action="store_true", help="print one frame and exit")

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
    compact.add_argument(
        "--dry-run", action="store_true", help="print what would be folded and change nothing"
    )
    compact.add_argument(
        "--yes",
        action="store_true",
        help=f"required above {CONFIRM_ABOVE_DAYS} days: this deletes the files it folds",
    )
    compact.add_argument("--data-dir", default=None, help="overrides paths.data_dir")
    paper = sub.add_parser(
        "paper", parents=[common], help="live feeds, simulated fills, Telegram and kill switch"
    )
    paper.add_argument(
        "--equity",
        type=float,
        default=DEFAULT_PAPER_EQUITY,
        help=f"paper starting equity in USD (default {DEFAULT_PAPER_EQUITY:,.0f})",
    )
    paper.add_argument(
        "--minutes", type=float, default=None, help="stop after N minutes, for smoke tests"
    )
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
    "backtest": cmd_backtest,
    "config": cmd_config,
    "backfill": cmd_backfill,
    "derive": cmd_derive,
    "record": cmd_record,
    "compact": cmd_compact,
    "status": cmd_status,
    "watch": cmd_watch,
    "sync": cmd_sync,
    "panel": cmd_panel,
    "lab": cmd_lab,
    "paper": cmd_paper,
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
