"""Write one SVG per lab screen into docs/lab/.

Run it after changing a screen so the docs show what the tool actually looks like:

    uv run python scripts/lab_screenshots.py

It builds a throwaway sandbox with two configs and two finished runs, so the shots have
something in them, and never touches the real lake, reports or data directory.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

from liqfade.control.protocol import FeedState, RiskState, StateSnapshot, now_ms
from liqfade.control.state import write_state
from liqfade.lab.app import LabApp
from liqfade.lab.configs import save_config

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "lab"
SIZE = (120, 34)

SUMMARY = {
    "config_hash": "abcd1234ef",
    "env": "paper",
    "trigger": "exact",
    "latency_ms": 1000,
    "lagged_ms": 3000,
    "latency_fragile": False,
    "start_ms": 1760054400000,
    "end_ms": 1760313600000,
    "coins": ["BTC", "ETH", "SOL", "HYPE"],
    "starting_equity": 10000.0,
    "metrics": {"trades": 59, "net_pnl": -410.0, "hit_rate": 0.44, "max_drawdown_frac": 0.061},
    "holdout": {"net_pnl": -88.0},
    "funnel": {"intents": 540, "trades": 59},
    "rules": {
        "min_cascade_notional": {"evaluated": 540, "passed": 540, "killed": 0, "required": True},
        "oi_flush": {"evaluated": 540, "passed": 122, "killed": 0, "required": False},
        "vwap_stretch": {"evaluated": 540, "passed": 500, "killed": 0, "required": False},
        "level_reclaim": {"evaluated": 540, "passed": 210, "killed": 0, "required": False},
    },
}


def sandbox(root: Path) -> Path:
    configs = root / "config" / "backtest"
    save_config(
        configs / "v0.yaml",
        "v0 baseline",
        "the spec's rules, nothing added",
        {"backtest.latency_ms": 1000, "backtest.holdout_frac": 0.25},
    )
    save_config(
        configs / "v1_confluence.yaml",
        "v1 confluence",
        "a discretionary setup made objective",
        {
            "cascade.trigger": "exact",
            "exhaustion.mode": "rule",
            "strategy.min_confluence": 2,
            "intent.target_from": "entry",
        },
    )
    reports = root / "reports"
    for run_id, name, net in (
        ("20260906T101500Z_aaaa1111", "v0 baseline", -410.0),
        ("20260906T144500Z_bbbb2222", "v1 confluence", 318.4),
    ):
        directory = reports / run_id
        directory.mkdir(parents=True)
        payload = {**SUMMARY, "run_id": run_id}
        payload["metrics"] = {**SUMMARY["metrics"], "net_pnl": net}
        (directory / "summary.json").write_text(json.dumps(payload))
        (directory / "report.md").write_text(
            f"# Backtest {run_id}\n\n"
            "## Headline\n\n"
            "| metric | value |\n|---|---|\n"
            f"| net | {net:,.2f} USD |\n| trades | 59 |\n| hit rate | 44% |\n\n"
            "Trigger: exact, the lake's Hyperliquid liquidations from the S3 pull.\n"
        )
        (directory / "lab.json").write_text(
            json.dumps(
                {
                    "config_name": name,
                    "config_path": str(configs / "v0.yaml"),
                    "overrides": {"intent.k_stop": 1.0 if net < 0 else 1.5},
                    "started_at": 1000.0,
                    "finished_at": 1000.0 + (2410 if net < 0 else 2680),
                }
            )
        )
    write_state(
        root / "data" / "state.json",
        StateSnapshot(
            proc="recorder",
            env="paper",
            uptime_s=86_400.0,
            risk=RiskState(paused=False, equity=10_000.0, peak_equity=10_000.0),
            feeds=[
                FeedState(
                    name="hyperliquid.trades", connected=True, last_msg_ms=now_ms(), lag_ms=38
                ),
                FeedState(name="hyperliquid.bbo", connected=True, last_msg_ms=now_ms(), lag_ms=41),
                FeedState(
                    name="bybit.liquidations", connected=True, last_msg_ms=now_ms(), lag_ms=210
                ),
                FeedState(name="okx.liquidations", connected=False, last_msg_ms=0, lag_ms=95_000),
            ],
            universe=["BTC", "ETH", "SOL", "HYPE"],
        ),
    )
    return configs


async def shoot() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        sandbox(root)
        # Run from inside the sandbox with relative paths, so the command the Run screen
        # shows reads like the one the owner would type rather than a temp directory.
        here = Path.cwd()
        os.chdir(root)
        app = LabApp(
            config_dir=Path("config/backtest"),
            reports_dir=Path("reports"),
            data_dir=Path("data"),
        )
        async with app.run_test(size=SIZE) as pilot:
            for key, name in (("1", "configs"), ("2", "run"), ("3", "results"), ("4", "status")):
                await pilot.press(key)
                await pilot.pause()
                (OUT / f"{name}.svg").write_text(app.export_screenshot())
                print(f"docs/lab/{name}.svg")
            # One modal each, because the form and the report are most of the tool.
            await pilot.press("1")
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            (OUT / "configs-edit.svg").write_text(app.export_screenshot())
            print("docs/lab/configs-edit.svg")
            await pilot.press("escape")
            await pilot.press("3")
            await pilot.pause()
            await pilot.press("o")
            await pilot.pause()
            (OUT / "results-report.svg").write_text(app.export_screenshot())
            print("docs/lab/results-report.svg")
        os.chdir(here)


if __name__ == "__main__":
    asyncio.run(shoot())
