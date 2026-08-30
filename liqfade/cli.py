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
    "record": 1,
    "backfill": 2,
    "backtest": 4,
    "paper": 5,
    "testnet": 5,
    "live": 6,
}

EXIT_NOT_IMPLEMENTED = 2
EXIT_ERROR = 1


def _settings(args: argparse.Namespace) -> Settings:
    extra = [Path(args.config)] if getattr(args, "config", None) else None
    return load_settings(getattr(args, "env", None), extra_files=extra)


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

    sub.add_parser("record", parents=[common], help="run the market data recorder (Phase 1)")
    sub.add_parser("backfill", parents=[common], help="pull historical data (Phase 2)")
    sub.add_parser("backtest", parents=[common], help="replay the strategy (Phase 4)")
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
