"""Time the backtest at the size the spec sets a target for: one month, ten coins.

`docs/STRATEGY_AND_RISK.md` section 6: *one month of a 10-coin universe in under 5 minutes*.
This builds a synthetic lake of that shape and times a real run over it, so the target can
be checked on a laptop without a copy of the real year. The owner runs the same check
against the real lake; this one proves the engine's shape rather than the machine's.

    uv run python scripts/backtest_speed_check.py --days 30 --coins 10
    uv run python scripts/backtest_speed_check.py --days 3 --coins 10 --extrapolate

`--extrapolate` runs a shorter window and scales the result, which is what to use while
iterating: a full thirty days takes as long as the thing being measured.

The synthetic tape is deliberately busy. One print a second per coin for the whole window
is roughly what a liquid perp does, and it is the row count rather than the price path that
the engine's speed depends on. The cascades are real ones that the strategy trades, so the
timing includes the fill simulation and the order lifecycle rather than only the feature
pass: a lake that produced no intents would time the cheap half of the engine.
"""

from __future__ import annotations

import argparse
import random
import time
from datetime import UTC, datetime

import polars as pl

from liqfade.backtest.engine import RunInputs, run_backtest
from liqfade.config import load_settings

START_MS = 1_772_323_200_000  # 2026-03-01T00:00:00Z
TRADE_STEP_MS = 1000
CASCADES_PER_DAY = 3
CASCADE_LEN_MS = 30_000
CASCADE_DROP = 0.02  # 200 bps, well over the episode floor
RECOVERY_MS = 600_000
BACKGROUND_STEP_MS = 70_000  # over the 60s join gap, so each is its own episode
BACKGROUND_NOTIONAL = 10_000.0
TARGET_MINUTES = 5.0
SEED = 20260906


def cascade_starts(days: int) -> list[int]:
    """When each injected cascade begins, spaced far enough apart never to merge."""
    return [
        START_MS + day * 86_400_000 + (n + 1) * 6_000_000
        for day in range(days)
        for n in range(CASCADES_PER_DAY)
    ]


def displacement(ts_ms: int, starts: list[int]) -> float:
    """Fractional price offset from every cascade running at `ts_ms`.

    Falls linearly over the cascade and recovers over ten minutes, which is the shape the
    strategy is built to fade, so the run produces trades rather than only features.
    """
    total = 0.0
    for start in starts:
        age = ts_ms - start
        if age < 0:
            continue
        if age <= CASCADE_LEN_MS:
            total -= CASCADE_DROP * age / CASCADE_LEN_MS
        else:
            total -= CASCADE_DROP * (1.0 - min((age - CASCADE_LEN_MS) / RECOVERY_MS, 1.0))
    return total


def build_lake(days: int, coins: int) -> tuple[pl.DataFrame, pl.DataFrame, int, int]:
    """A tape of one print a second per coin, with a few tradeable cascades a day."""
    rng = random.Random(SEED)
    end_ms = START_MS + days * 86_400_000
    names = [f"C{index:02d}" for index in range(coins)]
    starts = cascade_starts(days)

    trades: list[dict[str, object]] = []
    liquidations: list[dict[str, object]] = []
    for coin_index, coin in enumerate(names):
        base = 100.0 * (1 + coin_index)
        for ts_ms in range(START_MS, end_ms, TRADE_STEP_MS):
            px = base * (1.0 + displacement(ts_ms, starts) + rng.gauss(0.0, 0.00005))
            trades.append(
                {
                    "coin": coin,
                    "ts_ms": ts_ms,
                    "rx_ms": 0,
                    "px": px,
                    "sz": 1.0,
                    "side": "buy" if rng.random() < 0.5 else "sell",
                    "tid": ts_ms + coin_index,
                }
            )
        # Background liquidations, without which the z-score has no distribution to score
        # against and is null everywhere. See tests/test_strategy_cascade_consistency.py.
        for ts_ms in range(START_MS, end_ms, BACKGROUND_STEP_MS):
            if any(-90_000 <= ts_ms - start <= CASCADE_LEN_MS + 90_000 for start in starts):
                continue
            liquidations.append(
                _liq(coin, ts_ms, base, BACKGROUND_NOTIONAL * rng.uniform(0.9, 1.1))
            )
        for start in starts:
            for step in range(6):
                liquidations.append(_liq(coin, start + step * 5_000, base, 1_000_000.0))
    return pl.DataFrame(trades), pl.DataFrame(liquidations), START_MS, end_ms


def _liq(coin: str, ts_ms: int, px: float, notional: float) -> dict[str, object]:
    return {
        "venue": "hyperliquid",
        "coin": coin,
        "ts_ms": ts_ms,
        "rx_ms": 0,
        "side": "long",
        "px": px,
        "sz": notional / px,
        "notional_usd": notional,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="days of tape to build")
    parser.add_argument("--coins", type=int, default=10, help="coins in the universe")
    parser.add_argument(
        "--extrapolate",
        action="store_true",
        help="scale the measured time to 30 days rather than running them",
    )
    args = parser.parse_args()

    settings = load_settings("paper")
    build_started = time.perf_counter()
    trades, liquidations, start_ms, end_ms = build_lake(args.days, args.coins)
    build_s = time.perf_counter() - build_started

    print(
        f"lake: {args.days} days x {args.coins} coins, "
        f"{trades.height:,} trades, {liquidations.height:,} liquidations, "
        f"built in {build_s:.1f}s"
    )
    print(f"window: {_stamp(start_ms)} to {_stamp(end_ms)} UTC")

    started = time.perf_counter()
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
    elapsed = time.perf_counter() - started

    print(
        f"run: {elapsed:.1f}s for {args.days} days, "
        f"{result.intents} intents, {len(result.trades)} trades"
    )
    projected = elapsed * (30.0 / args.days) * (10.0 / args.coins)
    label = "projected" if args.extrapolate or args.days != 30 or args.coins != 10 else "measured"
    verdict = "PASS" if projected <= TARGET_MINUTES * 60 else "OVER TARGET"
    print(
        f"{label} for 30 days x 10 coins: {projected / 60:.1f} min "
        f"against a {TARGET_MINUTES:.0f} min target -> {verdict}"
    )
    return 0 if projected <= TARGET_MINUTES * 60 else 1


def _stamp(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M")


if __name__ == "__main__":
    raise SystemExit(main())
