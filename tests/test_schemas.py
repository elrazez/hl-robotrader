"""The canonical Parquet schemas are the project contract, so they get a guard test."""

from __future__ import annotations

import pyarrow as pa
import pytest

from liqfade.data.schemas import (
    SCHEMAS,
    SOURCE_RECORDER,
    SchemaError,
    rows_to_table,
    schema_for,
    validate_table,
)

GOOD_BBO = {
    "coin": "BTC",
    "ts_ms": 1_700_000_000_000,
    "rx_ms": 1_700_000_000_005,
    "bid_px": 60000.0,
    "bid_sz": 1.5,
    "ask_px": 60001.0,
    "ask_sz": 2.0,
}


def test_every_table_documented_in_data_sources_exists() -> None:
    assert set(SCHEMAS) == {
        "liquidations",
        "trades",
        "bbo",
        "l2",
        "asset_ctx",
        "candles_1m",
        "xchg_ctx",
        "fills_full",
    }


def test_fills_full_keeps_every_field_of_a_hyperliquid_fill() -> None:
    """The pay-once archive. A column dropped here is an S3 bill to get it back."""
    assert SCHEMAS["fills_full"].names == [
        "coin",
        "ts_ms",
        "rx_ms",
        "source",
        "block_time",
        "block_number",
        "local_time",
        "user_address",
        "px",
        "sz",
        "side",
        "start_position",
        "dir",
        "closed_pnl",
        "hash",
        "oid",
        "crossed",
        "fee",
        "fee_token",
        "tid",
        "cloid",
        "builder",
        "builder_fee",
        "twap_id",
        "liq_user",
        "liq_mark_px",
        "liq_method",
    ]


def test_column_order_matches_the_schema_doc() -> None:
    assert SCHEMAS["liquidations"].names == [
        "venue",
        "coin",
        "venue_symbol",
        "ts_ms",
        "rx_ms",
        "source",
        "side",
        "px",
        "sz",
        "notional_usd",
        "method",
        "complete",
    ]
    assert SCHEMAS["candles_1m"].names == [
        "coin",
        "ts_ms",
        "source",
        "o",
        "h",
        "l",
        "c",
        "v",
        "n",
    ]


def test_a_good_table_validates() -> None:
    validate_table("bbo", rows_to_table("bbo", [GOOD_BBO], SOURCE_RECORDER))


def test_source_is_stamped_on_every_row_and_never_read_from_the_row() -> None:
    """Provenance is per file, so a row that carries its own `source` cannot override it."""
    table = rows_to_table("bbo", [GOOD_BBO, {**GOOD_BBO, "source": "lies"}], "hl_s3")
    assert table.column("source").to_pylist() == ["hl_s3", "hl_s3"]


def test_a_wrong_typed_column_is_rejected() -> None:
    wrong = pa.Table.from_pylist(
        [GOOD_BBO],
        schema=pa.schema(
            [(f.name, pa.float32() if f.name == "bid_px" else f.type) for f in SCHEMAS["bbo"]]
        ),
    )
    with pytest.raises(SchemaError, match="bid_px"):
        validate_table("bbo", wrong)


def test_a_reordered_or_extra_column_is_rejected() -> None:
    fields = list(SCHEMAS["bbo"])
    reordered = pa.Table.from_pylist(
        [GOOD_BBO], schema=pa.schema([fields[1], fields[0], *fields[2:]])
    )
    with pytest.raises(SchemaError, match="do not match canonical"):
        validate_table("bbo", reordered)


def test_missing_keys_become_null_rather_than_failing() -> None:
    table = rows_to_table(
        "liquidations", [{"venue": "bybit", "venue_symbol": "BTCUSDT"}], SOURCE_RECORDER
    )
    validate_table("liquidations", table)
    assert table.column("coin").to_pylist() == [None]


def test_an_unknown_table_name_is_an_error() -> None:
    with pytest.raises(SchemaError, match="unknown table"):
        schema_for("nope")
