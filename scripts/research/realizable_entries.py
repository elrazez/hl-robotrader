"""Realizable entries: replay the Phase 3 cascades with entries a live loop can take.

Q1 and Q8 priced the fade from an entry one second after the cascade extreme, which is a
price nobody knows until later. Every backtest so far (v0 and v1, 2026-09-06) lost money
with entries the engine can actually make. This script asks why, on the same 2,722
qualifying episodes, using the 1s research grid `q8_path.py` left under
`reports/research/grid/` (last trade price per second, dense from 360s before each episode
to 66 minutes after it).

Per episode it mimics the engine: the cascade starts when the 60s move in the cascade
direction reaches `thr` times rv_300s (the detector's `cascade.ret_over_rv_min`), the
anchor is the mid at that tick, the running extreme is tracked from there, and every entry
rule below is evaluated online, one second of latency, fill at the last trade. Exits are the
engine's: target from the extreme or from the entry, stop `k_stop * rv_300s` beyond the
extreme, a time stop, tie to the stop.

Entry rules: `fixed_N` (N seconds after the start), `quiet_N` (no new extreme for N
seconds), `reclaim_r` (mid back `r` of the way from the running extreme to the anchor),
`liqend_N` (N seconds after the episode's LAST liquidation, which a live rule cannot know is
the last: an upper bound on what the exhaustion rule's liquidation-decay condition can
reach) and `ideal` (Q8's entry, for calibration). Every trade row carries whether the entry
landed after the true extreme.

Costs: `net_stress` is the preregistration's (9 bps plus one effective spread per side);
`net_maker` is 6 bps plus one spread, which is a taker entry and a maker exit. Equity return
per trade uses the engine's sizing, notional/E = min(risk_frac / stop_frac, coin cap).

Writes `reports/research/realizable_entries.parquet` (one row per simulated trade) and
`reports/research/realizable_entries.md`. Reads no lake and runs in under a minute.

    uv run python scripts/research/realizable_entries.py
"""

from __future__ import annotations

import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))

import _lib as lib

ROOT = Path(__file__).resolve().parents[2]
RESEARCH = ROOT / "reports" / "research"
GRID = RESEARCH / "grid"
EPISODES = RESEARCH / "q1_episodes.parquet"

THRESHOLDS = (1.0, 2.0)
MIN_AGE_S = 20
MAX_AGE_S = 900
LATENCY_S = 1
MIN_EDGE_BPS = 4 * 6.0  # filters.min_edge_multiple x (taker + maker)
RISK_FRAC = 0.005
COIN_CAP = 0.25
FLOORS = {"core": 500_000.0, "major": 250_000.0, "minor": 100_000.0}
DELAYS_S = (30, 60, 120, 180, 300, 450, 600, 900)
QUIET_S = (15, 30, 60, 120, 180, 300)
RECLAIM = (0.2, 0.3, 0.5)
LIQEND_S = (5, 10, 20, 30, 45, 60)
K_TARGETS = (0.5, 0.8)
TARGET_FROM = ("extreme", "entry")
K_STOPS = (1.0, 1.5)
HORIZONS_S = (900, 1800, 3600)

