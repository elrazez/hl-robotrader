"""A tiny synthetic lake with known cascades, for the research scripts' smoke tests.

Built rather than committed: a generator stays in step with `data/schemas.py`, is
diffable, and keeps a quarter megabyte of Parquet out of the repo. Every row goes through
`rows_to_table` and `write_rows`, so the layout, the column types and the partitioning are
exactly what the real lake has, and a schema change breaks these tests first.

What it is not: real data. It is four hours of a made up tape carrying about fifty made up
cascades. It proves the scripts run end to end and produce a parseable report. It proves
nothing about the science, and every report it produces should read `inconclusive`,
because four hours cannot clear a bar that asks for eight calendar months.

Shape: three coins across two tiers, HL-exact liquidations plus a Bybit mirror of most of
them (and a handful of Bybit-only cascades, so Q4 has something to find), a trade every
two seconds, `asset_ctx` once a minute with impact prices, and 1m candles.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from liqfade.data.schemas import SOURCE_HL_S3, SOURCE_TARDIS
from liqfade.recorder.writer import write_rows

DATE = "2026-03-01"
# Days of thin asset_ctx history written before the capture day. The universe rule needs a
# trailing snapshot to rank from and `universe.min_listing_days` is 14, so a lake that began
# on the capture day would have every coin excluded for being too young and the smoke tests
# would exercise the empty path instead of the pipeline.
HISTORY_DAYS = 20
START_MS = 1_772_323_200_000  # 2026-03-01T00:00:00Z
HOURS = 4
SPAN_MS = HOURS * 3_600_000
TRADE_STEP_MS = 2000
CTX_STEP_MS = 60_000

# Base price, tick size and typical trade size per coin. BTC and ETH are core, PEPE is the
# minor tier, so the three of them exercise the tier bucketing every report does.
COINS = {
    "BTC": (60_000.0, 1.0, 0.05),
    "ETH": (3_000.0, 0.1, 0.8),
    "PEPE": (0.00002, 1e-9, 5e8),
}
N_EPISODES = 50
SEED = 20260301


class Cascade:
    """One injected cascade: when, which coin, which way, how big, how far."""

    def __init__(self, rng: random.Random, coin: str, start_ms: int) -> None:
        self.coin = coin
        self.start_ms = start_ms
        self.side = rng.choice(("long", "short"))
        self.n_events = rng.randint(3, 8)
        self.notional = rng.uniform(3e5, 5e6)
        self.move_frac = rng.uniform(0.004, 0.012)  # 40 to 120 bps of displacement
        self.revert_frac = rng.uniform(0.2, 0.8)  # how much of it comes back
        self.duration_ms = rng.randint(20_000, 40_000)
        self.recovery_ms = rng.randint(300_000, 900_000)
        # A long liquidation is forced selling, so price falls into it.
        self.direction = -1.0 if self.side == "long" else 1.0

    def events(self, rng: random.Random) -> Iterator[tuple[int, float]]:
        """(ts_ms, notional) for each liquidation print, spread over the cascade."""
        share = self.notional / self.n_events
        for index in range(self.n_events):
            offset = round(self.duration_ms * index / max(self.n_events - 1, 1))
            yield self.start_ms + offset, share * rng.uniform(0.6, 1.4)

    def displacement(self, ts_ms: int) -> float:
        """Fraction added to the coin's baseline price at `ts_ms` by this cascade."""
        if ts_ms < self.start_ms:
            return 0.0
        age = ts_ms - self.start_ms
        if age <= self.duration_ms:
            fallen = self.move_frac * (age / self.duration_ms)
        else:
            recovered = min((age - self.duration_ms) / self.recovery_ms, 1.0)
            fallen = self.move_frac * (1.0 - self.revert_frac * recovered)
        return self.direction * fallen


