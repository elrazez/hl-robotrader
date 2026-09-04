"""Gate: does `asset_ctx.funding` carry the hourly rate Hyperliquid actually applied?

The archive samples `funding` once a minute; `fundingHistory` records the rate charged at
each hour boundary, stamped with the moment it was applied, which carries tens of
milliseconds of jitter. So the two are the same series read at different instants, and
comparing the exact top-of-hour sample to the charged rate is not the question.

**The applied rate is the sample one minute before the hour**, `HH:59`, and that is
measured rather than chosen. At the top of the hour the column has already reset to the
next hour's rate, so `+0m` misses by up to 1.3e-4; every offset from `-1m` to `-5m` is
inside 2e-5 on 100% of hours for all three coins, and `-1m` is the tightest at 5.8e-7
(BTC), 3.2e-7 (ETH) and 1.2e-6 (HYPE). Further back is worse, which is the rate still
converging.

**The gate.** For every hourly funding timestamp in the window, take the `asset_ctx` row
at `OFFSET_MIN` and require `|diff| <= TOLERANCE` on **100%** of the hours that have one.
`TOLERANCE` is 2e-5 per hour, against a 4.5e-4 taker fee, so a full-tolerance error is
under 4% of one side of one round trip.

Hours with no row at all are reported separately and do not fail the gate: the archive's
own day files are truncated on some days (in the 2026-08-24 to 2026-08-30 week, four of
seven), which is a source gap rather than anything about the funding column.

Free: `fundingHistory` is a public Info call and the archive is already on disk.

    uv run python scripts/research/funding_check.py --start 2026-08-24 --end 2026-08-30
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

API_PATH = "/info"
HOUR_MS = 3_600_000
# Minutes from the funding hour to the sample that carries the applied rate. See the
# module docstring: -1 is measured, not chosen, and 0 is the one offset that fails.
OFFSET_MIN = -1
# Per hour, absolute. Against the 4.5e-4 taker fee in docs/STRATEGY_AND_RISK.md, a
# full-tolerance error is under 4% of one side of one round trip, which is below the
# resolution any funding-aware decision in this strategy has.
TOLERANCE = 2e-5
COINS = ("BTC", "ETH", "HYPE")


def days_between(start: date, end: date) -> list[str]:
    return [(start + timedelta(days=n)).isoformat() for n in range((end - start).days + 1)]


def bounds_ms(start: date, end: date) -> tuple[int, int]:
    first = datetime.combine(start, time.min, tzinfo=UTC)
    last = datetime.combine(end, time.max, tzinfo=UTC)
    return int(first.timestamp() * 1000), int(last.timestamp() * 1000)


def archive_files(root: Path, days: list[str]) -> list[str]:
    from liqfade.recorder.writer import lake_files, partition_dir

    found: list[str] = []
    for day in days:
        directory = partition_dir(root, "asset_ctx", day)
        if directory.is_dir():
            found += [str(path) for path in sorted(lake_files(directory))]
    return found


def funding_history(client, api_url: str, coin: str, start_ms: int, end_ms: int) -> list[dict]:
    """The rate applied at each hour, from the public Info API. No key, no cost."""
    payload = {"type": "fundingHistory", "coin": coin, "startTime": start_ms, "endTime": end_ms}
    response = client.post(f"{api_url}{API_PATH}", json=payload, timeout=30.0)
    response.raise_for_status()
    return list(response.json() or [])


# The sample at the fixed offset, per published funding hour. The join floors the API's
# stamp to its hour first: `fundingHistory` records the instant the rate was applied,
# which carries tens of milliseconds of jitter past the boundary.
OFFSET_QUERY = """
SELECT count(*) AS hours,
       count(*) FILTER (WHERE diff <= $tolerance) AS within,
       max(diff) AS worst
FROM (
    SELECT abs(a.funding - r.rate) AS diff
    FROM ref r JOIN read_parquet($files) a
      ON a.coin = $coin
     AND a.ts_ms = (r.applied_ms // $hour) * $hour + $shift
    WHERE a.funding IS NOT NULL
)
"""


def check(con, client, root: Path, api_url: str, start: date, end: date) -> list[dict]:
    """One row per coin: hours compared, hours inside the tolerance, and the worst miss."""
    files = archive_files(root, days_between(start, end))
    if not files:
        raise SystemExit(f"no asset_ctx partitions for {start} to {end} under {root}")
    start_ms, end_ms = bounds_ms(start, end)

    results = []
    for coin in COINS:
        theirs = funding_history(client, api_url, coin, start_ms, end_ms)
        con.execute("CREATE OR REPLACE TEMP TABLE ref (applied_ms BIGINT, rate DOUBLE)")
        if theirs:
            con.executemany(
                "INSERT INTO ref VALUES (?, ?)",
                [(int(row["time"]), float(row["fundingRate"])) for row in theirs],
            )
        hours, within, worst = con.execute(
            OFFSET_QUERY,
            {
                "files": files,
                "coin": coin,
                "hour": HOUR_MS,
                "shift": OFFSET_MIN * 60_000,
                "tolerance": TOLERANCE,
            },
        ).fetchone()
        results.append(
            {
                "coin": coin,
                "published": len(theirs),
                "hours": hours,
                "within": within,
                "worst": worst,
            }
        )
    return results


def render(start: date, end: date, rows: list[dict]) -> tuple[str, bool]:
    passed = all(row["hours"] and row["within"] == row["hours"] for row in rows)
    lines = [
        "# Funding: the archive against the rate Hyperliquid applied",
        "",
        f"Window: {start} to {end}. Reference: the Info API's `fundingHistory`.",
        "",
        f"**Gate: {'PASS' if passed else 'FAIL'}.** The `asset_ctx` sample at",
        f"{OFFSET_MIN:+d} minute from each funding hour must be within {TOLERANCE:g} per",
        "hour, on 100% of the hours that have a sample, for every coin.",
        "",
        "Hours with no sample are the archive's own truncated day files, not a property",
        "of the funding column; they are reported, not counted against the gate.",
        "",
        "| coin | hours published | hours with a sample | within tolerance | worst abs diff |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in rows:
        rate = "n/a" if not row["hours"] else f"{row['within'] / row['hours']:.2%}"
        worst = "n/a" if row["worst"] is None else f"{row['worst']:.3g}"
        lines.append(
            f"| {row['coin']} | {row['published']:,} | {row['hours']:,} | {rate} | {worst} |"
        )
    return "\n".join(lines) + "\n", passed


def main(argv: list[str] | None = None) -> int:
    import duckdb
    import httpx

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", required=True, help="YYYY-MM-DD, inclusive, UTC")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD, inclusive, UTC")
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("LIQFADE_DATA_DIR", "data"),
        help="lake root holding parquet/ (default: $LIQFADE_DATA_DIR)",
    )
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--api-url", default="https://api.hyperliquid.xyz")
    args = parser.parse_args(argv)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    con = duckdb.connect()
    with httpx.Client() as client:
        rows = check(con, client, Path(args.data_dir) / "parquet", args.api_url, start, end)

    report, passed = render(start, end, rows)
    out = Path(args.reports_dir) / "data_quality" / f"funding_check_{start}_{end}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report)
    print(report)
    print(f"wrote {out}")
    if not passed:
        print(
            "FAIL: the archive's funding is not the applied rate within tolerance", file=sys.stderr
        )
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
