"""Canonical Parquet schemas: the contract between the recorder, ingestion and backtest.

Column names, order and types are fixed here and nowhere else, so adding a column shows
up in every reader at once. Types follow CLAUDE.md: integer milliseconds UTC for time
(`ts_ms` exchange, `rx_ms` local receive), float64 for prices and sizes, plain strings
for the small enums so a duckdb query over the lake needs no dictionary decode.

Layout written by the recorder, per docs/DATA_SOURCES.md:

    data/parquet/{table}/date=YYYY-MM-DD/{venue}_{hour}.parquet

Every table carries `source`, naming the job that produced the row: `recorder` for the
live feeds, and one of the ingestion job names for a historical pull. Provenance has to
be a column and not a filename because nightly compaction folds every file of a day into
one, so a Tardis day and a recorded day for the same table end up in the same file.

Historical pulls also write `rx_ms = 0`, which is how a reader tells recorded data (a
true local receive time exists, so no-lookahead checks are meaningful) from backfilled
data. `source` says where it came from, `rx_ms` says whether it has a receive time.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

I64 = pa.int64()
F64 = pa.float64()
STR = pa.string()

# Allowed values for the string enums. Not enforced by validate_table, which checks the
# schema only; parsers are responsible for emitting these and their tests assert it.
LIQ_SIDES = ("long", "short")  # which side was liquidated, not the taker direction
LIQ_METHODS = ("market", "backstop", "unknown")
TRADE_SIDES = ("buy", "sell")  # the aggressor

# Values for `source`: which job wrote the row.
SOURCE_RECORDER = "recorder"
SOURCE_HL_S3 = "hl_s3"  # requester-pays node_fills_by_block
SOURCE_HL_API = "hl_api"  # public Info API: candleSnapshot
SOURCE_HL_ARCHIVE = "hl_archive"  # requester-pays hyperliquid-archive L2 snapshots
SOURCE_TARDIS = "tardis"  # free first-of-month CSVs
SOURCE_HYDROMANCER = "hydromancer"  # Reservoir S3, pre-parsed
SOURCE_DERIVED = "derived"  # built from another table in this lake, no network involved
SOURCES = (
    SOURCE_RECORDER,
    SOURCE_HL_S3,
    SOURCE_HL_API,
    SOURCE_HL_ARCHIVE,
    SOURCE_TARDIS,
    SOURCE_HYDROMANCER,
    SOURCE_DERIVED,
)

LIQUIDATIONS = pa.schema(
    [
        ("venue", STR),
        ("coin", STR),  # null when the venue symbol maps to no HL coin
        ("venue_symbol", STR),
        ("ts_ms", I64),
        ("rx_ms", I64),
        ("source", STR),
        ("side", STR),  # long | short: the side that was liquidated
        ("px", F64),
        ("sz", F64),
        ("notional_usd", F64),
        ("method", STR),  # market | backstop | unknown
        ("complete", pa.bool_()),  # false when the venue throttles or samples the feed
    ]
)

TRADES = pa.schema(
    [
        ("venue", STR),
        ("coin", STR),
        ("ts_ms", I64),
        ("rx_ms", I64),
        ("source", STR),
        ("px", F64),
        ("sz", F64),
        ("side", STR),  # buy | sell aggressor
        ("tid", I64),
    ]
)

BBO = pa.schema(
    [
        ("coin", STR),
        ("ts_ms", I64),
        ("rx_ms", I64),
        ("source", STR),
        ("bid_px", F64),
        ("bid_sz", F64),
        ("ask_px", F64),
        ("ask_sz", F64),
    ]
)

L2 = pa.schema(
    [
        ("coin", STR),
        ("ts_ms", I64),
        ("rx_ms", I64),
        ("source", STR),
        ("level", pa.int32()),  # 0 is the touch, up to 19
        ("bid_px", F64),
        ("bid_sz", F64),
        ("ask_px", F64),
        ("ask_sz", F64),
    ]
)

ASSET_CTX = pa.schema(
    [
        ("coin", STR),
        ("ts_ms", I64),
        ("rx_ms", I64),
        ("source", STR),
        ("mark_px", F64),
        ("oracle_px", F64),
        ("mid_px", F64),
        ("funding", F64),  # hourly rate as a fraction, HL pays one eighth of 8h each hour
        ("open_interest", F64),  # coin units
        ("day_ntl_vlm", F64),  # USD
        ("premium", F64),
        # Hyperliquid's impact prices, the average fill price of a fixed notional against
        # the book on each side. `activeAssetCtx` sends them as `impactPxs: [bid, ask]`
        # and the `asset_ctxs` archive as two columns, so the same spread signal is
        # available live and for the year of history. Nullable: a coin with an empty side
        # has none, and the archive predates the field on its oldest days.
        ("impact_bid_px", F64),
        ("impact_ask_px", F64),
    ]
)

CANDLES_1M = pa.schema(
    [
        ("coin", STR),
        ("ts_ms", I64),  # candle open time
        ("source", STR),
        ("o", F64),
        ("h", F64),
        ("l", F64),
        ("c", F64),
        ("v", F64),
        ("n", I64),  # trade count in the candle
    ]
)

# The pay-once archive. Every field of every fill for every coin, no universe filter, so
# a parsing rule that turns out wrong is fixed by rederiving locally rather than by
# buying the S3 bytes a second time. Source field names are Hyperliquid's camelCase
# (`startPosition`, `closedPnl`, `feeToken`, `builderFee`, `twapId`); they are spelled
# snake_case here to match the rest of the lake. `ts_ms` is the fill's own `time`.
#
# px, sz and the other decimals arrive as strings and are stored as float64. Hyperliquid
# prices carry at most 5 significant figures and sizes are bounded by szDecimals, so
# float64 holds every value it emits exactly.
FILLS_FULL = pa.schema(
    [
        ("coin", STR),
        ("ts_ms", I64),  # the fill's `time`
        ("rx_ms", I64),  # always 0: a historical pull has no local receive time
        ("source", STR),
        ("block_time", I64),
        ("block_number", I64),
        ("local_time", I64),  # when the node saw the block, not when we did
        ("user_address", STR),
        ("px", F64),
        ("sz", F64),
        ("side", STR),  # B | A as Hyperliquid sends it, this user's side of the fill
        ("start_position", F64),
        ("dir", STR),  # human readable action, e.g. "Close Long"
        ("closed_pnl", F64),
        ("hash", STR),
        ("oid", I64),
        ("crossed", pa.bool_()),  # true when this user was the taker
        ("fee", F64),
        ("fee_token", STR),
        ("tid", I64),
        ("cloid", STR),
        ("builder", STR),
        ("builder_fee", F64),
        ("twap_id", I64),
        ("liq_user", STR),  # the three liq_ columns are null on a normal fill
        ("liq_mark_px", F64),
        ("liq_method", STR),  # market | backstop
    ]
)

XCHG_CTX = pa.schema(
    [
        ("venue", STR),
        ("coin", STR),
        ("ts_ms", I64),
        ("rx_ms", I64),
        ("source", STR),
        ("open_interest", F64),
        ("funding", F64),
        ("predicted_funding", F64),
    ]
)

# Public Hyperliquid positions, polled from `clearinghouseState` for the addresses the
# tape names. One row per open position per poll, so the table is a time series of who is
# holding what and where the venue would liquidate them. Single venue by construction:
# no other exchange publishes its accounts, so this is not in MULTI_VENUE_TABLES.
HL_POSITIONS = pa.schema(
    [
        ("coin", STR),
        ("ts_ms", I64),  # the response's `time`
        ("rx_ms", I64),
        ("source", STR),
        ("address", STR),
        ("szi", F64),  # signed as HL sends it: negative is short
        ("entry_px", F64),
        # Nullable, and it stays null when the venue sends null. A position with no
        # liquidation level is a fact about the account; a zero would put a cluster at
        # price zero and every map built from this table would be wrong there.
        ("liquidation_px", F64),
        ("position_value_usd", F64),
        ("margin_used_usd", F64),
        ("leverage", F64),  # `leverage.value`; float because only the venue decides it
        ("leverage_type", STR),  # cross | isolated, from `leverage.type`
        ("max_leverage", I64),  # the asset's cap, an integer in `meta`
    ]
)

SCHEMAS: dict[str, pa.Schema] = {
    "liquidations": LIQUIDATIONS,
    "trades": TRADES,
    "bbo": BBO,
    "l2": L2,
    "asset_ctx": ASSET_CTX,
    "candles_1m": CANDLES_1M,
    "xchg_ctx": XCHG_CTX,
    "fills_full": FILLS_FULL,
    "hl_positions": HL_POSITIONS,
}

# Tables written by more than one venue. Used by compaction to decide whether a day
# collapses into {venue}_day.parquet or all_day.parquet.
MULTI_VENUE_TABLES = frozenset({"liquidations", "trades", "xchg_ctx"})


class SchemaError(ValueError):
    """A table does not match its canonical schema."""


def schema_for(name: str) -> pa.Schema:
    try:
        return SCHEMAS[name]
    except KeyError:
        raise SchemaError(f"unknown table {name!r}, expected one of {sorted(SCHEMAS)}") from None


def validate_table(name: str, table: pa.Table) -> None:
    """Raise SchemaError unless `table` matches the canonical schema exactly.

    Exactly means same columns, same order, same types. A superset or a reordering is an
    error rather than something to fix silently: the whole point of this module is that
    every file in the lake can be read with one schema.
    """
    expected = schema_for(name)
    actual = table.schema
    if actual.equals(expected, check_metadata=False):
        return
    if actual.names != expected.names:
        raise SchemaError(f"{name}: columns {actual.names} do not match canonical {expected.names}")
    wrong = [
        f"{field.name}: {actual.field(i).type} != {field.type}"
        for i, field in enumerate(expected)
        if not actual.field(i).type.equals(field.type)
    ]
    raise SchemaError(f"{name}: wrong column types: {', '.join(wrong)}")


def rows_to_table(name: str, rows: list[dict[str, Any]], source: str) -> pa.Table:
    """Build a canonical table from parsed rows. Missing keys become null.

    `source` is stamped on the whole table rather than read from the rows: one file is
    always the output of one job, and a required argument here is what stops a parser
    from quietly writing rows with no provenance.
    """
    table = pa.Table.from_pylist(rows, schema=schema_for(name))
    index = table.schema.get_field_index("source")
    return table.set_column(index, "source", pa.array([source] * table.num_rows, STR))
