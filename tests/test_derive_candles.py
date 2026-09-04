"""Deriving `candles_1m` from `trades`. Offline, hand-built frames, no network.

The frames are written out rather than fixtured: a candle is three numbers and being
able to read the trades next to the expected bar is the point.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from liqfade.data.derive import candles
from liqfade.data.schemas import SOURCE_DERIVED, SOURCE_HL_S3, SOURCE_TARDIS, validate_table
from liqfade.recorder.writer import partition_dir, write_atomic

DAY = date(2026, 3, 15)
MINUTE = 1_773_532_800_000  # a whole minute, 2026-03-15 00:00:00 UTC
assert MINUTE % 60_000 == 0


def trade(
    ts_ms: int,
    px: float,
    sz: float,
    *,
    tid: int,
    coin: str = "BTC",
    venue: str = "hyperliquid",
    source: str = SOURCE_HL_S3,
    side: str = "buy",
) -> dict[str, object]:
    return {
        "venue": venue,
        "coin": coin,
        "ts_ms": ts_ms,
        "rx_ms": 0,
        "source": source,
        "px": px,
        "sz": sz,
        "side": side,
        "tid": tid,
    }


def frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    """A canonical `trades` frame, typed as the lake types it."""
    return pl.DataFrame(
        rows,
        schema={
            "venue": pl.String(),
            "coin": pl.String(),
            "ts_ms": pl.Int64(),
            "rx_ms": pl.Int64(),
            "source": pl.String(),
            "px": pl.Float64(),
            "sz": pl.Float64(),
            "side": pl.String(),
            "tid": pl.Int64(),
        },
    )


# --- the bars themselves ------------------------------------------------------


def test_three_trades_in_a_minute_give_one_bar() -> None:
    bars = candles.bars_from_trades(
        frame(
            [
                trade(MINUTE + 1_000, 100.0, 2.0, tid=1),
                trade(MINUTE + 2_000, 103.0, 3.0, tid=2),
                trade(MINUTE + 3_000, 99.0, 5.0, tid=3),
            ]
        )
    )
    assert bars.height == 1
    bar = bars.row(0, named=True)
    assert (bar["o"], bar["h"], bar["l"], bar["c"]) == (100.0, 103.0, 99.0, 99.0)
    assert bar["v"] == 10.0  # coin units, the sum of sz
    assert bar["n"] == 3
    assert bar["ts_ms"] == MINUTE  # labelled by the minute it opens
    assert bar["source"] == SOURCE_DERIVED


def test_trades_are_bucketed_by_the_minute_they_fall_in() -> None:
    bars = candles.bars_from_trades(
        frame(
            [
                trade(MINUTE + 59_999, 100.0, 1.0, tid=1),
                trade(MINUTE + 60_000, 200.0, 1.0, tid=2),
            ]
        )
    )
    assert bars["ts_ms"].to_list() == [MINUTE, MINUTE + 60_000]
    assert bars["o"].to_list() == [100.0, 200.0]


def test_ties_inside_one_millisecond_keep_the_order_of_the_trades_table() -> None:
    """A sweep fills several makers in one millisecond, so row order decides o and c.

    Hyperliquid's `tid` is a hash, not a sequence: ordering by it matched the API's open
    on 76% of BTC minutes, file order on 100%. See ROW_ORDER_IS_EXCHANGE_ORDER.
    """
    rows = [
        trade(MINUTE, 105.0, 1.0, tid=9_000_000_000_000),
        trade(MINUTE, 101.0, 1.0, tid=20),
        trade(MINUTE, 103.0, 1.0, tid=5_000_000_000_000),
    ]
    bar = candles.bars_from_trades(frame(rows)).row(0, named=True)
    assert (bar["o"], bar["c"]) == (105.0, 103.0)  # first row opens, last row closes
    # Sorting by tid instead would have given 101.0 and 105.0, which is what was wrong.
    reversed_bar = candles.bars_from_trades(frame(list(reversed(rows)))).row(0, named=True)
    assert (reversed_bar["o"], reversed_bar["c"]) == (103.0, 105.0)


def test_two_coins_do_not_mix() -> None:
    bars = candles.bars_from_trades(
        frame(
            [
                trade(MINUTE, 100.0, 1.0, tid=1, coin="BTC"),
                trade(MINUTE, 5.0, 7.0, tid=2, coin="SOL"),
            ]
        )
    )
    by_coin = {row["coin"]: row for row in bars.iter_rows(named=True)}
    assert by_coin["BTC"]["c"] == 100.0
    assert by_coin["SOL"]["c"] == 5.0
    assert by_coin["SOL"]["v"] == 7.0


def test_other_venues_are_excluded() -> None:
    """`trades` holds Binance, Bybit and OKX rows on the twelve Tardis days."""
    bars = candles.bars_from_trades(
        frame(
            [
                trade(MINUTE, 100.0, 1.0, tid=1),
                trade(MINUTE, 999.0, 50.0, tid=2, venue="binance", source=SOURCE_TARDIS),
            ]
        )
    )
    assert bars.height == 1
    assert bars.row(0, named=True)["h"] == 100.0


def test_the_same_hyperliquid_trade_from_two_sources_counts_once() -> None:
    """On a Tardis free day `trades` holds every HL trade twice, under one tid."""
    bars = candles.bars_from_trades(
        frame(
            [
                trade(MINUTE, 100.0, 4.0, tid=77, source=SOURCE_HL_S3),
                trade(MINUTE, 100.0, 4.0, tid=77, source=SOURCE_TARDIS),
                trade(MINUTE, 101.0, 1.0, tid=78, source=SOURCE_HL_S3),
            ]
        )
    )
    bar = bars.row(0, named=True)
    assert bar["n"] == 2
    assert bar["v"] == 5.0


def test_no_hyperliquid_rows_gives_an_empty_frame_with_the_right_columns() -> None:
    bars = candles.bars_from_trades(
        frame([trade(MINUTE, 1.0, 1.0, tid=1, venue="bybit", source=SOURCE_TARDIS)])
    )
    assert bars.height == 0
    assert bars.columns == ["coin", "ts_ms", "source", "o", "h", "l", "c", "v", "n"]


# --- empty minutes ------------------------------------------------------------


def test_a_minute_with_no_trades_repeats_the_previous_close_at_zero_volume() -> None:
    """Measured on the API 2026-09-04: it emits these rather than omitting the minute."""
    bars = candles.bars_from_trades(
        frame(
            [
                trade(MINUTE, 100.0, 1.0, tid=1),
                trade(MINUTE + 3 * 60_000, 120.0, 2.0, tid=2),
            ]
        )
    )
    assert bars["ts_ms"].to_list() == [MINUTE + i * 60_000 for i in range(4)]
    filled = bars.row(1, named=True)
    assert (filled["o"], filled["h"], filled["l"], filled["c"]) == (100.0,) * 4
    assert filled["v"] == 0.0
    assert filled["n"] == 0


def test_the_fill_stops_at_each_coins_own_last_trade() -> None:
    bars = candles.bars_from_trades(
        frame(
            [
                trade(MINUTE, 100.0, 1.0, tid=1, coin="BTC"),
                trade(MINUTE + 5 * 60_000, 101.0, 1.0, tid=2, coin="BTC"),
                trade(MINUTE + 60_000, 5.0, 1.0, tid=3, coin="SOL"),
            ]
        )
    )
    counts = bars.group_by("coin").len().to_dicts()
    assert {row["coin"]: row["len"] for row in counts} == {"BTC": 6, "SOL": 1}


# --- invariants ---------------------------------------------------------------


@pytest.mark.parametrize("seed", range(12))
def test_low_is_below_open_and_close_and_high_is_above_them(seed: int) -> None:
    """Property test over pseudo-random trades: the ohlc ordering can never break."""
    rng = pl.Series("x", [(seed * 37 + i * 17) % 41 for i in range(30)]).to_list()
    rows = [
        trade(MINUTE + (i // 5) * 60_000 + i * 137, 50.0 + value, 1.0 + value / 10, tid=i)
        for i, value in enumerate(rng)
    ]
    bars = candles.bars_from_trades(frame(rows))
    assert bars.height > 0
    for bar in bars.iter_rows(named=True):
        assert bar["l"] <= min(bar["o"], bar["c"])
        assert bar["h"] >= max(bar["o"], bar["c"])
        assert bar["l"] <= bar["h"]
        assert bar["v"] >= 0.0
        assert bar["n"] >= 0


# --- the runner ---------------------------------------------------------------


def write_trades(data_dir: Path, day: date, rows: list[dict[str, object]]) -> None:
    from liqfade.data.schemas import rows_to_table

    directory = partition_dir(data_dir / "parquet", "trades", day.isoformat())
    directory.mkdir(parents=True, exist_ok=True)
    table = rows_to_table("trades", rows, SOURCE_HL_S3)  # type: ignore[arg-type]
    write_atomic(table, directory / "all_day.parquet")


def output_path(data_dir: Path, day: date) -> Path:
    return partition_dir(data_dir / "parquet", "candles_1m", day.isoformat()) / candles.OUTPUT_NAME


def read_out(data_dir: Path, day: date = DAY) -> pa.Table:
    """partitioning=None, as compaction reads: hive discovery would append a `date`
    column that is not in the canonical schema."""
    return pq.read_table(output_path(data_dir, day), partitioning=None)


def test_a_run_writes_one_valid_file_per_day(tmp_path: Path) -> None:
    write_trades(
        tmp_path,
        DAY,
        [
            trade(MINUTE, 100.0, 1.0, tid=1),
            trade(MINUTE + 30_000, 102.0, 2.0, tid=2),
        ],
    )
    assert candles.run(tmp_path, DAY, DAY) == 0

    arrow = read_out(tmp_path)
    validate_table("candles_1m", arrow)
    assert arrow.num_rows == 1
    assert arrow.column("source").to_pylist() == [SOURCE_DERIVED]
    assert arrow.column("c").to_pylist() == [102.0]


def test_a_day_with_no_hyperliquid_trades_writes_nothing(tmp_path: Path) -> None:
    write_trades(tmp_path, DAY, [trade(MINUTE, 1.0, 1.0, tid=1, venue="okx", source=SOURCE_TARDIS)])
    assert candles.run(tmp_path, DAY, DAY) == 0
    assert not output_path(tmp_path, DAY).exists()


def test_a_second_run_skips_the_day_and_force_rewrites_it(tmp_path: Path) -> None:
    write_trades(tmp_path, DAY, [trade(MINUTE, 100.0, 1.0, tid=1)])
    candles.run(tmp_path, DAY, DAY)
    first = output_path(tmp_path, DAY).stat().st_mtime_ns

    write_trades(
        tmp_path, DAY, [trade(MINUTE, 100.0, 1.0, tid=1), trade(MINUTE, 111.0, 1.0, tid=2)]
    )
    candles.run(tmp_path, DAY, DAY)
    assert output_path(tmp_path, DAY).stat().st_mtime_ns == first
    assert read_out(tmp_path).column("n").to_pylist() == [1]

    candles.run(tmp_path, DAY, DAY, force=True)
    assert read_out(tmp_path).column("n").to_pylist() == [2]


def test_the_writer_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    write_trades(tmp_path, DAY, [trade(MINUTE, 100.0, 1.0, tid=1)])
    candles.run(tmp_path, DAY, DAY)
    directory = output_path(tmp_path, DAY).parent
    assert [p.name for p in directory.iterdir()] == [candles.OUTPUT_NAME]


def test_coins_narrows_the_run(tmp_path: Path) -> None:
    write_trades(
        tmp_path,
        DAY,
        [
            trade(MINUTE, 100.0, 1.0, tid=1, coin="BTC"),
            trade(MINUTE, 5.0, 1.0, tid=2, coin="SOL"),
        ],
    )
    candles.run(tmp_path, DAY, DAY, coins=["BTC"])
    assert read_out(tmp_path).column("coin").to_pylist() == ["BTC"]


def test_a_missing_trades_table_is_an_error_not_a_traceback(tmp_path: Path) -> None:
    from liqfade.data.ingest.common import BackfillError

    with pytest.raises(BackfillError, match="nothing to derive"):
        candles.run(tmp_path, DAY, DAY)


def test_hidden_applestyle_sidecars_are_not_read(tmp_path: Path) -> None:
    """macOS leaves a 4 KiB `._x.parquet` next to every file we write to the drive."""
    write_trades(tmp_path, DAY, [trade(MINUTE, 100.0, 1.0, tid=1)])
    directory = partition_dir(tmp_path / "parquet", "trades", DAY.isoformat())
    (directory / "._all_day.parquet").write_bytes(b"not parquet at all")
    assert candles.run(tmp_path, DAY, DAY) == 0
    assert read_out(tmp_path).num_rows == 1


# --- the API job's bars for the same day --------------------------------------


def write_api_bars(data_dir: Path, day: date, name: str = "hyperliquid_07.parquet") -> Path:
    """One `hl_api` bar for the same coin and minute the derivation produces."""
    from liqfade.data.schemas import SOURCE_HL_API, rows_to_table

    row = {"coin": "BTC", "ts_ms": MINUTE, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 0.0, "n": 0}
    directory = partition_dir(data_dir / "parquet", "candles_1m", day.isoformat())
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    write_atomic(rows_to_table("candles_1m", [row], SOURCE_HL_API), path)  # type: ignore[list-item]
    return path


def test_the_derivation_drops_the_api_bars_of_a_day_it_writes(tmp_path: Path) -> None:
    """A partition holding both sources ends up with one bar per (coin, ts_ms)."""
    from liqfade.recorder.writer import lake_files

    write_trades(tmp_path, DAY, [trade(MINUTE, 100.0, 1.0, tid=1)])
    api_file = write_api_bars(tmp_path, DAY)

    assert candles.run(tmp_path, DAY, DAY) == 0
    assert not api_file.exists()
    assert [p.name for p in lake_files(api_file.parent)] == [candles.OUTPUT_NAME]

    arrow = read_out(tmp_path)
    keys = list(
        zip(arrow.column("coin").to_pylist(), arrow.column("ts_ms").to_pylist(), strict=True)
    )
    assert keys == [("BTC", MINUTE)]


def test_a_partition_holding_both_becomes_compactable(tmp_path: Path) -> None:
    """`_hour_of("hyperliquid_day.parquet")` raises, so the mixed partition never folds."""
    from liqfade.recorder.writer import compact_day

    root = tmp_path / "parquet"
    write_trades(tmp_path, DAY, [trade(MINUTE, 100.0, 1.0, tid=1)])
    write_api_bars(tmp_path, DAY)
    write_api_bars(tmp_path, DAY, name=candles.OUTPUT_NAME)  # as the old run left it
    with pytest.raises(RuntimeError, match="cannot read the hour"):
        compact_day(root, DAY.isoformat(), tables=["candles_1m"])

    assert candles.run(tmp_path, DAY, DAY, force=True) == 0
    assert compact_day(root, DAY.isoformat(), tables=["candles_1m"]) == {}
