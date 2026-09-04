"""Hydromancer Reservoir: a verification job, not a default source.

Hydromancer's S3 bucket carries Hyperliquid perps from August 2025 with fills,
liquidations and ADLs already separated out, egress only, and the vendor says free
forever. If their liquidation table is right it is a far smaller download than parsing
every fill, and it happens to cover exactly the twelve month window we want. That is
worth checking before spending $33 with AWS, and worth not trusting until it is checked.

`liqfade backfill hydromancer-check --check-day YYYY-MM-DD` pulls one day from both
sources, compares liquidation count and total notional per coin, and writes
`reports/data_quality/hydromancer_check.md`. Hydromancer only becomes the source of the
fills job if the owner later passes `--use-hydromancer`, which does not exist yet and
should not until this check has passed.

**The bucket layout is not implemented.** It is not published anywhere that can be read
without a Hydromancer account, and guessing a requester-pays bucket name and prefix is
how you get either a 403 or a bill for the wrong objects. `fetch_day` therefore prints
the four things the owner has to look up and returns nothing. Everything downstream of
the fetch, the comparison and the report, is real and tested, so wiring in the layout is
a small change to one function.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

from liqfade.log import get_logger

log = get_logger(__name__)

JOB = "hydromancer-check"
REPORT_PATH = Path("reports/data_quality/hydromancer_check.md")

# What the owner has to find out before this job can fetch anything.
LOOKUP_CHECKLIST = (
    "the S3 bucket name (Hydromancer call it Reservoir)",
    "the AWS region, and whether the bucket is requester-pays",
    "the key layout for Hyperliquid perp liquidations: prefix, one file per day or hour, "
    "and the compression",
    "the column names in their liquidation records, in particular which field names the "
    "liquidated side and whether the size is in coin units",
)

# Relative differences under this are noise: two sources rounding a size differently.
TOLERANCE = 0.01


def fetch_day(day: date) -> list[dict[str, Any]] | None:
    """Not implemented: the bucket layout is not public. Prints what to look up."""
    print(
        f"hydromancer-check cannot fetch {day}: the Reservoir bucket layout is not "
        "documented publicly and this job will not guess at a requester-pays bucket.\n"
        "Look these up from Hydromancer's docs or their support, then wire them into "
        "liqfade/data/ingest/hydromancer.py fetch_day():"
    )
    for index, item in enumerate(LOOKUP_CHECKLIST, start=1):
        print(f"  {index}. {item}")
    return None


def summarise(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Liquidation count and total notional per coin. The unit of comparison."""
    totals: dict[str, dict[str, float]] = {}
    for row in rows:
        coin = row.get("coin") or "unknown"
        entry = totals.setdefault(coin, {"count": 0.0, "notional_usd": 0.0})
        entry["count"] += 1
        entry["notional_usd"] += float(row.get("notional_usd") or 0.0)
    return totals


def _rel_diff(left: float, right: float) -> float:
    """Relative difference against the larger magnitude. 0.0 when both are zero."""
    scale = max(abs(left), abs(right))
    return 0.0 if scale == 0 else abs(left - right) / scale


def compare(
    hydromancer: dict[str, dict[str, float]], hl_s3: dict[str, dict[str, float]]
) -> dict[str, Any]:
    """Per coin diff plus a verdict. `hl_s3` is the reference: it is the exchange's own."""
    coins = sorted(set(hydromancer) | set(hl_s3))
    rows: list[dict[str, Any]] = []
    for coin in coins:
        left = hydromancer.get(coin, {"count": 0.0, "notional_usd": 0.0})
        right = hl_s3.get(coin, {"count": 0.0, "notional_usd": 0.0})
        rows.append(
            {
                "coin": coin,
                "hydromancer_count": int(left["count"]),
                "hl_s3_count": int(right["count"]),
                "hydromancer_notional": left["notional_usd"],
                "hl_s3_notional": right["notional_usd"],
                "count_diff": _rel_diff(left["count"], right["count"]),
                "notional_diff": _rel_diff(left["notional_usd"], right["notional_usd"]),
            }
        )
    mismatched = [
        str(row["coin"])
        for row in rows
        if float(row["count_diff"]) > TOLERANCE or float(row["notional_diff"]) > TOLERANCE
    ]
    return {
        "rows": rows,
        "mismatched": mismatched,
        "verdict": "match" if not mismatched else "MISMATCH",
    }


def render(day: date, result: dict[str, Any]) -> str:
    """The markdown report. One table, one verdict, no charts."""
    lines = [
        "# Hydromancer versus Hyperliquid S3",
        "",
        f"Day compared: {day}. Reference is `hl_s3`, the exchange's own fills.",
        f"Tolerance: {TOLERANCE:.0%} relative difference per coin.",
        "",
        f"**Verdict: {result['verdict']}**",
        "",
    ]
    if result["mismatched"]:
        lines += [
            "Coins that disagree: " + ", ".join(result["mismatched"]),
            "",
            "Do not use Hydromancer as the liquidation source until this is explained. "
            "Pay for the S3 fills instead; a wrong liquidation table is worse than an "
            "expensive one.",
            "",
        ]
    else:
        lines += [
            "Counts and notional agree within tolerance on every coin. Hydromancer is "
            "safe to use as the liquidation source and saves most of the S3 egress bill.",
            "",
        ]
    lines += [
        "| coin | hydromancer count | hl_s3 count | count diff | "
        "hydromancer notional | hl_s3 notional | notional diff |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in result["rows"]:
        lines.append(
            f"| {row['coin']} | {row['hydromancer_count']} | {row['hl_s3_count']} | "
            f"{row['count_diff']:.2%} | {row['hydromancer_notional']:,.0f} | "
            f"{row['hl_s3_notional']:,.0f} | {row['notional_diff']:.2%} |"
        )
    return "\n".join(lines) + "\n"


def write_report(reports_dir: Path, day: date, result: dict[str, Any]) -> Path:
    path = Path(reports_dir) / "data_quality" / "hydromancer_check.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(day, result))
    return path


def run(data_dir: Path, reports_dir: Path, day: date) -> int:
    """Compare one day. Returns 2 while the Hydromancer half is unimplemented."""
    import pyarrow.parquet as pq

    from liqfade.recorder.writer import lake_files, partition_dir

    theirs = fetch_day(day)
    if theirs is None:
        print(
            "\nThe comparison and the report are implemented and tested; only the fetch "
            "is missing. Until then, pull the day from S3 with `liqfade backfill s3-fills "
            f"--start {day} --end {day}` and budget the full egress."
        )
        return 2

    directory = partition_dir(Path(data_dir) / "parquet", "liquidations", day.isoformat())
    # lake_files, not a glob: macOS leaves a 4 KiB AppleDouble `._x.parquet` beside every
    # file our code writes to the owner's drive and pyarrow fails outright on one.
    files = sorted(lake_files(directory)) if directory.is_dir() else []
    ours = [
        row
        for path in files
        for row in pq.read_table(path, partitioning=None).to_pylist()
        if row["source"] == "hl_s3"
    ]
    if not ours:
        print(f"no hl_s3 liquidations for {day}; pull that day first so there is a reference")
        return 1

    result = compare(summarise(theirs), summarise(ours))
    path = write_report(reports_dir, day, result)
    print(f"{result['verdict']}: wrote {path}")
    return 0