def _cascades(rng: random.Random) -> list[Cascade]:
    coins = list(COINS)
    # Placed on a jittered grid so no two land on top of each other and the 60s join gap
    # never merges two of them into one episode by accident.
    spacing = SPAN_MS // (N_EPISODES + 2)
    return [
        Cascade(rng, coins[index % len(coins)], START_MS + spacing * (index + 1))
        for index in range(N_EPISODES)
    ]


def _price(coin: str, ts_ms: int, cascades: list[Cascade], rng: random.Random) -> float:
    base, tick, _ = COINS[coin]
    drift = 0.002 * math.sin((ts_ms - START_MS) / 900_000.0)
    shock = sum(c.displacement(ts_ms) for c in cascades if c.coin == coin)
    noise = rng.gauss(0.0, 0.0002)
    return max(round((base * (1 + drift + shock + noise)) / tick) * tick, tick)


def _trades(cascades: list[Cascade], rng: random.Random) -> list[dict[str, Any]]:
    rows = []
    for coin, (_, _, size) in COINS.items():
        for ts_ms in range(START_MS, START_MS + SPAN_MS, TRADE_STEP_MS):
            px = _price(coin, ts_ms, cascades, rng)
            # Aggressor leans with any cascade running in this coin, which is what makes
            # CVD and the forced-flow proxy see something.
            pressure = sum(
                c.direction
                for c in cascades
                if c.coin == coin and 0 <= ts_ms - c.start_ms <= c.duration_ms
            )
            sell_odds = 0.5 + 0.35 * max(min(pressure, 1.0), -1.0) * -1.0
            rows.append(
                {
                    "venue": "hyperliquid",
                    "coin": coin,
                    "ts_ms": ts_ms,
                    "rx_ms": 0,
                    "px": px,
                    "sz": size * rng.uniform(0.2, 6.0),
                    "side": "sell" if rng.random() < sell_odds else "buy",
                    "tid": ts_ms + hash(coin) % 1000,
                }
            )
    return rows


