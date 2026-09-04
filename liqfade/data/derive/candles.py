"""1 minute candles built from our own `trades` table. Exact, free, offline.

Hyperliquid's public `candleSnapshot` keeps only about 3 days of `1m` bars (measured
2026-09-03: 72 hours back returns data, 96 hours back returns nothing), so the year of
`candles_1m` cannot be downloaded. It does not need to be: `trades` for `hl_s3` is
derived from every fill on the exchange, so bars built from it are the exchange's own
bars rather than an approximation. `liqfade backfill candles` stays for the last few
days and is the validation reference; see docs/DATA_SOURCES.md, "Candles and order book".

Four things measured against the live API on 2026-09-04 rather than assumed:

- **`tid` is a hash, not a sequence, so it cannot order trades.** Hyperliquid trade ids
  look like 997296749534590 and 653061949558227 for two fills of the same sweep, in that
  order. Ordering a millisecond's trades by `tid` therefore picks an arbitrary one to be
  the open and another to be the close: it matched the API's `o` on only 76% of BTC
  minutes and its `c` on 91%, while `h` matched 100% and `n` and `v` were identical on
  the very rows that disagreed, which is the signature of a trade set that is right and
  an order that is not. **ROW_ORDER_IS_EXCHANGE_ORDER:** the tie-break is the row's
  position in the `trades` file instead. Both writers emit rows in the exchange's own
  order (the S3 parser walks blocks in order, the recorder and Tardis follow the stream)
  and `compact_day` sorts with a stable Arrow sort, so a millisecond's rows keep that
  order in the lake. This is the only ordering signal the canonical `trades` schema
  carries; `fills_full` also has `block_number`, if a future rebuild ever needs it.


- **`v` is coin units, not USD.** BTC bars come back around 6.5 for a busy minute, which
  is BTC, not dollars. So `v` is the plain sum of `sz`.
- **The API emits empty minutes.** A minute with no trades comes back as a real bar with
  `v = 0.0`, `n = 0` and `o = h = l = c` set to the previous close; TNSR had 291 such
  bars in 360 minutes and no gap in the `t` sequence. Those bars are replicated here, so
  a window of N bars spans N minutes whether it is reading history or the live `candle`
  stream. Sparse output would have made the same window mean different things in the
  backtest and in production.
- **Empty minutes are filled between a coin's first and last trade of the day, not
  across the whole UTC day.** Filling 00:00 to 23:59 would need the previous day's close
  to seed the leading minutes, which makes a day file depend on the day before it and
  stops `--force` rebuilding one day on its own. For every coin liquid enough to trade,
  the first trade is in minute 0 and the last in minute 1439, so the two rules agree;
  they differ only on the dead ends of illiquid coins (11.5% of bars on 2026-03-15, none
  of them on BTC, ETH, SOL or HYPE).

Only `venue == "hyperliquid"` rows become Hyperliquid bars. `trades` also holds Binance,
Bybit and OKX rows from the twelve Tardis free days, and on those same days it holds
Hyperliquid rows twice: once from `hl_s3` and once from `tardis`, the same trades under
the same `tid`. Rows are therefore deduplicated on `(coin, tid)`, which is the exchange's
own identifier for a fill and is unique per coin-day (checked on both a Tardis day and a
plain one). Without it those twelve days would carry roughly double `v` and `n` for BTC,
ETH, SOL and HYPE.
"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import polars as pl

from liqfade.data.schemas import SOURCE_DERIVED, SOURCE_HL_API, schema_for, validate_table
from liqfade.log import get_logger
from liqfade.recorder.writer import lake_files, partition_dir, write_atomic

log = get_logger(__name__)

TABLE = "candles_1m"
SOURCE_TABLE = "trades"
VENUE = "hyperliquid"
MINUTE_MS = 60_000
# One file per day, named like a compacted partition so `liqfade compact` leaves it
# alone: writer.compact_day skips a partition that is already a single `_day` file.
OUTPUT_NAME = f"{VENUE}_day.parquet"

# The canonical `candles_1m` columns, in order, as polars types. `to_arrow().cast()`
# turns this into the pyarrow schema; polars spells strings `large_string` and the cast
# is what reconciles that.
_SCHEMA: dict[str, pl.DataType] = {
    "coin": pl.String(),
    "ts_ms": pl.Int64(),
    "source": pl.String(),
    "o": pl.Float64(),
    "h": pl.Float64(),
    "l": pl.Float64(),
    "c": pl.Float64(),
    "v": pl.Float64(),
    "n": pl.Int64(),
}


def bars_from_trades(trades: pl.DataFrame) -> pl.DataFrame:
    """Canonical `candles_1m` rows for the Hyperliquid trades of one day.

    `trades` is the canonical `trades` schema and may hold any number of coins and any
    venue; rows for other venues are dropped here rather than by the caller, so the
    function is correct on its own. Empty minutes inside a coin's traded span are
    emitted with `v = 0`, `n = 0` and `o = h = l = c` at the previous close, as the API
    does.

    Trades sharing a millisecond keep the order they have in `trades`, which is the
    order the exchange emitted them; see ROW_ORDER_IS_EXCHANGE_ORDER below.
    Returns an empty frame with the canonical columns when there are no Hyperliquid rows.
    """
    rows = (
        trades.filter(pl.col("venue") == VENUE)
        .with_row_index("_row")
        # `tid` is the exchange's fill id, unique per coin-day and never null on a
        # Hyperliquid row (both writers parse it as an int). The twelve Tardis days
        # carry the same Hyperliquid trades a second time; this is what stops them
        # counting twice. keep="first" on a frame still in file order keeps the copy
        # that came first, so `_row` stays a file position.
        .unique(subset=["coin", "tid"], keep="first", maintain_order=True)
        .sort(["coin", "ts_ms", "_row"])
    )
    if rows.height == 0:
        return pl.DataFrame(schema=_SCHEMA)

    bars = (
        rows.with_columns((pl.col("ts_ms") // MINUTE_MS * MINUTE_MS).alias("ts_ms"))
        # maintain_order on rows already sorted by (coin, ts_ms, _row) is what makes
        # `first` the earliest trade of the minute and `last` the latest.
        .group_by(["coin", "ts_ms"], maintain_order=True)
        .agg(
            pl.col("px").first().alias("o"),
            pl.col("px").max().alias("h"),
            pl.col("px").min().alias("l"),
            pl.col("px").last().alias("c"),
            pl.col("sz").sum().alias("v"),
            pl.len().cast(pl.Int64).alias("n"),
        )
    )
    return _fill_empty_minutes(bars)


def _fill_empty_minutes(bars: pl.DataFrame) -> pl.DataFrame:
    """Add the minutes with no trades between each coin's first and last bar.

    The grid starts on a real bar by construction, so the forward fill never has a null
    to carry at the front and every emitted bar has a price.
    """
    grid = (
        bars.group_by("coin")
        .agg(
            pl.int_ranges(
                pl.col("ts_ms").min(), pl.col("ts_ms").max() + MINUTE_MS, MINUTE_MS
            ).alias("ts_ms")
        )
        .explode("ts_ms", empty_as_null=False)
    )
    return (
        grid.join(bars, on=["coin", "ts_ms"], how="left")
        .sort(["coin", "ts_ms"])
        .with_columns(pl.col("c").forward_fill().over("coin"))
        .with_columns(
            o=pl.col("o").fill_null(pl.col("c")),
            h=pl.col("h").fill_null(pl.col("c")),
            l=pl.col("l").fill_null(pl.col("c")),
            v=pl.col("v").fill_null(0.0),
            n=pl.col("n").fill_null(0),
            source=pl.lit(SOURCE_DERIVED, pl.String),
        )
        .select(list(_SCHEMA))
    )


def drop_hl_api(directory: Path, output: Path) -> int:
    """Remove the `hl_api` bars of a partition this derivation has just written.

    `liqfade backfill candles` writes `hyperliquid_HH.parquet` with `source = hl_api`
    into this same partition, and no reader filters `source`, so a day holding both
    counts every bar twice on exactly the overlap days the validation uses. It also
    leaves the partition permanently uncompactable, because `_hour_of` cannot read an
    hour out of `hyperliquid_day.parquet`. The derivation is built from every fill on the
    exchange and covers the whole year, so it owns any day it writes; the API job is for
    the last few days and for validation. Returns the rows dropped.

    One file is one job's output, so in practice this unlinks whole files. The filter is
    still written row-wise because a compaction that folded an `hl_api` file together
    with a recorded one would leave a file that is not.
    """
    dropped = 0
    for path in lake_files(directory):
        if path == output:
            continue
        frame = pl.read_parquet(path)
        keep = frame.filter(pl.col("source") != SOURCE_HL_API)
        if keep.height == frame.height:
            continue
        dropped += frame.height - keep.height
        if keep.height == 0:
            path.unlink()
        else:
            write_atomic(keep.to_arrow().cast(schema_for(TABLE)), path)
    return dropped


def read_trades(files: list[Path], coins: list[str] | None = None) -> pl.DataFrame:
    """Hyperliquid rows of one `trades` partition.

    `files` comes from `lake_files` rather than a glob: on the owner's drive macOS
    leaves a 4 KiB AppleDouble `._x.parquet` beside every file our code writes, and
    polars fails outright on it. The venue and coin filters are pushed into the scan,
    which matters on a Tardis day where Hyperliquid is a tenth of 51M rows.
    """
    scan = pl.scan_parquet([str(path) for path in files]).filter(pl.col("venue") == VENUE)
    if coins:
        scan = scan.filter(pl.col("coin").is_in(coins))
    return scan.collect()


def run(
    data_dir: Path,
    start: date,
    end: date,
    *,
    coins: list[str] | None = None,
    force: bool = False,
) -> int:
    """Derive `candles_1m` from `trades` for [start, end], oldest day first.

    One file per day, `candles_1m/date=YYYY-MM-DD/hyperliquid_day.parquet`. A day whose
    file already exists is skipped unless `force`, so an interrupted year resumes where
    it stopped.
    """
    from liqfade.data.ingest.common import BackfillError, day_range

    root = Path(data_dir) / "parquet"
    if not (root / SOURCE_TABLE).is_dir():
        raise BackfillError(f"no {SOURCE_TABLE} table at {root}: nothing to derive candles from")

    total_bars = 0
    days = day_range(start, end, newest_first=False)
    for day in days:
        began = time.monotonic()
        directory = partition_dir(root, TABLE, day.isoformat())
        output = directory / OUTPUT_NAME
        if output.exists() and not force:
            print(f"{day}  skipped (exists)", flush=True)
            continue

        files = lake_files(partition_dir(root, SOURCE_TABLE, day.isoformat()))
        bars = (
            bars_from_trades(read_trades(files, coins)) if files else pl.DataFrame(schema=_SCHEMA)
        )
        if bars.height == 0:
            print(f"{day}  no hyperliquid trades, nothing written", flush=True)
            continue

        arrow = bars.to_arrow().cast(schema_for(TABLE))
        validate_table(TABLE, arrow)
        directory.mkdir(parents=True, exist_ok=True)
        write_atomic(arrow, output)

        dropped = drop_hl_api(directory, output)

        n_coins = bars.get_column("coin").n_unique()
        total_bars += bars.height
        log.info(
            "derived_candles",
            date=day.isoformat(),
            coins=n_coins,
            bars=bars.height,
            hl_api_dropped=dropped,
        )
        print(
            f"{day}  {n_coins:>4} coins  {bars.height:>9,} bars  "
            + (f"{dropped:,} hl_api bars dropped  " if dropped else "")
            + f"{time.monotonic() - began:7.1f}s",
            flush=True,
        )

    print(f"\ntotal: {total_bars:,} bars over {len(days)} days")
    return 0
