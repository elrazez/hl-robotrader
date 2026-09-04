"""Tardis CSV mapping for each exchange, and what the free-day pull does with it.

Sample rows are embedded rather than fixtured: they are three lines each and being able
to read the header next to the assertion is the point.
"""

from __future__ import annotations

import gzip
from datetime import date
from pathlib import Path

import httpx
import pyarrow.parquet as pq
import pytest

from liqfade.data.ingest import tardis_free
from liqfade.data.ingest.common import Manifest
from liqfade.data.schemas import SOURCE_TARDIS, validate_table

DAY = date(2026, 9, 1)

# Real Tardis column layouts. Timestamps are microseconds.
LIQ_CSV = {
    "binance-futures": (
        "exchange,symbol,timestamp,local_timestamp,id,side,price,amount\n"
        "binance-futures,BTCUSDT,1788609540000000,1788609540100000,,sell,78400.0,2.0\n"
        "binance-futures,BTCUSDT,1788609600000000,1788609600100000,,buy,78500.0,1.0\n"
    ),
    "bybit": (
        "exchange,symbol,timestamp,local_timestamp,id,side,price,amount\n"
        "bybit,BTCUSDT,1788609540000000,1788609540100000,,sell,78400.0,3.0\n"
    ),
    "okex-swap": (
        "exchange,symbol,timestamp,local_timestamp,id,side,price,amount\n"
        "okex-swap,BTC-USDT-SWAP,1788609540000000,1788609540100000,,sell,78400.0,5.0\n"
    ),
    "hyperliquid": (
        "exchange,symbol,timestamp,local_timestamp,id,side,price,amount\n"
        "hyperliquid,BTC,1788609540000000,1788609540100000,,buy,78400.0,4.0\n"
    ),
}
TRADES_CSV = (
    "exchange,symbol,timestamp,local_timestamp,id,side,price,amount\n"
    "bybit,BTCUSDT,1788609540000000,1788609540100000,12345,buy,78400.0,0.5\n"
    "bybit,BTCUSDT,1788609541000000,1788609541100000,not-a-number,sell,78399.0,0.25\n"
)
BOOK_CSV = (
    "exchange,symbol,timestamp,local_timestamp,"
    "asks[0].price,asks[0].amount,bids[0].price,bids[0].amount\n"
    "bybit,BTCUSDT,1788609540000000,1788609540100000,78401.0,1.5,78400.0,2.5\n"
)
TICKER_CSV = (
    "exchange,symbol,timestamp,local_timestamp,funding_timestamp,funding_rate,"
    "predicted_funding_rate,open_interest,last_price,index_price,mark_price\n"
    "bybit,BTCUSDT,1788609540000000,1788609540100000,1788624000000000,0.0001,0.00012,"
    "50000.0,78400.0,78398.0,78402.0\n"
)


def gz(text: str) -> bytes:
    return gzip.compress(text.encode())


def rows_of(csv_text: str) -> list[dict[str, str]]:
    return list(tardis_free.read_csv(gz(csv_text)))


def test_the_csv_reader_streams_rather_than_decompressing_the_whole_file() -> None:
    """A Binance BTC trades day is gigabytes once decompressed and decoded."""
    import inspect

    assert inspect.isgeneratorfunction(tardis_free.read_csv)
    assert rows_of(LIQ_CSV["bybit"])[0]["price"] == "78400.0"


# --- URLs and months ----------------------------------------------------------


def test_the_url_is_the_documented_free_day_url() -> None:
    assert tardis_free.url_for("binance-futures", "liquidations", DAY, "BTCUSDT") == (
        "https://datasets.tardis.dev/v1/binance-futures/liquidations/2026/09/01/BTCUSDT.csv.gz"
    )


def test_only_the_first_of_each_month_is_requested() -> None:
    """Day 01 is the free one; any other day in the range still maps to the 1st."""
    url = tardis_free.url_for("bybit", "trades", date(2026, 9, 17), "BTCUSDT")
    assert url.endswith("/2026/09/01/BTCUSDT.csv.gz")


