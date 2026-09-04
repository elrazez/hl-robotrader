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

from liqfade.data.ingest.common import BackfillError
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

    The whole repair rests on "all fills of one millisecond sit in one file": the sort
    below is stable on `ts_ms` alone, so rows sharing a millisecond keep their file order,
    and that is only the exchange's order if no other file holds part of the same
    millisecond. Nothing checked it, so it is checked here, on every run.
    """
    frames = []
    last_max: int | None = None
    for path in files:
        frame = pl.read_parquet(path, columns=FILL_COLUMNS)
        frames.append(frame)
        first, last = frame["ts_ms"].min(), frame["ts_ms"].max()
        if first is None or last is None:
            continue
        first_ms, last_ms = int(first), int(last)  # type: ignore[arg-type]
        if last_max is not None and first_ms <= last_max:
            raise BackfillError(
                f"{path.name} starts at ts_ms {first_ms} but an earlier file of "
                f"{path.parent} already reaches {last_max}: a millisecond spans two "
                "files, so file order is no longer the exchange's event order. Compact "
                "the partition before rederiving it."
            )
        last_max = last_ms
    return pl.concat(frames, how="vertical", rechunk=False)


def stable_by_ts(frame: pl.DataFrame) -> pl.DataFrame:
    """Sort on `ts_ms` alone, stably, so rows sharing a millisecond keep their order.

    The hardest invariant in this repo, so it is written once and both callers use it:
    the twins here, and `hl_s3._assemble`, which sorts the same rows again after
    concatenating the partition's other venues around them.

    **The tie rule is that there is no tie to break.** Rows sharing a millisecond keep
    their input order, and for our rows that is `fills_full`'s order, which is the
    exchange's own event order. Never sort on `tid`: a Hyperliquid `tid` is a hash, not a
    sequence, so it replaces that order with an arbitrary one and a minute's open and
    close come out wrong (docs/DECISIONS.md, 2026-09-04). And after `_assemble` drops
    every Hyperliquid row that is not ours, a cross-venue tie inside one millisecond is
    between events on different exchanges, so the order between them carries no meaning
    and any stable answer is as good as another.

    `fill_null(0)` mirrors the dict functions' `row["ts_ms"] or 0`: a row with no
    timestamp sorts as if it were at the epoch rather than being pushed to one end.
    """
    return frame.sort(pl.col("ts_ms").fill_null(0), maintain_order=True)


def _pick_one_per_group(
    indexed: pl.DataFrame, fills: pl.DataFrame, preferred: pl.Expr, flag: str
) -> pl.DataFrame:
    """One row of `fills` per group of `indexed`, in first-appearance order, plus `flag`.

    `preferred` picks the group's meaningful fill: the taker for a trade, the liquidated
    user's own record for a liquidation. When the file holds only the other half of the
    pair there is none, so the group falls back to its first fill and `flag` is False,
    which is the branch that inverts the side. `indexed` carries `_row`, the position in
    `fills` taken before any join reordered anything.
    """
    picked = (
        indexed.group_by(GROUP_KEY, maintain_order=True)
        .agg(_chosen=pl.col("_row").filter(preferred).first(), _first=pl.col("_row").first())
        .with_columns(**{flag: pl.col("_chosen").is_not_null()})
    )
    rows = picked.select(pl.coalesce("_chosen", "_first")).to_series()
    gathered = fills.clear() if rows.is_empty() else fills[rows]
    return gathered.with_columns(**{flag: picked.get_column(flag)})


def derive_trades_frame(fills: pl.DataFrame) -> pl.DataFrame:
    """Canonical `trades` rows: one per (block_time, coin, tid), aggressor side.

    The twin of `hl_s3.derive_trades`. `crossed` marks the taker, so the taker's own
    `side` is the aggressor side; when only the maker's half of a pair is in the file the
    aggressor is the opposite of that maker's side, which is why a group with no crossed
    record still produces a row.
    """
    taker = pl.col("crossed").fill_null(False)
    rows = _pick_one_per_group(fills.with_row_index("_row"), fills, taker, "_has_taker")

    # The taker's B is a buy; a lone maker's B means the aggressor sold to it. Written as
    # the dict function's two branches rather than as `(side == "B") == _has_taker`,
    # because the XOR reads a null `side` as "not B" in both branches, where the dict
    # reads it as "not B" in one and falls through to `buy` in the other. The two
    # disagreed on a maker-only group with no side; neither writer emits one today.
    bid = pl.col("side") == "B"
    buy, sell = pl.lit("buy"), pl.lit("sell")
    return stable_by_ts(
        rows.select(
            venue=pl.lit(VENUE, pl.String),
            coin=pl.col("coin"),
            ts_ms=pl.col("ts_ms"),
            rx_ms=pl.lit(0, pl.Int64),
            px=pl.col("px"),
            sz=pl.col("sz"),
            side=pl.when(pl.col("_has_taker"))
            .then(pl.when(bid).then(buy).otherwise(sell))
            .otherwise(pl.when(bid).then(sell).otherwise(buy)),
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
    # nulls_equal=True (polars' current spelling of join_nulls), because `block_time` and
    # `tid` are both nullable Int64 and the default drops a row whose key holds a null.
    # `_groups` builds a Python tuple key, which matches `None` to `None` happily, so the
    # default silently dropped a liquidation the dict function keeps.
    liq = (
        indexed.join(
            indexed.filter(named).select(GROUP_KEY).unique(),
            on=GROUP_KEY,
            how="semi",
            nulls_equal=True,
        )
        .sort("_row")
        # `over` broadcasts the group's liquidated user back onto each of its fills, so
        # the victim can be recognised row by row in the aggregation below.
        .with_columns(_liq_user=pl.col("liq_user").filter(named).first().over(GROUP_KEY))
    )
    victim = pl.col("user_address") == pl.col("_liq_user")
    rows = _pick_one_per_group(liq, fills, victim, "_has_victim")

    # The forced party sold if it is the victim and its side is "A"; if the victim's own
    # record is missing we hold the counterparty's, whose "B" means the victim sold.
    forced_to_sell = (pl.col("side") == "A") == pl.col("_has_victim")
    return stable_by_ts(
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
