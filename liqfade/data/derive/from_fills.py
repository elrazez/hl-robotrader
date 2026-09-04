"""Vectorised twins of `hl_s3.derive_trades` and `hl_s3.derive_liquidations`.

The dict functions in `liqfade/data/ingest/hl_s3.py` are the reference: the S3 parser
still uses them, one batch of freshly parsed JSON at a time, where a batch is small and
the rows are already Python objects. They are the wrong tool for a rebuild. A year of
`fills_full` is about 3 billion fills, and walking that through `dict` groups is six to
ten hours; the same work in polars is minutes, because nothing ever becomes a Python
object.

So these are twins, not replacements. They take one day of `fills_full` as a
`pl.DataFrame` and must produce **exactly** the rows the dict functions produce, in
exactly the same order. `tests/test_derive_from_fills.py` asserts that row for row on the
fixture and on a real day; if the two ever disagree the dict function is right.

**Order is the whole point.** `hl_s3.derive_trades` used to sort its output by
`(ts_ms, tid)`, and a Hyperliquid `tid` is a hash rather than a sequence, so inside a
millisecond the exchange's event order was replaced by an arbitrary one and a minute's
open and close came out wrong on 8 to 19% of minutes (docs/DECISIONS.md, 2026-09-04).
The rule here, and in everything downstream:

- the order of the rows in `fills_full` is the exchange's own event order,
- every `group_by` passes `maintain_order=True`, so groups come out in the order their
  first fill appears,
- the only sort is a stable sort on `ts_ms`,
- nothing sorts on `tid`, ever.

That `fills_full` really does still carry the exchange's order, after the compaction
folded a day's hourly files into one, is not assumed: `scripts/research/fills_order_check.py`
chains every perp fill's `start_position` against the previous fill's and found zero
broken links in 26.1M links over three days.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from liqfade.data.ingest.hl_s3 import LIQUIDATED_ON_BUY, LIQUIDATED_ON_SELL, VENUE

# One trade, seen from both sides: fills arrive in maker and taker pairs, so every event
# appears twice and is counted once. Same key as `hl_s3._groups`.
GROUP_KEY = ["block_time", "coin", "tid"]

# The `fills_full` columns these derivations read. Naming them keeps a 24 column, 13M row
# cascade day out of memory: reading all of it costs several GB, reading these costs one.
FILL_COLUMNS = [
    "block_time",
    "coin",
    "tid",
    "ts_ms",
    "px",
    "sz",
    "side",
    "crossed",
    "user_address",
    "liq_user",
    "liq_method",
]

# The canonical columns each twin emits, in canonical order minus `source`. `source` is
# stamped by the writer, exactly as `rows_to_table` stamps it for the dict path, which is
# also what lets the equality test compare `frame.to_dicts()` against the dict rows
# directly.
TRADE_COLUMNS = ["venue", "coin", "ts_ms", "rx_ms", "px", "sz", "side", "tid"]
LIQUIDATION_COLUMNS = [
    "venue",
    "coin",
    "venue_symbol",
    "ts_ms",
    "rx_ms",
    "side",
    "px",
    "sz",
    "notional_usd",
    "method",
    "complete",
]


def read_fills(files: list[Path]) -> pl.DataFrame:
    """One day of `fills_full`, in file order, with only the derived columns.

    `files` is `sorted(lake_files(...))`, never a bare glob: macOS leaves a 4 KiB
    AppleDouble `._x.parquet` beside every file our code writes to the owner's drive and
    polars fails outright on one.

    The files are read one at a time and concatenated rather than handed to
    `pl.read_parquet` as a list, because the concatenation order is the thing this whole
    module is about and an explicit loop is the only way to be sure of it. On this mount
    the cost is the file open, which is paid either way. `rechunk=False` keeps the
    concatenation a relabelling of chunks rather than a copy of the day.
    """
    frames = [pl.read_parquet(path, columns=FILL_COLUMNS) for path in files]
    return pl.concat(frames, how="vertical", rechunk=False)


def _stable_by_ts(frame: pl.DataFrame) -> pl.DataFrame:
    """Sort on `ts_ms` alone, stably, so rows sharing a millisecond keep their order.

    `fill_null(0)` mirrors the dict functions' `row["ts_ms"] or 0`: a row with no
    timestamp sorts as if it were at the epoch rather than being pushed to one end.
    """
    return frame.sort(pl.col("ts_ms").fill_null(0), maintain_order=True)


def derive_trades_frame(fills: pl.DataFrame) -> pl.DataFrame:
    """Canonical `trades` rows: one per (block_time, coin, tid), aggressor side.

    The twin of `hl_s3.derive_trades`. `crossed` marks the taker, so the taker's own
    `side` is the aggressor side; when only the maker's half of a pair is in the file the
    aggressor is the opposite of that maker's side, which is why a group with no crossed
    record still produces a row.
    """
    taker = pl.col("crossed").fill_null(False)
    picked = (
        fills.with_row_index("_row")
        .group_by(GROUP_KEY, maintain_order=True)
        .agg(
            _chosen=pl.col("_row").filter(taker).first(),
            _first=pl.col("_row").first(),
        )
        .with_columns(_has_taker=pl.col("_chosen").is_not_null())
    )
    rows = _gather(fills, picked).with_columns(_has_taker=picked.get_column("_has_taker"))

    return _stable_by_ts(
        rows.select(
            venue=pl.lit(VENUE, pl.String),
            coin=pl.col("coin"),
            ts_ms=pl.col("ts_ms"),
            rx_ms=pl.lit(0, pl.Int64),
            px=pl.col("px"),
            sz=pl.col("sz"),
            # The taker's B is a buy; a lone maker's B means the aggressor sold to it.
            # So the aggressor is a buyer exactly when `side == "B"` agrees with whether
            # the row we picked is the taker's.
            side=pl.when((pl.col("side") == "B") == pl.col("_has_taker"))
            .then(pl.lit("buy"))
            .otherwise(pl.lit("sell")),
            tid=pl.col("tid"),
        )
    )


def derive_liquidations_frame(fills: pl.DataFrame) -> pl.DataFrame:
    """Canonical `liquidations` rows: one per liquidation event, side of the forced party.

    The twin of `hl_s3.derive_liquidations`. Only fills carrying a `liquidation` object
    count, and a maker plus taker pair counts once. The forced party is the record whose
    `user_address` equals `liquidation.liquidatedUser`; when only the counterparty's half
    is present its side is inverted to recover the forced party's.

    Liquidations are a few thousand fills in a day of millions, so the groups that carry
    one are selected first and the rest of the work runs on that slice.
    """
    named = pl.col("liq_user").is_not_null() & (pl.col("liq_user") != "")
    indexed = fills.with_row_index("_row")
    # A semi-join does not promise to preserve the left frame's order, so the row index
    # taken before it is what restores it. Everything after this is first-appearance
    # order again, as `_groups` sees it.
    liq = (
        indexed.join(indexed.filter(named).select(GROUP_KEY).unique(), on=GROUP_KEY, how="semi")
        .sort("_row")
        # `over` broadcasts the group's liquidated user back onto each of its fills, so
        # the victim can be recognised row by row in the aggregation below.
        .with_columns(_liq_user=pl.col("liq_user").filter(named).first().over(GROUP_KEY))
    )
    picked = (
        liq.group_by(GROUP_KEY, maintain_order=True)
        .agg(
            _chosen=pl.col("_row").filter(pl.col("user_address") == pl.col("_liq_user")).first(),
            _first=pl.col("_row").first(),
        )
        .with_columns(_has_victim=pl.col("_chosen").is_not_null())
    )
    rows = _gather(fills, picked).with_columns(_has_victim=picked.get_column("_has_victim"))

    # The forced party sold if it is the victim and its side is "A"; if the victim's own
    # record is missing we hold the counterparty's, whose "B" means the victim sold.
    forced_to_sell = (pl.col("side") == "A") == pl.col("_has_victim")
    return _stable_by_ts(
        rows.select(
            venue=pl.lit(VENUE, pl.String),
            coin=pl.col("coin"),
            venue_symbol=pl.col("coin"),
            ts_ms=pl.col("ts_ms"),
            rx_ms=pl.lit(0, pl.Int64),
            side=pl.when(forced_to_sell)
            .then(pl.lit(LIQUIDATED_ON_SELL))
            .otherwise(pl.lit(LIQUIDATED_ON_BUY)),
            px=pl.col("px"),
            sz=pl.col("sz"),
            notional_usd=pl.col("px") * pl.col("sz"),
            method=pl.when(pl.col("liq_method").is_null() | (pl.col("liq_method") == ""))
            .then(pl.lit("unknown"))
            .otherwise(pl.col("liq_method")),
            complete=pl.lit(True, pl.Boolean),
        )
    )


def _gather(fills: pl.DataFrame, picked: pl.DataFrame) -> pl.DataFrame:
    """One row of `fills` per group of `picked`, in `picked`'s order.

    `_chosen` is the group's preferred fill (the taker, or the liquidated user's own
    record) and is null when the file holds only the other half of the pair; `_first`
    is the group's first fill, which is what the dict functions fall back to.
    """
    rows = picked.select(pl.coalesce("_chosen", "_first")).to_series()
    return fills.clear() if rows.is_empty() else fills[rows]
