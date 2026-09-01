"""Scan the data lake and write reports/data_quality/counts.md. Runnable any time.

Answers the one question a backfill leaves open: is there a hole in it? Rows per table
per day, days with zero rows flagged, and the daily liquidation count drawn as a text
sparkline so a gap is visible without opening a chart.

Row counts come from the Parquet footers rather than from reading the data, so this stays
fast over a lake with a year of fills in it. The liquidation series does read the
`liquidations` partitions, which are small.

    uv run python scripts/research/data_quality.py [--data-dir data] [--reports-dir reports]
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path

import pyarrow.parquet as pq

# Eighth-blocks, low to high. The first is used for a nonzero-but-tiny day so a day with
# one liquidation never renders as blank and reads as a hole.
BLOCKS = "▁▂▃▄▅▆▇█"


def scan(root: Path) -> dict[str, dict[str, int]]:
    """table -> {YYYY-MM-DD: rows}, from Parquet metadata only."""
    counts: dict[str, dict[str, int]] = {}
    if not root.is_dir():
        return counts
    for table_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        per_day: dict[str, int] = {}
        for partition in sorted(table_dir.glob("date=*")):
            day = partition.name.removeprefix("date=")
            per_day[day] = sum(
                pq.read_metadata(path).num_rows for path in sorted(partition.glob("*.parquet"))
            )
        if per_day:
            counts[table_dir.name] = per_day
    return counts


def all_days(counts: dict[str, dict[str, int]]) -> list[str]:
    """Every calendar day between the lake's first and last, including missing ones.

    Listing only the partitions that exist would hide the worst case: a day with no
    directory at all, which is a hole rather than an empty day.
    """
    days = {day for per_day in counts.values() for day in per_day}
    if not days:
        return []
    first, last = date.fromisoformat(min(days)), date.fromisoformat(max(days))
    span = (last - first).days
    return [(first + timedelta(days=offset)).isoformat() for offset in range(span + 1)]


def sparkline(values: list[int]) -> str:
    """One block per value, scaled to the largest. Zero renders as a space, not a block."""
    if not values:
        return ""
    peak = max(values)
    if peak == 0:
        return " " * len(values)
    return "".join(
        " " if value == 0 else BLOCKS[min(len(BLOCKS) - 1, value * len(BLOCKS) // peak)]
        for value in values
    )


def render(counts: dict[str, dict[str, int]], days: list[str]) -> str:
    tables = sorted(counts)
    lines = [
        "# Data lake counts",
        "",
        f"{len(tables)} tables, {len(days)} days from {days[0] if days else 'n/a'} to "
        f"{days[-1] if days else 'n/a'}.",
        "",
    ]

    empty = {table: [day for day in days if counts[table].get(day, 0) == 0] for table in tables}
    flagged = {table: found for table, found in empty.items() if found}
    if flagged:
        lines += ["## Days with zero rows", ""]
        for table, found in sorted(flagged.items()):
            shown = ", ".join(found[:10]) + (
                f" and {len(found) - 10} more" if len(found) > 10 else ""
            )
            lines.append(f"- **{table}**: {len(found)} of {len(days)} days: {shown}")
        lines += [
            "",
            "A zero-row day is either a recorder outage, a backfill that has not reached "
            "that day yet, or a table that legitimately starts later. Explain each one "
            "before trusting a backtest that spans it.",
            "",
        ]
    else:
        lines += ["Every table has rows on every day in range.", "", ""]

    if "liquidations" in counts:
        series = [counts["liquidations"].get(day, 0) for day in days]
        lines += [
            "## Liquidations per day",
            "",
            "```",
            sparkline(series),
            f"{days[0]}{' ' * max(0, len(days) - 20)}{days[-1]}",
            f"peak {max(series):,} on {days[series.index(max(series))]}",
            "```",
            "",
        ]

    lines += ["## Rows per table per day", "", "| day | " + " | ".join(tables) + " |"]
    lines.append("|---|" + "---:|" * len(tables))
    for day in days:
        cells = [f"{counts[table].get(day, 0):,}" for table in tables]
        lines.append(f"| {day} | " + " | ".join(cells) + " |")

    totals = [f"{sum(counts[table].values()):,}" for table in tables]
    lines.append("| **total** | " + " | ".join(f"**{value}**" for value in totals) + " |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default="data", help="lake root holding parquet/")
    parser.add_argument("--reports-dir", default="reports", help="where the report is written")
    args = parser.parse_args(argv)

    counts = scan(Path(args.data_dir) / "parquet")
    if not counts:
        print(f"no parquet under {args.data_dir}/parquet: nothing to report")
        return 1

    days = all_days(counts)
    out = Path(args.reports_dir) / "data_quality" / "counts.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(counts, days))
    print(f"wrote {out}: {len(counts)} tables over {len(days)} days")
    for table in sorted(counts):
        missing = sum(1 for day in days if counts[table].get(day, 0) == 0)
        flag = f"  <- {missing} zero-row days" if missing else ""
        print(f"  {table}: {sum(counts[table].values()):,} rows{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