def test_months_covers_every_month_touched_newest_first() -> None:
    assert tardis_free.months(date(2025, 11, 20), date(2026, 2, 3)) == [
        date(2026, 2, 1),
        date(2026, 1, 1),
        date(2025, 12, 1),
        date(2025, 11, 1),
    ]


def test_months_within_one_month_is_that_month() -> None:
    assert tardis_free.months(date(2026, 9, 5), date(2026, 9, 20)) == [DAY]


# --- liquidation mapping, one per exchange ------------------------------------


@pytest.mark.parametrize(
    ("exchange", "venue", "coin", "venue_symbol"),
    [
        ("binance-futures", "binance", "BTC", "BTCUSDT"),
        ("bybit", "bybit", "BTC", "BTCUSDT"),
        ("okex-swap", "okx", "BTC", "BTC-USDT-SWAP"),
        ("hyperliquid", "hyperliquid", "BTC", "BTC"),
    ],
)
def test_each_exchange_maps_onto_the_canonical_liquidation_schema(
    exchange: str, venue: str, coin: str, venue_symbol: str
) -> None:
    rows = tardis_free.to_liquidations(rows_of(LIQ_CSV[exchange]), venue, DAY)
    first = rows[0]
    assert first["venue"] == venue
    assert first["coin"] == coin
    assert first["venue_symbol"] == venue_symbol
    assert first["ts_ms"] == 1_788_609_540_000  # microseconds became milliseconds
    assert first["rx_ms"] == 0
    assert first["method"] == "unknown"


def test_a_sell_liquidation_order_means_a_long_was_liquidated() -> None:
    rows = tardis_free.to_liquidations(rows_of(LIQ_CSV["binance-futures"]), "binance", DAY)
    assert [row["side"] for row in rows] == ["long", "short"]


def test_notional_is_price_times_amount() -> None:
    row = tardis_free.to_liquidations(rows_of(LIQ_CSV["bybit"]), "bybit", DAY)[0]
    assert row["notional_usd"] == pytest.approx(78400.0 * 3.0)


def test_completeness_follows_bybit_and_the_date_it_started_publishing_everything() -> None:
    assert tardis_free.is_complete("bybit", date(2025, 3, 1))
    assert not tardis_free.is_complete("bybit", date(2025, 1, 1))
    assert not tardis_free.is_complete("binance", date(2026, 1, 1))
    assert not tardis_free.is_complete("okx", date(2026, 1, 1))


def test_an_unmappable_symbol_keeps_its_venue_symbol_and_a_null_coin() -> None:
    csv_text = LIQ_CSV["okex-swap"].replace("BTC-USDT-SWAP", "BTC-USD-SWAP")
    row = tardis_free.to_liquidations(rows_of(csv_text), "okx", DAY)[0]
    assert row["coin"] is None  # inverse swap: size is in USD contracts
    assert row["venue_symbol"] == "BTC-USD-SWAP"


# --- trade mapping ------------------------------------------------------------


def test_trades_map_onto_the_canonical_schema_with_the_aggressor_side() -> None:
    rows = tardis_free.to_trades(rows_of(TRADES_CSV), "bybit")
    assert [row["side"] for row in rows] == ["buy", "sell"]
    assert rows[0]["coin"] == "BTC"
    assert rows[0]["tid"] == 12345
    assert rows[0]["ts_ms"] == 1_788_609_540_000
    assert rows[0]["rx_ms"] == 0


def test_a_non_numeric_trade_id_becomes_null_rather_than_failing() -> None:
    assert tardis_free.to_trades(rows_of(TRADES_CSV), "bybit")[1]["tid"] is None


# --- side tables --------------------------------------------------------------


def test_book_snapshots_keep_their_native_columns(tmp_path: Path) -> None:
    written = tardis_free.write_side_table(
        tmp_path, "tardis_book_snapshot_5", "bybit", "BTC", DAY, gz(BOOK_CSV)
    )
    assert written == 1
    path = tmp_path / "tardis_book_snapshot_5" / f"date={DAY}" / "bybit_BTC_00.parquet"
    arrow = pq.read_table(path)
    assert "asks[0].price" in arrow.schema.names
    assert arrow.column("bids[0].amount").to_pylist() == [2.5]


