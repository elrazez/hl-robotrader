"""Delete Tardis's Hyperliquid rows from `trades` and `liquidations`. One-off sweep.

`hl_s3` is the one source of Hyperliquid trades and liquidations: both sources carry the
same fills under the same `tid`, so on the twelve Tardis free days a naive
`venue == "hyperliquid"` filter counts every trade twice. See docs/DATA_SOURCES.md,
"Counting a trade once".

`liqfade backfill rederive` already drops those rows, but only from a partition it
rewrites. A day with no `fills_full` returns `"empty"` and keeps them, and 2026-09-01,
the day the candle validation uses, is exactly that case. This sweeps every day instead.

Rewrites are in place and atomic (temp file plus `os.replace`), and the Parquet footer
metadata is carried over, so a partition the rederive stamped stays stamped and the
resume rule does not redo the day.

    uv run python scripts/research/drop_tardis_hl.py --start 2025-09-01 --end 2026-09-04
    uv run python scripts/research/drop_tardis_hl.py --start .. --end .. --yes
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pyarrow.compute as pc
import pyarrow.parquet as pq

TABLES = ("trades", "liquidations")
VENUE = "hyperliquid"
SOURCE = "tardis"


def days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def drop_from_file(path: Path, *, dry_run: bool) -> tuple[int, int]:
    """Rewrite one file without its Tardis Hyperliquid rows. Returns (before, dropped).

    `partitioning=None`: the directory is named `date=...` and hive discovery would
    otherwise append a `date` column that is not in the canonical schema.
    """
    from liqfade.recorder.writer import write_atomic

    table = pq.read_table(path, partitioning=None)
    theirs = pc.and_(
        pc.equal(table.column("venue"), VENUE), pc.equal(table.column("source"), SOURCE)
    )
    dropped = int(pc.sum(pc.cast(pc.fill_null(theirs, False), "int64")).as_py() or 0)
    if dropped and not dry_run:
        # `filter` keeps the schema, and with it the footer metadata: a partition the
        # rederive stamped must stay stamped or the next run redoes the day.
        write_atomic(table.filter(pc.invert(pc.fill_null(theirs, False))), path)
    return table.num_rows, dropped


def sweep(root: Path, start: date, end: date, *, dry_run: bool) -> list[dict[str, object]]:
    """One row per (day, table) that holds Tardis Hyperliquid rows, oldest first."""
    from liqfade.recorder.writer import lake_files, partition_dir

    found: list[dict[str, object]] = []
    for day in days(start, end):
        for table in TABLES:
            directory = partition_dir(root, table, day.isoformat())
            if not directory.is_dir():
                continue
            before = dropped = 0
            for path in sorted(lake_files(directory)):
                rows, gone = drop_from_file(path, dry_run=dry_run)
                before += rows
                dropped += gone
            if dropped:
                found.append(
                    {"day": day.isoformat(), "table": table, "before": before, "dropped": dropped}
                )
    return found


def render(found: list[dict[str, object]], *, dry_run: bool) -> str:
    verb = "would drop" if dry_run else "dropped"
    lines = [f"| day | table | before | {verb} | after |", "|---|---|---:|---:|---:|"]
    for row in found:
        before, dropped = int(row["before"]), int(row["dropped"])  # type: ignore[arg-type]
        lines.append(
            f"| {row['day']} | {row['table']} | {before:,} | {dropped:,} | {before - dropped:,} |"
        )
    totals = sum(int(row["dropped"]) for row in found)  # type: ignore[arg-type]
    lines.append(f"\n{len(found)} partitions, {totals:,} rows {verb}.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default="data", help="lake root holding parquet/")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD, inclusive, UTC")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD, inclusive, UTC")
    parser.add_argument("--dry-run", action="store_true", help="report, change nothing")
    parser.add_argument("--yes", action="store_true", help="required to delete anything")
    args = parser.parse_args(argv)

    if not args.dry_run and not args.yes:
        print("this deletes recorded rows: pass --dry-run to look, or --yes to do it")
        return 1

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    if end < start:
        print(f"end {end} is before start {start}", file=sys.stderr)
        return 1

    found = sweep(Path(args.data_dir) / "parquet", start, end, dry_run=args.dry_run)
    print(render(found, dry_run=args.dry_run) if found else "no Tardis Hyperliquid rows in range")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
