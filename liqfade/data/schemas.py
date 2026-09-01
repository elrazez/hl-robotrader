"""Canonical Parquet schemas: the contract between the recorder, ingestion and backtest.

Column names, order and types are fixed here and nowhere else, so adding a column shows
up in every reader at once. Types follow CLAUDE.md: integer milliseconds UTC for time
(`ts_ms` exchange, `rx_ms` local receive), float64 for prices and sizes, plain strings
for the small enums so a duckdb query over the lake needs no dictionary decode.

Layout written by the recorder, per docs/DATA_SOURCES.md:

    data/parquet/{table}/date=YYYY-MM-DD/{venue}_{hour}.parquet

Historical pulls write `rx_ms = 0`, which is how a reader tells recorded data (a true
local receive time exists, so no-lookahead checks are meaningful) from backfilled data.
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

LIQUIDATIONS = pa.schema(
    [
        ("venue", STR),
        ("coin", STR),  # null when the venue symbol maps to no HL coin
        ("venue_symbol", STR),
        ("ts_ms", I64),
        ("rx_ms", I64),
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
        ("mark_px", F64),
        ("oracle_px", F64),
        ("mid_px", F64),
        ("funding", F64),  # hourly rate as a fraction, HL pays one eighth of 8h each hour
        ("open_interest", F64),  # coin units
        ("day_ntl_vlm", F64),  # USD
        ("premium", F64),
    ]
)

CANDLES_1M = pa.schema(
    [
        ("coin", STR),
        ("ts_ms", I64),  # candle open time
        ("o", F64),
        ("h", F64),
        ("l", F64),
        ("c", F64),
        ("v", F64),
        ("n", I64),  # trade count in the candle
    ]
)

XCHG_CTX = pa.schema(
    [
        ("venue", STR),
        ("coin", STR),
        ("ts_ms", I64),
        ("rx_ms", I64),
        ("open_interest", F64),
        ("funding", F64),
        ("predicted_funding", F64),
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


def rows_to_table(name: str, rows: list[dict[str, Any]]) -> pa.Table:
    """Build a canonical table from parsed rows. Missing keys become null."""
    return pa.Table.from_pylist(rows, schema=schema_for(name))