def test_derivative_tickers_keep_their_native_columns(tmp_path: Path) -> None:
    tardis_free.write_side_table(
        tmp_path, "tardis_derivative_ticker", "bybit", "BTC", DAY, gz(TICKER_CSV)
    )
    path = tmp_path / "tardis_derivative_ticker" / f"date={DAY}" / "bybit_BTC_00.parquet"
    arrow = pq.read_table(path)
    assert set(arrow.schema.names) >= {"funding_rate", "predicted_funding_rate", "open_interest"}


def test_two_coins_of_one_venue_and_day_write_two_files(tmp_path: Path) -> None:
    """The bug this name guards: one file per venue and day kept only the last coin."""
    for coin in ("BTC", "ETH"):
        tardis_free.write_side_table(
            tmp_path, "tardis_book_snapshot_5", "bybit", coin, DAY, gz(BOOK_CSV)
        )
    directory = tmp_path / "tardis_book_snapshot_5" / f"date={DAY}"
    assert sorted(p.name for p in directory.glob("*.parquet")) == [
        "bybit_BTC_00.parquet",
        "bybit_ETH_00.parquet",
    ]


def test_an_empty_side_table_file_writes_nothing(tmp_path: Path) -> None:
    header = BOOK_CSV.splitlines()[0] + "\n"
    assert tardis_free.write_side_table(tmp_path, "t", "bybit", "BTC", DAY, gz(header)) == 0


# --- the job ------------------------------------------------------------------


class FakeTardis:
    """Serves the sample CSVs and 404s anything not listed, as Tardis does."""

    def __init__(self, available: dict[str, str]) -> None:
        self.available = available
        self.requested: list[str] = []

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = str(request.url)
        self.requested.append(path)
        for suffix, body in self.available.items():
            if path.endswith(suffix):
                return httpx.Response(200, content=gz(body))
        return httpx.Response(404)


def test_a_pull_writes_the_canonical_tables_and_the_side_tables(tmp_path: Path) -> None:
    api = FakeTardis(
        {
            "bybit/liquidations/2026/09/01/BTCUSDT.csv.gz": LIQ_CSV["bybit"],
            "bybit/trades/2026/09/01/BTCUSDT.csv.gz": TRADES_CSV,
            "bybit/book_snapshot_5/2026/09/01/BTCUSDT.csv.gz": BOOK_CSV,
            "bybit/derivative_ticker/2026/09/01/BTCUSDT.csv.gz": TICKER_CSV,
        }
    )
    assert (
        tardis_free.run(api.client(), tmp_path, DAY, DAY, ["BTC"], exchanges={"bybit": "bybit"})
        == 0
    )
    root = tmp_path / "parquet"
    for table in ("liquidations", "trades"):
        paths = sorted((root / table).glob("date=*/*.parquet"))
        assert paths
        for path in paths:
            arrow = pq.read_table(path, partitioning=None)
            validate_table(table, arrow)
            assert set(arrow.column("source").to_pylist()) == {SOURCE_TARDIS}
    assert list((root / "tardis_book_snapshot_5").glob("date=*/*.parquet"))
    assert list((root / "tardis_derivative_ticker").glob("date=*/*.parquet"))


def test_a_missing_month_is_skipped_and_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    api = FakeTardis({})
    tardis_free.run(
        api.client(),
        tmp_path,
        DAY,
        DAY,
        ["BTC"],
        exchanges={"bybit": "bybit"},
        data_types=("liquidations",),
    )
    out = capsys.readouterr().out
    assert "1 files Tardis does not have (404, skipped)" in out
    assert "bybit/liquidations/2026/09/01/BTCUSDT.csv.gz" in out


def test_an_unsupported_data_type_is_skipped_like_a_missing_month(tmp_path: Path) -> None:
    """Tardis answers 400, not 404, for a data type an exchange never had.

    That is coverage information, not a broken request, so the pull records it and moves
    on instead of aborting the whole run on the first file. Hyperliquid liquidations were
    the case that found this; they are no longer requested at all (see the test below),
    so the same 400 is exercised here on a venue that is still pulled.
    """
    api = FakeTardis({})
    api.handle = lambda request: httpx.Response(  # type: ignore[method-assign]
        400, json={"code": 300, "message": "Data type 'liquidations' is not supported for 'BTC'"}
    )
    assert (
        tardis_free.run(
            api.client(),
            tmp_path,
            DAY,
            DAY,
            ["BTC"],
            exchanges={"bybit": "bybit"},
            data_types=("liquidations",),
        )
        == 0
    )


