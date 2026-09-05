"""Scan the data lake and write reports/data_quality/counts.md. The Phase 2 gate.

Answers the one question a backfill leaves open: is there a hole in it that would
silently bias a backtest? Per table per day: rows, files, and for the two tables that
carry more than one venue, the rows per `venue` and per `source`. Days with zero rows are
flagged, a "days present / days expected" line says how much of the span each table
covers, and the daily liquidation count is drawn as a text sparkline so a gap is visible
without opening a chart.

The last section is the side rule sanity check over the whole year, month by month: the
mean signed price move in the 60s into long liquidations against the same into short
liquidations. Long liquidations are forced selling, so the price into them must be
falling, every month. If that sign ever inverts, every downstream sign is wrong. It is
the same measurement `SideRuleCheck` makes on one file in `liqfade/data/ingest/hl_s3.py`,
in SQL because a year does not fit through a Python loop, and on Hyperliquid rows only,
so the liquidations and the price series come from one venue.

Row and file counts come from the Parquet footers rather than from reading the data, so
that part stays fast over a lake with a year of fills in it. The venue/source breakdown
and the side rule do read data: about a minute for the first and ten for the second over
a full year. `--footers-only` skips both.

    uv run python scripts/research/data_quality.py [--data-dir DIR] [--footers-only]
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import NamedTuple

import pyarrow.parquet as pq

# Eighth-blocks, low to high. The first is used for a nonzero-but-tiny day so a day with
# one liquidation never renders as blank and reads as a hole.
BLOCKS = "▁▂▃▄▅▆▇█"

# Not compacted, by design: they keep Tardis's own columns, so they have no canonical
# schema and no `ts_ms` to sort on, and the job writes one file per venue per coin.
# See docs/DECISIONS.md, 2026-09-04. Four venues by four coins is the whole expectation,
# and a day short of it is a re-pull that did not finish rather than a quiet gap.
SIDE_TABLES = ("tardis_book_snapshot_5", "tardis_derivative_ticker")
SIDE_TABLE_FILES = 16

# The tables that carry rows from more than one venue or more than one pull, and so need
# the counts split before "the day has rows" means anything.
BREAKDOWN_TABLES = ("liquidations", "trades")

# The window `SideRuleCheck` uses, in ms. Same number, same meaning.
SIDE_RULE_WINDOW_MS = 60_000


class Day(NamedTuple):
    """One partition, from its footers: rows in it and files it took to hold them."""

    rows: int
    files: int


Counts = dict[str, dict[str, Day]]


def scan(root: Path) -> Counts:
    """table -> {YYYY-MM-DD: Day}, from Parquet metadata only.

    Files come from `lake_files`, never a bare `*.parquet` glob: on the owner's drive
    macOS leaves a 4 KiB AppleDouble `._x.parquet` beside every file our code writes and
    `pq.read_metadata` fails outright on one, which is this report, the Phase 2 gate.
    """
    from liqfade.recorder.writer import lake_files

    counts: Counts = {}
    if not root.is_dir():
        return counts
    for table_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        per_day: dict[str, Day] = {}
        for partition in sorted(table_dir.iterdir()):
            if not partition.name.startswith("date="):
                continue
            day = partition.name.removeprefix("date=")
            try:
                files = sorted(lake_files(partition))
                per_day[day] = Day(
                    sum(pq.read_metadata(path).num_rows for path in files), len(files)
                )
            except OSError as exc:
                # A partition the filesystem itself cannot open. On the owner's NTFS
                # volume a drive drop left one directory entry that raises EINVAL on
                # `stat`, which a `date=*` glob dies on before reaching any good day.
                # A report that is the Phase 2 gate has to name that as a hole, not
                # crash on it.
                print(f"UNREADABLE {partition}: {exc}", file=sys.stderr)
                per_day[day] = Day(0, 0)
        if per_day:
            counts[table_dir.name] = per_day
    return counts


def all_days(counts: Counts) -> list[str]:
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


def days_expected(table: str, days: list[str]) -> int:
    """How many days of the span this table could hold if nothing were missing.

    Every table but the two Tardis side tables is meant to cover the span day by day.
    Those two exist only for Tardis's free first-of-month days, so measuring them against
    365 would report a 97% hole every run and teach the reader to ignore the line.
    """
    if table in SIDE_TABLES:
        return sum(1 for day in days if day.endswith("-01"))
    return len(days)


def expected_files(table: str) -> int:
    return SIDE_TABLE_FILES if table in SIDE_TABLES else 1


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


# --- the two sections that read data ------------------------------------------


def _partition_files(root: Path, table: str, day: str) -> list[str]:
    from liqfade.recorder.writer import lake_files, partition_dir

    directory = partition_dir(root, table, day)
    if not directory.is_dir():
        return []
    return [str(path) for path in sorted(lake_files(directory))]


def breakdown(
    root: Path, days: list[str], tables: tuple[str, ...] = BREAKDOWN_TABLES
) -> dict[str, dict[tuple[str, str], dict[str, int]]]:
    """table -> (venue, source) -> {day: rows}, read from the data.

    "The day has rows" is not the question for `liquidations` and `trades`: both carry
    four venues from two pulls, and a day can be full of Binance rows with every
    Hyperliquid row missing. One query per table over an explicit file list (never a
    glob, see `scan`), grouped by the file it came from, so a venue that stops appearing
    is visible per day rather than only in the year's total.
    """
    import duckdb

    con = duckdb.connect()
    con.execute("PRAGMA disable_progress_bar")
    found: dict[str, dict[tuple[str, str], dict[str, int]]] = {}
    for table in tables:
        files = [path for day in days for path in _partition_files(root, table, day)]
        if not files:
            continue
        rows = con.execute(
            "SELECT filename, venue, source, count(*) FROM "
            "read_parquet(?, filename = true, union_by_name = true) GROUP BY 1, 2, 3",
            [files],
        ).fetchall()
        per_pair: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
        for filename, venue, source, count in rows:
            day = Path(filename).parent.name.removeprefix("date=")
            key = (venue or "?", source or "?")
            per_pair[key][day] = per_pair[key].get(day, 0) + count
        found[table] = dict(per_pair)
    return found


# One day of the side rule. `bisect_right(times, t) - 1` in `SideRuleCheck.add` is the
# last trade at or before `t`, which is what an ASOF join on `>=` gives. A liquidation
# with no trade a full window before it is dropped by the join, exactly as `before < 0`
# drops it there; so is a non-positive baseline price.
SIDE_RULE_SQL = """
WITH tr AS (
    SELECT coin, ts_ms, px FROM read_parquet($trades)
    WHERE venue = 'hyperliquid' AND px IS NOT NULL AND ts_ms IS NOT NULL
), lq AS (
    SELECT coin, ts_ms, side FROM read_parquet($liquidations)
    WHERE venue = 'hyperliquid' AND ts_ms IS NOT NULL
)
SELECT side, count(*), sum((a.px - b.px) / b.px * 10000)
FROM lq
ASOF JOIN tr a ON lq.coin = a.coin AND lq.ts_ms >= a.ts_ms
ASOF JOIN tr b ON lq.coin = b.coin AND lq.ts_ms - $window >= b.ts_ms
WHERE b.px > 0
GROUP BY 1
"""


def side_rule(root: Path, days: list[str]) -> dict[str, dict[str, tuple[int, float]]]:
    """YYYY-MM -> side -> (liquidations, summed bps), one day at a time.

    Summed rather than averaged per day because a month's mean is over its liquidations,
    not over its days: 2025-10-10 alone carries more than most months.
    """
    import duckdb

    con = duckdb.connect()
    con.execute("PRAGMA disable_progress_bar")
    totals: dict[str, dict[str, tuple[int, float]]] = defaultdict(dict)
    for day in days:
        trades = _partition_files(root, "trades", day)
        liquidations = _partition_files(root, "liquidations", day)
        if not trades or not liquidations:
            continue
        try:
            rows = con.execute(
                SIDE_RULE_SQL,
                {
                    "trades": trades,
                    "liquidations": liquidations,
                    "window": SIDE_RULE_WINDOW_MS,
                },
            ).fetchall()
        except Exception as exc:  # a bad day is a finding in the report, not a crash
            print(f"SIDE RULE FAILED {day}: {exc}", file=sys.stderr)
            continue
        month = day[:7]
        for side, count, summed in rows:
            have_n, have_sum = totals[month].get(side, (0, 0.0))
            totals[month][side] = (have_n + count, have_sum + float(summed or 0.0))
    return dict(totals)


# --- rendering ----------------------------------------------------------------


def _coverage_lines(counts: Counts, days: list[str], tables: list[str]) -> list[str]:
    lines = [
        "## Days present / days expected",
        "",
        "Expected is every day of the span, except the two Tardis side tables, which only "
        "ever cover Tardis's free first-of-month days. A day counts as present when it "
        "holds rows.",
        "",
        "| table | days present | days expected | rows | files |",
        "|---|---:|---:|---:|---:|",
    ]
    for table in tables:
        present = sum(1 for day in days if counts[table].get(day, Day(0, 0)).rows > 0)
        expect = days_expected(table, days)
        rows = sum(day.rows for day in counts[table].values())
        files = sum(day.files for day in counts[table].values())
        short = "" if present == expect else f" **-{expect - present}**"
        lines.append(f"| {table} | {present}{short} | {expect} | {rows:,} | {files:,} |")
    return [*lines, ""]


def _file_lines(counts: Counts, days: list[str], tables: list[str]) -> list[str]:
    """Partitions holding anything but the file count compaction should have left."""
    lines: list[str] = []
    for table in tables:
        want = expected_files(table)
        odd = [
            (day, counts[table][day].files)
            for day in days
            if day in counts[table] and counts[table][day].files != want
        ]
        if odd:
            shown = ", ".join(f"{day} ({files})" for day, files in odd[:10])
            more = f" and {len(odd) - 10} more" if len(odd) > 10 else ""
            lines.append(
                f"- **{table}**: {len(odd)} partitions not at {want} file(s): {shown}{more}"
            )
    if not lines:
        return [
            "## Files per partition",
            "",
            "Every partition holds exactly the file count it should: one per day after "
            f"compaction, {SIDE_TABLE_FILES} (four venues by four coins) for the two "
            "uncompacted Tardis side tables.",
            "",
        ]
    return ["## Files per partition", "", *lines, ""]


def _breakdown_lines(found: dict[str, dict[tuple[str, str], dict[str, int]]]) -> list[str]:
    if not found:
        return []
    lines = [
        "## Rows per venue and source",
        "",
        "| table | venue | source | rows | days | first | last |",
        "|---|---|---|---:|---:|---|---|",
    ]
    for table in sorted(found):
        pairs = sorted(found[table].items(), key=lambda item: -sum(item[1].values()))
        for (venue, source), per_day in pairs:
            present = sorted(day for day, rows in per_day.items() if rows > 0)
            lines.append(
                f"| {table} | {venue} | {source} | {sum(per_day.values()):,} | "
                f"{len(present)} | {present[0] if present else 'n/a'} | "
                f"{present[-1] if present else 'n/a'} |"
            )
    return [*lines, ""]


def _side_rule_lines(totals: dict[str, dict[str, tuple[int, float]]]) -> list[str]:
    if not totals:
        return []
    lines = [
        "## Side rule, month by month",
        "",
        f"Mean signed price move in the {SIDE_RULE_WINDOW_MS // 1000}s into each "
        "liquidation, Hyperliquid rows only. Long liquidations are forced selling, so "
        "`long` must be below `short` in every month; a month where it is not inverts "
        "every downstream sign.",
        "",
        "| month | long n | long mean bps | short n | short mean bps | verdict |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for month in sorted(totals):
        sides = totals[month]
        long_n, long_sum = sides.get("long", (0, 0.0))
        short_n, short_sum = sides.get("short", (0, 0.0))
        long_mean = long_sum / long_n if long_n else None
        short_mean = short_sum / short_n if short_n else None
        if long_mean is None or short_mean is None:
            verdict = "inconclusive"
        elif long_mean < short_mean:
            verdict = "consistent"
        else:
            verdict = "**INVERTED**"
        lines.append(
            f"| {month} | {long_n:,} | "
            f"{'n/a' if long_mean is None else f'{long_mean:+.2f}'} | {short_n:,} | "
            f"{'n/a' if short_mean is None else f'{short_mean:+.2f}'} | {verdict} |"
        )
    return [*lines, ""]


def render(
    counts: Counts,
    days: list[str],
    found: dict[str, dict[tuple[str, str], dict[str, int]]] | None = None,
    totals: dict[str, dict[str, tuple[int, float]]] | None = None,
) -> str:
    tables = sorted(counts)
    lines = [
        "# Data lake counts",
        "",
        f"{len(tables)} tables, {len(days)} days from {days[0] if days else 'n/a'} to "
        f"{days[-1] if days else 'n/a'}.",
        "",
    ]

    lines += _coverage_lines(counts, days, tables)

    empty = {
        table: [day for day in days if counts[table].get(day, Day(0, 0)).rows == 0]
        for table in tables
    }
    flagged = {table: found_days for table, found_days in empty.items() if found_days}
    if flagged:
        lines += ["## Days with zero rows", ""]
        for table, found_days in sorted(flagged.items()):
            shown = ", ".join(found_days[:10]) + (
                f" and {len(found_days) - 10} more" if len(found_days) > 10 else ""
            )
            lines.append(f"- **{table}**: {len(found_days)} of {len(days)} days: {shown}")
        lines += [
            "",
            "A zero-row day is either a recorder outage, a backfill that has not reached "
            "that day yet, or a table that legitimately starts later. Explain each one "
            "before trusting a backtest that spans it.",
            "",
        ]
    else:
        lines += ["Every table has rows on every day in range.", "", ""]

    lines += _file_lines(counts, days, tables)

    if "liquidations" in counts:
        series = [counts["liquidations"].get(day, Day(0, 0)).rows for day in days]
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

    lines += _breakdown_lines(found or {})
    lines += _side_rule_lines(totals or {})

    lines += ["## Rows per table per day", "", "| day | " + " | ".join(tables) + " |"]
    lines.append("|---|" + "---:|" * len(tables))
    for day in days:
        cells = [f"{counts[table].get(day, Day(0, 0)).rows:,}" for table in tables]
        lines.append(f"| {day} | " + " | ".join(cells) + " |")

    grand = [f"{sum(day.rows for day in counts[table].values()):,}" for table in tables]
    lines.append("| **total** | " + " | ".join(f"**{value}**" for value in grand) + " |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("LIQFADE_DATA_DIR", "data"),
        help="lake root holding parquet/ (default: $LIQFADE_DATA_DIR)",
    )
    parser.add_argument("--reports-dir", default="reports", help="where the report is written")
    parser.add_argument(
        "--footers-only",
        action="store_true",
        help="skip the venue/source breakdown and the side rule, which read data",
    )
    args = parser.parse_args(argv)

    root = Path(args.data_dir) / "parquet"
    counts = scan(root)
    if not counts:
        print(f"no parquet under {args.data_dir}/parquet: nothing to report")
        return 1

    days = all_days(counts)
    found: dict[str, dict[tuple[str, str], dict[str, int]]] = {}
    totals: dict[str, dict[str, tuple[int, float]]] = {}
    if not args.footers_only:
        print("reading venue and source ...", flush=True)
        found = breakdown(root, days)
        print("checking the side rule month by month ...", flush=True)
        totals = side_rule(root, days)

    out = Path(args.reports_dir) / "data_quality" / "counts.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(counts, days, found, totals))
    print(f"wrote {out}: {len(counts)} tables over {len(days)} days")
    for table in sorted(counts):
        rows = sum(day.rows for day in counts[table].values())
        missing = days_expected(table, days) - sum(
            1 for day in days if counts[table].get(day, Day(0, 0)).rows > 0
        )
        flag = f"  <- {missing} days short of expected" if missing else ""
        print(f"  {table}: {rows:,} rows{flag}")
    inverted = [month for month, sides in totals.items() if _inverted(sides)]
    if inverted:
        print(f"SIDE RULE INVERTED in {', '.join(sorted(inverted))}", file=sys.stderr)
        return 2
    return 0


def _inverted(sides: dict[str, tuple[int, float]]) -> bool:
    long_n, long_sum = sides.get("long", (0, 0.0))
    short_n, short_sum = sides.get("short", (0, 0.0))
    if not long_n or not short_n:
        return False
    return long_sum / long_n >= short_sum / short_n


if __name__ == "__main__":
    raise SystemExit(main())
