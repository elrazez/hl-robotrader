"""Drop the doubled half of Bybit ETH on 2026-01-01 from `trades`. One-off repair.

02b left a residual it could not place: Tardis `trades` in the lake were 173,868 rows
(0.05%) above the manifest. The 2026-09-05 quality gate found the whole of it in one file.
Binance and OKX match their manifests to the row; Bybit is over by 173,868, all of it on
2026-01-01 and all of it ETH, where the lake holds 657,605 rows against the manifest's
483,737 and 657,605 - 173,868 = 483,737 exactly.

**The first half of that file was written twice.** Group the day's Bybit ETH rows on
`(ts_ms, px, sz, side)`: hours 00:00 to 11:59 UTC have 140,841 groups and not one has an
odd count, while hours 12 to 23, and BTC on both halves of the same day, are odd on 85 to
90% of groups as real trades are. Those hours hold 347,736 rows, exactly twice 173,868. A
first attempt died after hour 11 without recording a manifest entry, the retry wrote all
24 hours, and compaction folded both into `all_day.parquet`.

So the repair is to keep half of every group in that slice, in file order, and touch
nothing else in the partition. `tid` is null on Tardis Bybit rows, which is why
`(coin, tid)` cannot do this and why the defect survived every dedup in the pipeline.

Three things guard it, because this deletes recorded data (CLAUDE.md rule 8):

- an odd group anywhere in the slice aborts the whole run, since "keep half" is only
  defined on even counts and a wrong parity would mean the diagnosis is wrong;
- the row counts are asserted against the manifest's 483,737 before anything is written;
- the rewrite is atomic, and the file is re-opened afterwards and its footer counted.

The filter is applied to the pyarrow table rather than to a polars frame so the schema,
and with it the footer metadata, survives: the partition carries the rederive's
`liqfade_rederive` stamp and must keep it or the next rederive redoes the day.

    uv run python scripts/research/drop_doubled_bybit_eth.py --dry-run
    uv run python scripts/research/drop_doubled_bybit_eth.py --yes
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, date, datetime, time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

DAY = "2026-01-01"
TABLE = "trades"
VENUE, COIN, SOURCE = "bybit", "ETH", "tardis"
# The doubled slice is hours 00:00 to 11:59 UTC of that day.
DOUBLED_BEFORE_HOUR = 12
HOUR_MS = 3_600_000
# What the Tardis manifest records for bybit ETHUSDT on 2026-01-01, which is the count
# the complete retry wrote and therefore the truth. See docs/DECISIONS.md, 2026-09-05.
EXPECTED_AFTER = 483_737
EXPECTED_DROPPED = 173_868

# The columns a Tardis trade is identified by here. `tid` is deliberately absent: it is
# null on every Tardis Bybit row, which is the reason this went unnoticed.
GROUP_COLUMNS = ("ts_ms", "px", "sz", "side")


class OddGroupError(Exception):
    """A group in the doubled slice has an odd count, so it was not written twice."""


def keep_mask(table: pa.Table, day: str = DAY) -> tuple[pa.Array, int, int]:
    """Which rows of a partition survive. Returns (mask, dropped, slice size).

    Every row outside the slice is kept. Inside it, the first half of each
    `(ts_ms, px, sz, side)` group is kept in file order, which is the copy the retry
    wrote in the same order Tardis's CSV carries.

    Raises `OddGroupError` if any group in the slice has an odd count: that would mean the
    slice is not a clean doubling and the row to drop is a guess rather than a copy.
    """
    import polars as pl

    midnight = datetime.combine(date.fromisoformat(day), time.min, tzinfo=UTC)
    cutoff = int(midnight.timestamp() * 1000) + DOUBLED_BEFORE_HOUR * HOUR_MS

    frame = pl.from_arrow(table.select(["venue", "coin", "source", *GROUP_COLUMNS])).with_row_index(
        "_row"
    )
    in_slice = (
        (pl.col("venue") == VENUE)
        & (pl.col("coin") == COIN)
        & (pl.col("source") == SOURCE)
        & (pl.col("ts_ms") < cutoff)
    )
    doubled = frame.filter(in_slice)
    if doubled.is_empty():
        return pa.array([True] * table.num_rows), 0, 0

    counted = doubled.with_columns(
        # Default `group_to_rows` mapping, so the occurrence index lands back on the
        # row it describes. `explode` returns the frame in group order instead, which
        # silently misaligns it against `_row` and drops the wrong copies.
        occurrence=pl.int_range(pl.len()).over(GROUP_COLUMNS),
        size=pl.len().over(GROUP_COLUMNS),
    )
    odd = counted.filter(pl.col("size") % 2 == 1)
    if not odd.is_empty():
        raise OddGroupError(
            f"{odd.height} of {doubled.height} rows in the slice sit in a group with an "
            "odd count, so the slice is not a clean doubling"
        )

    drop = set(counted.filter(pl.col("occurrence") >= pl.col("size") // 2)["_row"].to_list())
    mask = pa.array([index not in drop for index in range(table.num_rows)])
    return mask, len(drop), doubled.height


def repair(root: Path, *, dry_run: bool) -> dict[str, int]:
    """Rewrite the day's single compacted file without the second copy."""
    from liqfade.recorder.writer import lake_files, partition_dir, write_atomic

    directory = partition_dir(root, TABLE, DAY)
    files = sorted(lake_files(directory))
    if len(files) != 1:
        raise SystemExit(f"expected one compacted file in {directory}, found {len(files)}")
    path = files[0]

    table = pq.read_table(path, partitioning=None)
    mask, dropped, in_slice = keep_mask(table)
    kept = table.filter(mask)

    def slice_rows(source_table: pa.Table) -> int:
        import polars as pl

        frame = pl.from_arrow(source_table.select(["venue", "coin", "source"]))
        return int(
            frame.filter(
                (pl.col("venue") == VENUE) & (pl.col("coin") == COIN) & (pl.col("source") == SOURCE)
            ).height
        )

    after = slice_rows(kept)
    result = {
        "before": table.num_rows,
        "after": kept.num_rows,
        "dropped": dropped,
        "slice_rows": in_slice,
        "venue_coin_after": after,
    }
    if dropped != EXPECTED_DROPPED or after != EXPECTED_AFTER:
        raise SystemExit(
            f"refusing to write: dropped {dropped:,} (expected {EXPECTED_DROPPED:,}) "
            f"leaving {after:,} {VENUE} {COIN} rows (expected {EXPECTED_AFTER:,})"
        )
    if dry_run:
        return result

    write_atomic(kept, path)
    # The mount has timed out mid-write before and left a Parquet with no footer, so the
    # file is re-opened and counted rather than trusted. See docs/DECISIONS.md, 2026-09-04.
    written = pq.read_metadata(path).num_rows
    if written != kept.num_rows:
        raise SystemExit(f"wrote {written:,} rows, expected {kept.num_rows:,}: {path}")
    result["verified"] = written
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("LIQFADE_DATA_DIR", "data"),
        help="lake root holding parquet/ (default: $LIQFADE_DATA_DIR)",
    )
    parser.add_argument("--dry-run", action="store_true", help="report, change nothing")
    parser.add_argument("--yes", action="store_true", help="required to delete anything")
    args = parser.parse_args(argv)

    if not args.dry_run and not args.yes:
        print("this deletes recorded rows: pass --dry-run to look, or --yes to do it")
        return 1

    try:
        result = repair(Path(args.data_dir) / "parquet", dry_run=args.dry_run)
    except OddGroupError as exc:
        print(f"aborted, the diagnosis does not hold: {exc}", file=sys.stderr)
        return 2

    verb = "would drop" if args.dry_run else "dropped"
    print(f"{TABLE} {DAY}: {result['before']:,} rows, {verb} {result['dropped']:,}")
    print(f"  the doubled slice held {result['slice_rows']:,} rows over hours 0 to 11")
    print(
        f"  {VENUE} {COIN} {SOURCE} on the day: {result['venue_coin_after']:,}, "
        f"the manifest's {EXPECTED_AFTER:,}"
    )
    if "verified" in result:
        print(f"  re-opened and counted: {result['verified']:,} rows in the partition")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