def test_a_400_that_is_not_about_coverage_fails_the_object(tmp_path: Path) -> None:
    """Every 400 used to be recorded as done with zero rows and never asked again.

    A throttle or a malformed URL answering 400 was therefore permanent silent data loss.
    """
    api = FakeTardis({})
    api.handle = lambda request: httpx.Response(  # type: ignore[method-assign]
        400, json={"code": 429, "message": "Too many requests, slow down"}
    )
    with pytest.raises(httpx.HTTPStatusError):
        tardis_free.run(
            api.client(),
            tmp_path,
            DAY,
            DAY,
            ["BTC"],
            exchanges={"bybit": "bybit"},
            data_types=("liquidations",),
        )
    assert Manifest.load(tmp_path, tardis_free.JOB).objects == {}


def test_only_the_documented_body_reads_as_coverage() -> None:
    assert tardis_free.is_coverage_400("Data type 'liquidations' is not supported for 'BTC'")
    assert not tardis_free.is_coverage_400("Too many requests, slow down")
    assert not tardis_free.is_coverage_400("")


def test_hyperliquid_trades_and_liquidations_are_not_requested_but_the_side_tables_are(
    tmp_path: Path,
) -> None:
    """`hl_s3` owns Hyperliquid trades and liquidations; a Tardis copy would double them.

    Both carry the same exchange fills under the same `tid`, so on the twelve free days a
    `venue == hyperliquid` filter over `trades` would count every trade twice. The side
    tables have no other source and are still pulled.
    """
    api = FakeTardis(
        {
            "hyperliquid/book_snapshot_5/2026/09/01/BTC.csv.gz": BOOK_CSV,
            "hyperliquid/derivative_ticker/2026/09/01/BTC.csv.gz": TICKER_CSV,
        }
    )
    tardis_free.run(
        api.client(), tmp_path, DAY, DAY, ["BTC"], exchanges={"hyperliquid": "hyperliquid"}
    )
    assert [url.rsplit("/v1/", 1)[-1] for url in api.requested] == [
        "hyperliquid/book_snapshot_5/2026/09/01/BTC.csv.gz",
        "hyperliquid/derivative_ticker/2026/09/01/BTC.csv.gz",
    ]
    root = tmp_path / "parquet"
    assert not (root / "trades").exists()
    assert not (root / "liquidations").exists()
    assert list((root / "tardis_book_snapshot_5").glob("date=*/*.parquet"))


def test_a_404_is_remembered_so_the_next_run_does_not_ask_again(tmp_path: Path) -> None:
    api = FakeTardis({})
    kwargs = {"exchanges": {"bybit": "bybit"}, "data_types": ("liquidations",)}
    tardis_free.run(api.client(), tmp_path, DAY, DAY, ["BTC"], **kwargs)
    assert len(api.requested) == 1
    tardis_free.run(api.client(), tmp_path, DAY, DAY, ["BTC"], **kwargs)
    assert len(api.requested) == 1


def test_estimate_only_counts_the_files_and_fetches_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    api = FakeTardis({})
    tardis_free.run(api.client(), tmp_path, DAY, DAY, ["BTC", "ETH"], estimate_only=True)
    assert "Free, no AWS involved" in capsys.readouterr().out
    assert api.requested == []


def test_a_completed_file_is_recorded_in_the_manifest(tmp_path: Path) -> None:
    api = FakeTardis({"bybit/liquidations/2026/09/01/BTCUSDT.csv.gz": LIQ_CSV["bybit"]})
    tardis_free.run(
        api.client(),
        tmp_path,
        DAY,
        DAY,
        ["BTC"],
        exchanges={"bybit": "bybit"},
        data_types=("liquidations",),
    )
    assert Manifest.load(tmp_path, tardis_free.JOB).total_rows() == {"liquidations": 1}
