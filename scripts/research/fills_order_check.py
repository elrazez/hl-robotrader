"""Prove that `fills_full` still carries the exchange's event order.

The whole 02d repair rests on one assumption: the row order inside a `fills_full`
partition is the order Hyperliquid emitted the fills, and compaction preserved it. If
that is false, rederiving `trades` from it just replaces one wrong order with another.

The check does not need an external reference. Every fill carries `startPosition`, the
user's position in that coin *before* the fill, so consecutive fills of one
`(user_address, coin)` chain:

    start_position[n] == start_position[n-1] + signed_sz[n-1]      ("B" adds, "A" subtracts)

That identity holds only if the rows are walked in the exchange's order. Reorder two
fills of one sweep and both links break. Ordering is `ts_ms` first, then file order
(file index in `sorted(lake_files())`, then row number inside the file), which is exactly
the stable sort `rederive` uses.

Violations that share a `ts_ms` with their predecessor are the interesting ones: those
are the intra-millisecond ties this prompt is about. Violations across different
milliseconds are position changes that are not fills (funding is not one, but ADL,
settlement and transfers move a balance without emitting a fill for it), so a handful of
those is expected and does not say anything about ordering.

**Perps and spot are counted separately, and only perps are evidence.** On a perp the
`startPosition` is the position and nothing but a fill moves it, so the chain is exact.
On a spot market (`coin` is `@107` or `PURR/USDC`) it is the base token balance, which
also moves on deposits, withdrawals, transfers between the spot and perp wallets, and the
fee when the fee is taken in that token. Those events are not in this file at all, so
spot links break for reasons that have nothing to do with row order: measured 2026-01-10,
48% of spot links break and 0 of 2,379,722 perp links do.

    uv run python scripts/research/fills_order_check.py 2025-10-10 2026-03-01 2026-01-10
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Relative tolerance on the chained position. float64 carries ~1e-16 relative, and a
# real reordering moves the position by a whole fill size, so anything in between is
# safely accumulated rounding rather than a broken chain.
TOLERANCE = 1e-9

QUERY = """
WITH src AS (
    SELECT f.user_address, f.coin, f.ts_ms, f.side, f.sz, f.start_position,
           o.idx AS file_idx, f.file_row_number
    FROM read_parquet($files, filename = true, file_row_number = true) f
    JOIN file_order o ON o.path = f.filename
),
walked AS (
    SELECT ts_ms, start_position, coin,
           lag(ts_ms) OVER w AS prev_ts,
           lag(start_position) OVER w AS prev_start,
           lag(CASE WHEN side = 'B' THEN sz ELSE -sz END) OVER w AS prev_signed
    FROM src
    WINDOW w AS (PARTITION BY user_address, coin ORDER BY ts_ms, file_idx, file_row_number)
),
checked AS (
    SELECT ts_ms = prev_ts AS same_ms,
           coin LIKE '@%' OR coin LIKE '%/%' AS spot,
           abs(start_position - (prev_start + prev_signed)) AS err,
           greatest(1.0, abs(start_position), abs(prev_start)) * $tol AS tol
    FROM walked
    WHERE prev_start IS NOT NULL AND start_position IS NOT NULL AND prev_signed IS NOT NULL
)
SELECT spot,
       count(*) AS links,
       count(*) FILTER (WHERE err > tol) AS violations,
       count(*) FILTER (WHERE err > tol AND same_ms) AS violations_same_ms,
       coalesce(max(err) FILTER (WHERE err > tol), 0.0) AS worst_err
FROM checked
GROUP BY spot
"""

COLUMNS = ("links", "violations", "violations_same_ms", "worst_err")


def check_day(con, files: list[Path], tolerance: float = TOLERANCE) -> dict[str, dict[str, float]]:
    """Broken position links for one day's `fills_full` files, split into perp and spot."""
    con.execute("CREATE OR REPLACE TEMP TABLE file_order (path VARCHAR, idx INTEGER)")
    con.executemany(
        "INSERT INTO file_order VALUES (?, ?)",
        [(str(path), index) for index, path in enumerate(files)],
    )
    paths = [str(path) for path in files]
    empty = dict.fromkeys(COLUMNS, 0)
    found = {"perp": dict(empty), "spot": dict(empty)}
    for row in con.execute(QUERY, {"files": paths, "tol": tolerance}).fetchall():
        found["spot" if row[0] else "perp"] = dict(zip(COLUMNS, row[1:], strict=True))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("days", nargs="+", help="YYYY-MM-DD partitions to check")
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("LIQFADE_DATA_DIR", "data"),
        help="lake root holding parquet/ (default: $LIQFADE_DATA_DIR)",
    )
    parser.add_argument("--memory-limit", default="4GB", help="duckdb memory limit")
    parser.add_argument(
        "--temp-dir", default=None, help="duckdb spill directory; keep it off the lake drive"
    )
    args = parser.parse_args(argv)

    import duckdb

    from liqfade.recorder.writer import lake_files, partition_dir

    root = Path(args.data_dir) / "parquet"
    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{args.memory_limit}'")
    if args.temp_dir:
        con.execute(f"SET temp_directory = '{args.temp_dir}'")

    failed = False
    header = f"{'day':<12} {'market':<5} {'files':>6} {'links':>14} {'bad':>9} {'bad same ms':>12}"
    print(f"{header} {'worst':>12}")
    for day in args.days:
        files = sorted(lake_files(partition_dir(root, "fills_full", day)))
        if not files:
            print(f"{day:<12} no fills_full partition")
            failed = True
            continue
        began = time.monotonic()
        result = check_day(con, files)
        # Only perps carry the ordering claim; see the module docstring on spot balances.
        failed = failed or bool(result["perp"]["violations"])
        for market in ("perp", "spot"):
            counts = result[market]
            print(
                f"{day:<12} {market:<5} {len(files):>6} {counts['links']:>14,} "
                f"{counts['violations']:>9,} {counts['violations_same_ms']:>12,} "
                f"{counts['worst_err']:>12.6g}",
                flush=True,
            )
        print(f"{'':<12} {time.monotonic() - began:.1f}s", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