SCHEMA = {
    "key": pl.String,
    "coin": pl.String,
    "tier": pl.String,
    "side": pl.String,
    "cluster": pl.String,
    "btc": pl.Boolean,
    "thr": pl.Float64,
    "rule": pl.String,
    "tf": pl.String,
    "kt": pl.Float64,
    "ks": pl.Float64,
    "horizon_s": pl.Int64,
    "outcome": pl.String,
    "entry_age_s": pl.Float64,
    "entry_ts": pl.Int64,
    "since_ext_s": pl.Float64,
    "after_ext": pl.Boolean,
    "hold_s": pl.Float64,
    "pnl_bps": pl.Float64,
    "net_stress": pl.Float64,
    "net_maker": pl.Float64,
    "stop_bps": pl.Float64,
    "eq_ret": pl.Float64,
}


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def _next_day(day: str) -> str:
    return (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def _load_grid(days: list[str]) -> pl.DataFrame | None:
    parts = [
        pl.read_parquet(GRID / f"{day}.parquet", columns=["coin", "ts_ms", "mid"])
        for day in days
        if (GRID / f"{day}.parquet").exists()
    ]
    if not parts:
        return None
    return pl.concat(parts).unique(subset=["coin", "ts_ms"]).sort("coin", "ts_ms")


def _first_true(mask: np.ndarray, start: int) -> int:
    """Index of the first True at or after `start`, or -1."""
    if start >= mask.size:
        return -1
    tail = mask[start:]
    if not tail.any():
        return -1
    return start + int(np.argmax(tail))


def _entries(
    ts: np.ndarray, mid: np.ndarray, s0: int, cascade_sign: float
) -> tuple[dict[str, int], np.ndarray]:
    """Every entry rule's tick for one detected cascade, and the running extreme."""
    n = ts.size
    seg = mid[s0:]
    ext = np.minimum.accumulate(seg) if cascade_sign < 0 else np.maximum.accumulate(seg)
    changed = np.empty(seg.size, dtype=bool)
    changed[0] = True
    changed[1:] = ext[1:] != ext[:-1]
    ext_ts = np.maximum.accumulate(np.where(changed, ts[s0:], 0))
    age = (ts[s0:] - ts[s0]) / 1000.0
    ok_age = (age >= MIN_AGE_S) & (age <= MAX_AGE_S)
    anchor = mid[s0]
    span = np.abs(anchor - ext)
    fade_sign = -cascade_sign

    out: dict[str, int] = {}
    for delay_s in DELAYS_S:
        if s0 + delay_s < n and MIN_AGE_S <= delay_s <= MAX_AGE_S:
            out[f"fixed_{delay_s}"] = s0 + delay_s
    for quiet_s in QUIET_S:
        j = _first_true(ok_age & ((ts[s0:] - ext_ts) >= quiet_s * 1000), 0)
        if j >= 0:
            out[f"quiet_{quiet_s}"] = s0 + j
    for r in RECLAIM:
        j = _first_true(ok_age & (fade_sign * (seg - ext) >= r * span) & (span > 0), 0)
        if j >= 0:
            out[f"reclaim_{r}"] = s0 + j
    return out, ext


def _simulate(episodes: pl.DataFrame) -> tuple[pl.DataFrame, dict[str, dict[str, int]]]:
    rows: list[dict[str, Any]] = []
    detected: dict[str, dict[str, int]] = {}
    by_day = episodes.with_columns(
        pl.col("start_ts").map_elements(_day, return_dtype=pl.String).alias("day")
    )
    for day in sorted(by_day["day"].unique().to_list()):
        grid = _load_grid([day, _next_day(day)])
        if grid is None:
            continue
        for e in by_day.filter(pl.col("day") == day).iter_rows(named=True):
            _simulate_episode(e, grid, rows, detected)
    return pl.DataFrame(rows, schema=SCHEMA), detected


def _simulate_episode(
    e: dict[str, Any],
    grid: pl.DataFrame,
    rows: list[dict[str, Any]],
    detected: dict[str, dict[str, int]],
) -> None:
    start_ts, end_ts, extreme_ts = int(e["start_ts"]), int(e["end_ts"]), int(e["extreme_ts"])
    g = grid.filter(
        (pl.col("coin") == e["coin"])
        & (pl.col("ts_ms") >= start_ts - 360_000)
        & (pl.col("ts_ms") <= end_ts + 66 * 60_000)
    )
    if g.height < 400:
        return
    ts = g["ts_ms"].to_numpy()
    mid = g["mid"].to_numpy().astype(float)
    n = ts.size
    cascade_sign = -1.0 if e["side"] == "long" else 1.0
    fade_sign = -cascade_sign
    with np.errstate(divide="ignore", invalid="ignore"):
        step = np.diff(np.log(mid), prepend=np.nan)
    cum = np.cumsum(np.where(np.isnan(step), 0.0, step) ** 2)
    rv = np.full(n, np.nan)
    rv[300:] = np.sqrt(cum[300:] - cum[:-300])
    ret60 = np.full(n, np.nan)
    ret60[60:] = np.log(mid[60:] / mid[:-60])
    raw_spread = e["spread_bps"]
    spread = (
        float(raw_spread) if isinstance(raw_spread, float) and not math.isnan(raw_spread) else 5.0
    )
    tier = str(e["tier"])
    i_start = int(np.searchsorted(ts, start_ts))
    i_end_plus = int(np.searchsorted(ts, end_ts + 60_000, side="right"))

    # Uniform accrual of the episode's notional: when the trailing 60s window first holds
    # the tier floor. The engine needs the floor and the move; the grid has no liquidation
    # stream, so the floor's timing is estimated and the move's is exact.
    dur_s = max((end_ts - start_ts) / 1000.0, 1.0)
    total = float(e["total_notional"])
    floor = FLOORS[tier]
    i_floor = -1
    if total * min(dur_s, 60.0) / dur_s >= floor:
        i_floor = int(np.searchsorted(ts, start_ts + int(1000 * floor * dur_s / total)))

    for thr in THRESHOLDS:
        counts = detected.setdefault(f"{thr}", {"episodes": 0, "detected": 0})
        counts["episodes"] += 1
        moved = (cascade_sign * ret60 >= thr * rv) & (rv > 0)
        i_move = _first_true(moved, i_start)
        if i_move < 0 or i_floor < 0:
            continue
        s0 = max(i_move, i_floor)
        if s0 >= i_end_plus:
            continue
        counts["detected"] += 1
        entries, ext = _entries(ts, mid, s0, cascade_sign)
        if thr == 2.0:
            i_ideal = int(np.searchsorted(ts, extreme_ts + 1000))
            if s0 < i_ideal < n:
                entries["ideal"] = i_ideal
        if thr == 1.0:
            for liqend_s in LIQEND_S:
                i = int(np.searchsorted(ts, end_ts + liqend_s * 1000))
                if s0 < i < n and (ts[i] - ts[s0]) / 1000.0 <= MAX_AGE_S:
                    entries[f"liqend_{liqend_s}"] = i
        anchor = mid[s0]
        for rule, i_e in entries.items():
            i_f = i_e + LATENCY_S
            if i_f >= n - 2 or not rv[i_e] > 0:
                continue
            px = mid[i_f]
            ext_e = ext[i_e - s0]
            path = mid[i_f + 1 :]
            base_row = {
                "key": e["key"],
                "coin": e["coin"],
                "tier": tier,
                "side": e["side"],
                "cluster": e["cluster"],
                "btc": bool(e["btc_cascading"]),
                "thr": thr,
                "rule": rule,
                "entry_age_s": float((ts[i_e] - ts[s0]) / 1000),
                "entry_ts": int(ts[i_f]),
                "since_ext_s": float((ts[i_e] - extreme_ts) / 1000),
                "after_ext": bool(ts[i_e] >= extreme_ts),
            }
            _exits(
                base_row, path, px, ext_e, anchor, rv[i_e], cascade_sign, fade_sign, spread, rows
            )


def _exits(
    base_row: dict[str, Any],
    path: np.ndarray,
    px: float,
    ext_e: float,
    anchor: float,
    rv_e: float,
    cascade_sign: float,
    fade_sign: float,
    spread: float,
    rows: list[dict[str, Any]],
) -> None:
    stops = {ks: ext_e + cascade_sign * ks * rv_e * px for ks in K_STOPS}
    stop_hit = {ks: _first_true(fade_sign * (path - sp) <= 0, 0) for ks, sp in stops.items()}
    targets = {
        (tf, kt): (ext_e if tf == "extreme" else px)
        + kt * (anchor - (ext_e if tf == "extreme" else px))
        for tf in TARGET_FROM
        for kt in K_TARGETS
    }
    target_hit = {k: _first_true(fade_sign * (path - tp) >= 0, 0) for k, tp in targets.items()}
    order = {"stop": 0, "target": 1, "time": 2}
    for ks, sp in stops.items():
        stop_bps = 1e4 * abs(px - sp) / px
        if stop_bps <= 0:
            continue
        frac = min(RISK_FRAC / (stop_bps / 1e4), COIN_CAP)
        for (tf, kt), tp in targets.items():
            edge_bps = 1e4 * fade_sign * (tp - px) / px
            for horizon_s in HORIZONS_S:
                row = dict(base_row, tf=tf, kt=kt, ks=ks, horizon_s=horizon_s, stop_bps=stop_bps)
                if edge_bps < MIN_EDGE_BPS:
                    row.update(
                        outcome="no_edge",
                        hold_s=0.0,
                        pnl_bps=0.0,
                        net_stress=0.0,
                        net_maker=0.0,
                        eq_ret=0.0,
                    )
                    rows.append(row)
                    continue
                candidates = [(min(horizon_s, path.size - 1), "time")]
                if stop_hit[ks] >= 0:
                    candidates.append((stop_hit[ks], "stop"))
                if target_hit[(tf, kt)] >= 0:
                    candidates.append((target_hit[(tf, kt)], "target"))
                j, outcome = min(candidates, key=lambda c: (c[0], order[c[1]]))
                exit_px = sp if outcome == "stop" else tp if outcome == "target" else path[j]
                pnl = 1e4 * fade_sign * (exit_px - px) / px
                net_stress = pnl - 9.0 - 2 * spread
                net_maker = pnl - 6.0 - spread
                row.update(
                    outcome=outcome,
                    hold_s=float(j + 1),
                    pnl_bps=pnl,
                    net_stress=net_stress,
                    net_maker=net_maker,
                    eq_ret=frac * net_maker / 1e4,
                )
                rows.append(row)


# --- report ---------------------------------------------------------------------


def _summary(frame: pl.DataFrame, by: list[str]) -> pl.DataFrame:
    return (
        frame.group_by(by)
        .agg(
            pl.len().alias("n"),
            pl.col("net_maker").mean().alias("nm"),
            pl.col("net_stress").mean().alias("ns"),
            (pl.col("net_maker") > 0).mean().alias("hit"),
            (pl.col("outcome") == "stop").mean().alias("stop"),
            pl.col("after_ext").mean().alias("after"),
            (pl.col("eq_ret").sum() * 100).alias("eq"),
            pl.col("since_ext_s").median().alias("since"),
        )
        .sort(by)
    )


def _rows(frame: pl.DataFrame, keys: list[str]) -> list[list[str]]:
    out = []
    for r in frame.iter_rows(named=True):
        out.append(
            [str(r[k]) for k in keys]
            + [
                str(r["n"]),
                f"{r['nm']:+.1f}",
                f"{r['ns']:+.1f}",
                f"{r['hit']:.0%}",
                f"{r['stop']:.0%}",
                f"{r['after']:.0%}",
                f"{r['since']:+.0f}",
                f"{r['eq']:+.1f}%",
            ]
        )
    return out


HEAD = [
    "n",
    "net maker bps",
    "net stress bps",
    "hit",
    "stop",
    "after extreme",
    "s since extreme",
    "equity/yr",
]


def _path(frame: pl.DataFrame) -> tuple[float, float, float, float, int, int]:
    """Equity return, worst drawdown, the same with one trade per cluster, months > 0."""
    d = frame.sort("entry_ts")
    c = np.cumsum(d["eq_ret"].to_numpy())
    dd = float((c - np.maximum.accumulate(np.concatenate([[0.0], c]))[1:]).min()) if c.size else 0.0
    one = d.unique(subset=["cluster"], keep="first").sort("entry_ts")
    c1 = np.cumsum(one["eq_ret"].to_numpy())
    dd1 = (
        float((c1 - np.maximum.accumulate(np.concatenate([[0.0], c1]))[1:]).min())
        if c1.size
        else 0.0
    )
    months = (
        d.with_columns(pl.from_epoch("entry_ts", time_unit="ms").dt.strftime("%Y-%m").alias("m"))
        .group_by("m")
        .agg(pl.col("eq_ret").sum().alias("r"))
    )
    total = float(c[-1]) if c.size else 0.0
    total1 = float(c1[-1]) if c1.size else 0.0
    return total, dd, total1, dd1, int((months["r"] > 0).sum()), months.height


def _report(trades: pl.DataFrame, detected: dict[str, dict[str, int]], path: Path) -> None:
    tr = trades.filter(pl.col("outcome") != "no_edge")
    base = tr.filter(
        (pl.col("thr") == 1.0)
        & (pl.col("tf") == "entry")
        & (pl.col("kt") == 0.8)
        & (pl.col("ks") == 1.0)
        & (pl.col("horizon_s") == 900)
    )
    blocks: list[str] = []
    det_rows = [
        [
            thr,
            str(c["episodes"]),
            str(c["detected"]),
            f"{c['detected'] / max(c['episodes'], 1):.0%}",
        ]
        for thr, c in sorted(detected.items())
    ]
    blocks += [
        "Episodes: the 2,722 qualifying cascades of `q1_episodes.parquet`. Every table below "
        "prices a taker entry at the last trade one second after the decision and a maker "
        "exit (`net maker`, 6 bps plus one effective spread), and beside it the "
        "preregistration's stress cost (9 bps plus two spreads). `equity/yr` sums each "
        "trade's return at 0.5% risk per trade with the 25% coin cap, over the year. "
        "`after extreme` is the share of entries that landed after the cascade's true low "
        "or high, which nothing live can know; `s since extreme` is the median entry time "
        "relative to it.",
        "",
        "## Does the engine start a cascade at all",
        "",
        "The start condition is `abs(ret_60s) / rv_300s >= cascade.ret_over_rv_min`, and "
        "`rv_300s` holds the cascade's own moves, so a fast flush scores about 1 and only a "
        "slow slide clears 2.",
        "",
        lib.table(["ret_over_rv_min", "episodes", "start a cascade", "share"], det_rows),
        "",
        "## Entry rules, threshold 1.0, target from entry 0.8, stop 1.0 rv, 15 min",
        "",
        lib.table(["rule", *HEAD], _rows(_summary(base, ["rule"]), ["rule"])),
        "",
        "## Before and after the true extreme",
        "",
        "The realizable rules lose on the entries that land before the extreme and roughly "
        "break even on the rest; the rule cannot tell the two apart from price alone.",
        "",
        lib.table(
            ["rule", "after extreme", *HEAD],
            _rows(
                _summary(
                    base.filter(
                        pl.col("rule").is_in(
                            [
                                "fixed_60",
                                "fixed_120",
                                "fixed_300",
                                "quiet_15",
                                "quiet_30",
                                "quiet_120",
                                "reclaim_0.3",
                            ]
                        )
                    ),
                    ["rule", "after_ext"],
                ),
                ["rule", "after_ext"],
            ),
        ),
        "",
        "## Entries N seconds after the last liquidation, the upper bound for the exhaustion rule",
        "",
        "Hindsight for the last print, realizable for everything else. The exhaustion rule's "
        "`liq_decay_frac` and `no_new_extreme_s` are the live approximation.",
        "",
        lib.table(
            ["rule", *HEAD],
            _rows(
                _summary(base.filter(pl.col("rule").str.starts_with("liqend")), ["rule"]), ["rule"]
            ),
        ),
        "",
        lib.table(
            ["rule", "tier", *HEAD],
            _rows(
                _summary(
                    base.filter(pl.col("rule").is_in(["liqend_10", "liqend_20"])), ["rule", "tier"]
                ),
                ["rule", "tier"],
            ),
        ),
        "",
        lib.table(
            ["rule", "btc cascading", *HEAD],
            _rows(
                _summary(
                    base.filter(pl.col("rule").is_in(["liqend_10", "liqend_20"])), ["rule", "btc"]
                ),
                ["rule", "btc"],
            ),
        ),
        "",
        "## Exits for the liqend_10 entry",
        "",
        lib.table(
            ["target from", "k_target", "k_stop", "time stop s", *HEAD],
            _rows(
                _summary(
                    tr.filter((pl.col("thr") == 1.0) & (pl.col("rule") == "liqend_10")),
                    ["tf", "kt", "ks", "horizon_s"],
                ),
                ["tf", "kt", "ks", "horizon_s"],
            ),
        ),
        "",
        "## Equity paths",
        "",
    ]
    finalists = {
        "liqend_10, all regimes (v2a's target)": base.filter(pl.col("rule") == "liqend_10"),
        "liqend_10, isolated only (v2b's target)": base.filter(
            (pl.col("rule") == "liqend_10") & ~pl.col("btc")
        ),
        "liqend_20, all regimes (v2c's target)": base.filter(pl.col("rule") == "liqend_20"),
        "liqend_10, k_target 0.5": tr.filter(
            (pl.col("thr") == 1.0)
            & (pl.col("rule") == "liqend_10")
            & (pl.col("tf") == "entry")
            & (pl.col("kt") == 0.5)
            & (pl.col("ks") == 1.0)
            & (pl.col("horizon_s") == 900)
        ),
        "fixed_120 at threshold 2.0 (the v0 shape)": tr.filter(
            (pl.col("thr") == 2.0)
            & (pl.col("rule") == "fixed_120")
            & (pl.col("tf") == "entry")
            & (pl.col("kt") == 0.8)
            & (pl.col("ks") == 1.0)
            & (pl.col("horizon_s") == 900)
        ),
        "fixed_120 at threshold 1.0": base.filter(pl.col("rule") == "fixed_120"),
    }
    path_rows = []
    for label, frame in finalists.items():
        total, dd, total1, dd1, pos, months = _path(frame)
        path_rows.append(
            [
                label,
                str(frame.height),
                f"{100 * total:+.1f}%",
                f"{100 * dd:.1f}%",
                f"{100 * total1:+.1f}%",
                f"{100 * dd1:.1f}%",
                f"{pos}/{months}",
            ]
        )
    blocks += [
        lib.table(
            [
                "cell",
                "trades",
                "equity/yr",
                "worst drawdown",
                "one per cluster",
                "its drawdown",
                "months > 0",
            ],
            path_rows,
        ),
        "",
        "## What this is not",
        "",
        "- Not a backtest: no book, fills at the last trade, stops fill at their price with no "
        "slippage, the cluster cap and cooldowns are not applied except in the one-per-cluster "
        "column, and the detector's notional floor is timed by uniform accrual because the "
        "grid carries no liquidation stream.",
        "- `liqend_N` knows which liquidation was the last. The exhaustion rule does not, and "
        "how often it fires on a mid-cascade lull is the number the v2 backtests measure: a "
        "clean after-extreme entry runs a 10 to 15% stop-out share here.",
    ]
    lib.write_report(
        path,
        "Realizable entries: where the Phase 3 edge goes when the entry is live",
        blocks,
        costs=False,
    )


def main() -> None:
    episodes = pl.read_parquet(EPISODES)
    trades, detected = _simulate(episodes)
    trades.write_parquet(RESEARCH / "realizable_entries.parquet")
    _report(trades, detected, RESEARCH / "realizable_entries.md")
    print(f"{trades.height} simulated trades over {episodes.height} episodes")
    print(f"wrote {RESEARCH / 'realizable_entries.md'}")


if __name__ == "__main__":
    main()
