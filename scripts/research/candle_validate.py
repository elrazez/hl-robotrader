"""Validate `candles_1m` against a reference set of bars. Re-runnable, free.

The Phase 2 definition of done says the derived bars are "validated against the API on
the overlap days". That validation existed only as prose in docs/DECISIONS.md; this is
the script that produces those numbers again.

Two comparisons, because neither alone is conclusive:

- **Against Hyperliquid's own `candleSnapshot`** (`--reference api`). The exchange's
  answer, but it keeps only about 3 days of `1m`, so it can only check days inside that
  window. On a day the lake does not cover, `--ours tardis` builds our side from Tardis's
  free first-of-month Hyperliquid trades instead, which is how 2026-09-01 was checked.
- **Against the recorder's own `candle` stream** (`--reference recorder`), for a window
  the recorder captured. This one cannot separate a derivation bug from a source gap on
  its own either, which is why the report splits every rate by whether the two sides saw
  the same number of trades in the minute. A minute where `n` disagrees is a minute where
  one side missed a trade, and says nothing about the derivation.

    uv run python scripts/research/candle_validate.py --day 2026-09-03
    uv run python scripts/research/candle_validate.py --day 2026-09-01 --ours tardis
    uv run python scripts/research/candle_validate.py --day 2026-09-03 --reference recorder
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

import polars as pl

FIELDS = ("o", "h", "l", "c", "v", "n")
# `v` is a sum of float64 sizes built in a different order on each side, so it is
# compared relatively. Everything else is a price the exchange quoted or a count.
V_RELATIVE_TOLERANCE = 1e-9


# --- the two sides ------------------------------------------------------------


def lake_bars(data_dir: Path, day: date, *, source: str) -> pl.DataFrame:
    """`candles_1m` rows of one day for one `source`, from the lake."""
    from liqfade.recorder.writer import lake_files, partition_dir

    files = lake_files(partition_dir(Path(data_dir) / "parquet", "candles_1m", day.isoformat()))
    if not files:
        return pl.DataFrame(schema={"coin": pl.String, "ts_ms": pl.Int64})
    return (
        pl.scan_parquet([str(path) for path in files])
        .filter(pl.col("source") == source)
        .select(["coin", "ts_ms", *FIELDS])
        .collect()
    )


def api_bars(client: object, api_url: str, day: date, coins: list[str]) -> pl.DataFrame:
    """`candleSnapshot` bars for `coins` on `day`. Free, and about 3 days deep."""
    from liqfade.data.ingest.hl_candles import day_bounds, to_rows, walk_back

    start_ms, end_ms = day_bounds(day, day)
    rows: list[dict[str, object]] = []
    for coin in coins:
        for page in walk_back(client, api_url, coin, start_ms, end_ms):  # type: ignore[arg-type]
            rows += to_rows(page, start_ms, end_ms)
    if not rows:
        return pl.DataFrame(schema={"coin": pl.String, "ts_ms": pl.Int64})
    return pl.DataFrame(rows).select(["coin", "ts_ms", *FIELDS])


def tardis_bars(client: object, day: date, coins: list[str]) -> pl.DataFrame:
    """Bars derived from Tardis's free first-of-month Hyperliquid trades.

    Our side, for a day the lake does not cover. `tardis_free.run` no longer requests
    Hyperliquid trades, because `hl_s3` owns that venue in the lake; these are fetched
    directly and never written, so nothing is double counted.
    """
    from liqfade.data.derive.candles import bars_from_trades
    from liqfade.data.ingest.tardis_free import fetch, read_csv, to_trades, url_for
    from liqfade.data.schemas import SOURCE_TARDIS, rows_to_table

    rows: list[dict[str, object]] = []
    for coin in coins:
        payload = fetch(client, url_for("hyperliquid", "trades", day, coin))  # type: ignore[arg-type]
        if payload is None:
            print(f"  Tardis has no hyperliquid/trades/{day:%Y/%m}/01/{coin}.csv.gz")
            continue
        rows += to_trades(read_csv(payload), "hyperliquid")
    if not rows:
        return pl.DataFrame(schema={"coin": pl.String, "ts_ms": pl.Int64})
    frame = pl.from_arrow(rows_to_table("trades", rows, SOURCE_TARDIS))
    return bars_from_trades(frame).select(["coin", "ts_ms", *FIELDS])  # type: ignore[union-attr]


# --- the comparison -----------------------------------------------------------


def compare_bars(ours: pl.DataFrame, theirs: pl.DataFrame) -> pl.DataFrame:
    """Per coin match rates on the minutes both sides have.

    Two rows' worth of numbers per coin: the rate over every shared minute, and the rate
    over the minutes where `n` agrees. The second is the one that says something about
    the derivation; a minute where the trade counts differ is a minute one side missed a
    trade in, and its `o` and `c` can differ for that reason alone.
    """
    if ours.height == 0 or theirs.height == 0:
        return pl.DataFrame(schema={"coin": pl.String, "minutes": pl.UInt32})

    joined = ours.join(theirs, on=["coin", "ts_ms"], how="inner", suffix="_ref")
    matches = [
        (
            (pl.col("v") - pl.col("v_ref")).abs()
            <= V_RELATIVE_TOLERANCE * pl.max_horizontal(pl.col("v").abs(), pl.col("v_ref").abs())
            if field == "v"
            else pl.col(field) == pl.col(f"{field}_ref")
        ).alias(f"same_{field}")
        for field in FIELDS
    ]
    flagged = joined.with_columns(matches).with_columns(
        same_ohlc=pl.all_horizontal([pl.col(f"same_{f}") for f in ("o", "h", "l", "c")])
    )
    return (
        flagged.group_by("coin", maintain_order=True)
        .agg(
            pl.len().alias("minutes"),
            pl.col("same_n").sum().alias("same_n_minutes"),
            *[pl.col(f"same_{f}").mean().alias(f) for f in FIELDS],
            pl.col("same_ohlc").filter(pl.col("same_n")).mean().alias("ohlc_where_n_agrees"),
        )
        .sort("coin")
    )


def coverage(ours: pl.DataFrame, theirs: pl.DataFrame) -> dict[str, int]:
    """Minutes each side holds and the overlap, so a thin comparison is visible."""
    keys = ["coin", "ts_ms"]
    return {
        "ours": ours.height,
        "reference": theirs.height,
        "shared": ours.select(keys).join(theirs.select(keys), on=keys, how="inner").height,
    }


def render(day: date, ours_name: str, ref_name: str, table: pl.DataFrame, seen: dict) -> str:
    lines = [
        "# Candle validation",
        "",
        f"Day: {day}. Ours: `{ours_name}`. Reference: `{ref_name}`.",
        "",
        f"Minutes: {seen['ours']:,} ours, {seen['reference']:,} reference, "
        f"{seen['shared']:,} shared.",
        "",
        "`o/h/l/c` and `n` are exact equality; `v` is equal to within a relative "
        f"{V_RELATIVE_TOLERANCE:g}. The last column is the one that says something about "
        "the derivation: on a minute where the two sides disagree on `n`, one of them "
        "missed a trade, and `o` and `c` can differ for that reason alone.",
        "",
        "| coin | minutes | o | h | l | c | v | n | o/h/l/c where n agrees |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in table.iter_rows(named=True):
        rates = " | ".join(f"{row[field]:.2%}" for field in FIELDS)
        where = row["ohlc_where_n_agrees"]
        lines.append(
            f"| {row['coin']} | {row['minutes']:,} | {rates} | "
            + ("n/a" if where is None else f"{where:.2%} of {row['same_n_minutes']:,}")
            + " |"
        )
    return "\n".join(lines) + "\n"


# --- the script ---------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import httpx

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--day", required=True, help="YYYY-MM-DD, UTC")
    parser.add_argument("--ours", choices=("lake", "tardis"), default="lake")
    parser.add_argument("--reference", choices=("api", "recorder"), default="api")
    parser.add_argument("--coins", default="BTC,ETH,SOL,HYPE", help="comma separated HL coins")
    parser.add_argument("--data-dir", default="data", help="lake root holding parquet/")
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--api-url", default="https://api.hyperliquid.xyz")
    args = parser.parse_args(argv)

    day = datetime.strptime(args.day, "%Y-%m-%d").date()
    coins = [coin.strip().upper() for coin in args.coins.split(",") if coin.strip()]
    data_dir = Path(args.data_dir)

    with httpx.Client() as client:
        ours = (
            lake_bars(data_dir, day, source="derived")
            if args.ours == "lake"
            else tardis_bars(client, day, coins)
        )
        theirs = (
            api_bars(client, args.api_url, day, coins)
            if args.reference == "api"
            else lake_bars(data_dir, day, source="recorder")
        )

    ours = ours.filter(pl.col("coin").is_in(coins))
    theirs = theirs.filter(pl.col("coin").is_in(coins))
    seen = coverage(ours, theirs)
    if seen["shared"] == 0:
        print(
            f"no minute of {day} is in both sets ({seen['ours']:,} ours, "
            f"{seen['reference']:,} reference). `candleSnapshot` keeps about 3 days of 1m "
            "bars; for an older day use --ours tardis on a first-of-month.",
            file=sys.stderr,
        )
        return 1

    table = compare_bars(ours, theirs)
    report = render(day, args.ours, args.reference, table, seen)
    out = Path(args.reports_dir) / "data_quality" / f"candle_validation_{day}_{args.reference}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report)
    print(report)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
