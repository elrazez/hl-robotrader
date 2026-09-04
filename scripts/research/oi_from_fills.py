"""Open interest per coin per minute, computed exactly from `fills_full`.

Every fill carries `start_position`, the user's position in that coin before the fill, so
the fill moves that user from `start_position` to `start_position + signed_sz`.

**Hyperliquid's `openInterest` counts both sides of the book**, longs plus shorts, which
is twice the long exposure. Measured, not assumed: the archive's minute-to-minute change
divided by the long-only sum is 2.000000 at the 5th and 95th percentiles on BTC, ETH, SOL
and HYPE, and `|2 x ours - theirs|` is float noise. So an account contributes `abs(pos)`
and one fill changes open interest by

    abs(start_position + signed_sz) - abs(start_position)

and the change over a minute is the sum of that across every fill in the minute. Exact,
block by block: both halves of a trade are separate fills with their own `user_address`
and `start_position`, so summing over all of them is summing over all accounts.

Two uses. It proves the `asset_ctxs` archive's minute series, which is a snapshot the
exchange publishes and which nothing else in the lake can check. And it stays as the
sub-minute source if a Phase 3 gate ever needs open interest between two archive samples.

**Perps only.** On a spot market (`coin` is `@107` or `PURR/USDC`) `start_position` is a
token balance that deposits, withdrawals and fees move without a fill, and there is no
open interest to speak of. Same split as `scripts/research/fills_order_check.py`.

    uv run python scripts/research/oi_from_fills.py 2025-10-10 --coins BTC,ETH,SOL,HYPE
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

MINUTE_MS = 60_000

# The whole computation. The `CASE` is the fill's effect on the user's position, and the
# two `abs` terms are that user's contribution to open interest after and before it: a
# long of 5 and a short of 5 each count 5, because Hyperliquid counts both sides.
DELTA_QUERY = """
SELECT coin,
       (ts_ms // $minute) * $minute AS minute_ms,
       sum(abs(start_position + CASE WHEN side = 'B' THEN sz ELSE -sz END)
           - abs(start_position)) AS oi_delta,
       count(*) AS fills
FROM read_parquet($files)
WHERE start_position IS NOT NULL AND sz IS NOT NULL AND ts_ms IS NOT NULL
  AND NOT (coin LIKE '@%' OR coin LIKE '%/%')
  AND ($coins IS NULL OR list_contains($coins, coin))
GROUP BY 1, 2
ORDER BY 1, 2
"""

# The archive's own series, differenced so that `minute_ms` means the same thing on both
# sides: the change that happened *during* that minute. The archive stamps a sample with
# the instant it was taken, so the difference between two samples belongs to the earlier
# one's minute, which is what `lag(ts_ms)` labels it with. Getting this wrong reads as a
# real disagreement: measured on 2026-08-28, aligning them correctly takes the
# correlation from 0.08-0.32 to exactly 1.0000 on BTC, ETH, SOL and HYPE.
ARCHIVE_QUERY = """
SELECT coin,
       lag(ts_ms) OVER w AS minute_ms,
       open_interest - lag(open_interest) OVER w AS oi_delta
FROM read_parquet($files)
WHERE source = 'hl_archive'
  AND ($coins IS NULL OR list_contains($coins, coin))
WINDOW w AS (PARTITION BY coin ORDER BY ts_ms)
QUALIFY oi_delta IS NOT NULL
ORDER BY 1, 2
"""

COMPARE_QUERY = """
SELECT f.coin,
       count(*) AS minutes,
       corr(f.oi_delta, a.oi_delta) AS correlation,
       max(abs(f.oi_delta - a.oi_delta)) AS worst_abs,
       median(abs(f.oi_delta - a.oi_delta)) AS median_abs,
       max(abs(a.oi_delta)) AS biggest_move
FROM fills f JOIN archive a USING (coin, minute_ms)
GROUP BY 1
ORDER BY 1
"""


def files_for(root: Path, table: str, day: str) -> list[str]:
    from liqfade.recorder.writer import lake_files, partition_dir

    return [str(path) for path in sorted(lake_files(partition_dir(root, table, day)))]


def oi_deltas(con, files: list[str], coins: list[str] | None):
    """Open-interest change per coin per minute, from `fills_full`."""
    return con.execute(DELTA_QUERY, {"files": files, "minute": MINUTE_MS, "coins": coins})


def render(day: str, rows: list[tuple]) -> str:
    lines = [
        "# Open interest from fills, against the asset-context archive",
        "",
        f"Day: {day}. Ours is the exact change per minute computed from every fill's",
        "`start_position`; the reference is the archive's own minute series, differenced.",
        "",
        "`worst` and `median` are in coin units, and `biggest move` is the largest",
        "minute-to-minute change the archive itself saw, which is what makes `worst`",
        "readable as large or small.",
        "",
        "| coin | minutes | correlation | median abs diff | worst abs diff | biggest move |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for coin, minutes, correlation, worst, median, biggest in rows:
        shown = "n/a" if correlation is None else f"{correlation:.6f}"
        # `.6g` rather than a fixed 4 decimals: when the two sides agree the residual is
        # float noise, and rounding it to 0.0000 hides whether that is what happened.
        lines.append(
            f"| {coin} | {minutes:,} | {shown} | {median:.6g} | {worst:.6g} | {biggest:,.4f} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("day", help="YYYY-MM-DD partition to compare")
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("LIQFADE_DATA_DIR", "data"),
        help="lake root holding parquet/ (default: $LIQFADE_DATA_DIR)",
    )
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--coins", default="", help="comma separated; default is every perp")
    parser.add_argument("--memory-limit", default="4GB", help="duckdb memory limit")
    parser.add_argument(
        "--temp-dir", default=None, help="duckdb spill directory; keep it off the lake drive"
    )
    args = parser.parse_args(argv)

    import duckdb

    root = Path(args.data_dir) / "parquet"
    coins = [coin.strip().upper() for coin in args.coins.split(",") if coin.strip()] or None
    fills = files_for(root, "fills_full", args.day)
    archive = files_for(root, "asset_ctx", args.day)
    if not fills:
        print(f"no fills_full for {args.day} under {root}", file=sys.stderr)
        return 1
    if not archive:
        print(
            f"no asset_ctx for {args.day}: run `liqfade backfill asset-ctxs` first",
            file=sys.stderr,
        )
        return 1

    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{args.memory_limit}'")
    if args.temp_dir:
        con.execute(f"SET temp_directory = '{args.temp_dir}'")

    con.execute(
        f"CREATE TEMP TABLE fills AS {DELTA_QUERY}",
        {"files": fills, "minute": MINUTE_MS, "coins": coins},
    )
    con.execute(f"CREATE TEMP TABLE archive AS {ARCHIVE_QUERY}", {"files": archive, "coins": coins})
    rows = con.execute(COMPARE_QUERY).fetchall()
    if not rows:
        print(f"no minute of {args.day} is in both sets", file=sys.stderr)
        return 1

    report = render(args.day, rows)
    out = Path(args.reports_dir) / "data_quality" / f"oi_from_fills_{args.day}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report)
    print(report)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