def _liquidations(
    cascades: list[Cascade], rng: random.Random
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """HL-exact rows, and the Bybit mirror with a few Bybit-only cascades."""
    hl: list[dict[str, Any]] = []
    bybit: list[dict[str, Any]] = []
    for index, cascade in enumerate(cascades):
        bybit_only = index % 9 == 0  # roughly one in nine fires on Bybit and not on HL
        mirrored = index % 3 != 0
        for ts_ms, notional in cascade.events(rng):
            px = _price(cascade.coin, ts_ms, cascades, rng)
            row = {
                "coin": cascade.coin,
                "venue_symbol": f"{cascade.coin}USDT",
                "ts_ms": ts_ms,
                "rx_ms": 0,
                "side": cascade.side,
                "px": px,
                "sz": notional / px,
                "notional_usd": notional,
                "method": "market",
                "complete": True,
            }
            if not bybit_only:
                hl.append({**row, "venue": "hyperliquid", "venue_symbol": cascade.coin})
            if mirrored or bybit_only:
                bybit.append({**row, "venue": "bybit", "ts_ms": ts_ms + rng.randint(-2000, 2000)})
    return hl, bybit


def _asset_ctx(cascades: list[Cascade], rng: random.Random) -> list[dict[str, Any]]:
    rows = []
    for coin in COINS:
        interest = rng.uniform(1e7, 5e7)
        for ts_ms in range(START_MS, START_MS + SPAN_MS, CTX_STEP_MS):
            mid = _price(coin, ts_ms, cascades, rng)
            # Open interest falls while a cascade runs, which is the proxy's second gate.
            running = any(
                c.coin == coin and 0 <= ts_ms - c.start_ms <= c.duration_ms + 60_000
                for c in cascades
            )
            interest *= 0.97 if running else rng.uniform(0.999, 1.002)
            spread = mid * rng.uniform(0.0002, 0.0008) * (2.0 if running else 1.0)
            rows.append(
                {
                    "coin": coin,
                    "ts_ms": ts_ms,
                    "rx_ms": 0,
                    "mark_px": mid * (1 + rng.gauss(0.0, 0.0003)),
                    "oracle_px": mid,
                    "mid_px": mid,
                    "funding": rng.gauss(0.0, 1.2e-5),
                    "open_interest": interest / mid,
                    "day_ntl_vlm": rng.uniform(5e7, 9e8),
                    "premium": rng.gauss(0.0, 0.0002),
                    "impact_bid_px": mid - spread / 2,
                    "impact_ask_px": mid + spread / 2,
                }
            )
    return rows


def _candles(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """1m OHLCV from the same trades, the way `liqfade derive candles` builds them."""
    buckets: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for trade in trades:
        key = (trade["coin"], trade["ts_ms"] // 60_000 * 60_000)
        buckets.setdefault(key, []).append(trade)
    rows = []
    for (coin, minute), prints in sorted(buckets.items()):
        prices = [p["px"] for p in prints]
        rows.append(
            {
                "coin": coin,
                "ts_ms": minute,
                "o": prices[0],
                "h": max(prices),
                "l": min(prices),
                "c": prices[-1],
                "v": sum(p["sz"] for p in prints),
                "n": len(prints),
            }
        )
    return rows


def _history(rng: random.Random) -> dict[str, list[dict[str, Any]]]:
    """One asset_ctx row an hour for the days before the capture, per UTC day.

    Enough for the universe rule to have a trailing snapshot and for the coins to be old
    enough to trade; far too thin to be mistaken for real context data.
    """
    days: dict[str, list[dict[str, Any]]] = {}
    for back in range(HISTORY_DAYS, 0, -1):
        day_start = START_MS - back * 86_400_000
        day = datetime.fromtimestamp(day_start / 1000, UTC).strftime("%Y-%m-%d")
        rows = []
        for coin, (base, _, _) in COINS.items():
            for hour in range(24):
                mid = base * (1 + rng.gauss(0.0, 0.002))
                rows.append(
                    {
                        "coin": coin,
                        "ts_ms": day_start + hour * 3_600_000,
                        "rx_ms": 0,
                        "mark_px": mid,
                        "oracle_px": mid,
                        "mid_px": mid,
                        "funding": rng.gauss(0.0, 1.2e-5),
                        "open_interest": rng.uniform(2e7, 6e7) / mid,
                        "day_ntl_vlm": rng.uniform(5e7, 9e8),
                        "premium": rng.gauss(0.0, 0.0002),
                        "impact_bid_px": mid * 0.9999,
                        "impact_ask_px": mid * 1.0001,
                    }
                )
        days[day] = rows
    return days


def build(root: Path) -> Path:
    """Write the whole fixture lake under `root` and return the parquet directory."""
    rng = random.Random(SEED)
    cascades = _cascades(rng)
    trades = _trades(cascades, rng)
    hl_liqs, bybit_liqs = _liquidations(cascades, rng)
    parquet = Path(root) / "parquet"
    written = {
        ("trades", "hyperliquid"): (trades, SOURCE_HL_S3),
        ("liquidations", "hyperliquid"): (hl_liqs, SOURCE_HL_S3),
        ("liquidations", "bybit"): (bybit_liqs, SOURCE_TARDIS),
        ("asset_ctx", "hyperliquid"): (_asset_ctx(cascades, rng), SOURCE_HL_S3),
        ("candles_1m", "hyperliquid"): (_candles(trades), SOURCE_HL_S3),
    }
    for day, rows in _history(rng).items():
        write_rows(parquet, ("asset_ctx", "hyperliquid", day, 0), rows, source=SOURCE_HL_S3)
    for (table, venue), (rows, source) in written.items():
        for hour in range(HOURS):
            window = range(START_MS + hour * 3_600_000, START_MS + (hour + 1) * 3_600_000)
            slice_ = [row for row in rows if row["ts_ms"] in window]
            write_rows(parquet, (table, venue, DATE, hour), slice_, source=source)
    return parquet
