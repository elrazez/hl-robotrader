"""The one-off repair of the doubled Bybit ETH half-day: keep half, touch nothing else."""

from __future__ import annotations

import importlib.util
from datetime import UTC, date, datetime, time
from pathlib import Path

import pyarrow as pa
import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "research" / "drop_doubled_bybit_eth.py"


def load() -> object:
    spec = importlib.util.spec_from_file_location("drop_doubled_bybit_eth", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fix = load()

MIDNIGHT = int(datetime.combine(date(2026, 1, 1), time.min, tzinfo=UTC).timestamp() * 1000)
HOUR = 3_600_000


def trade(
    ts_ms: int, px: float, *, venue: str = "bybit", coin: str = "ETH", source: str = "tardis"
) -> dict[str, object]:
    return {
        "venue": venue,
        "coin": coin,
        "ts_ms": ts_ms,
        "rx_ms": 0,
        "source": source,
        "px": px,
        "sz": 1.0,
        "side": "buy",
        "tid": None,
    }


def as_table(rows: list[dict[str, object]]) -> pa.Table:
    return pa.Table.from_pylist(rows)


def test_half_of_every_doubled_group_survives_in_file_order() -> None:
    """Two copies in, one copy out, and the one kept is the first in file order."""
    rows = [trade(MIDNIGHT + 1, 100.0), trade(MIDNIGHT + 2, 101.0)] * 2
    mask, dropped, in_slice = fix.keep_mask(as_table(rows))

    assert (dropped, in_slice) == (2, 4)
    # Rows 0 and 1 are each group's first occurrence; 2 and 3 are the second copy.
    assert mask.to_pylist() == [True, True, False, False]


def test_a_group_of_four_keeps_two() -> None:
    rows = [trade(MIDNIGHT + 1, 100.0)] * 4
    mask, dropped, _ = fix.keep_mask(as_table(rows))
    assert dropped == 2
    assert mask.to_pylist() == [True, True, False, False]


def test_an_odd_group_aborts_rather_than_guessing() -> None:
    """An odd count means the diagnosis is wrong, and "keep half" is undefined on it.

    Dropping a row there would delete a real trade.
    """
    rows = [trade(MIDNIGHT + 1, 100.0)] * 3
    with pytest.raises(fix.OddGroupError):
        fix.keep_mask(as_table(rows))


def test_everything_outside_the_slice_is_left_alone() -> None:
    """The partition also holds Binance, OKX, other coins, and the day's second half.

    Each of these is a duplicate pair by the group key and must still survive whole: the
    defect is one venue, one coin, one source, in the first twelve hours.
    """
    rows = [
        *[trade(MIDNIGHT + 1, 100.0, venue="binance")] * 2,
        *[trade(MIDNIGHT + 1, 100.0, coin="BTC")] * 2,
        *[trade(MIDNIGHT + 1, 100.0, source="hl_s3")] * 2,
        *[trade(MIDNIGHT + 12 * HOUR, 100.0)] * 2,
        *[trade(MIDNIGHT + 12 * HOUR - 1, 100.0)] * 2,
    ]
    mask, dropped, in_slice = fix.keep_mask(as_table(rows))

    # Only the last pair, at 11:59:59.999, is inside the slice.
    assert (dropped, in_slice) == (1, 2)
    assert mask.to_pylist() == [True] * 8 + [True, False]


def test_a_partition_with_no_slice_rows_is_untouched() -> None:
    rows = [trade(MIDNIGHT + 1, 100.0, venue="okx")] * 2
    mask, dropped, in_slice = fix.keep_mask(as_table(rows))
    assert (dropped, in_slice) == (0, 0)
    assert mask.to_pylist() == [True, True]


def test_the_repair_refuses_a_partition_that_is_not_the_known_defect(tmp_path: Path) -> None:
    """The row counts are asserted against the manifest before anything is written.

    A partition that does not drop exactly 173,868 rows down to exactly 483,737 is not
    the file this script diagnosed, so it must not be rewritten on a name match alone.
    """
    from liqfade.recorder.writer import partition_dir, write_atomic

    directory = partition_dir(tmp_path / "parquet", "trades", fix.DAY)
    directory.mkdir(parents=True)
    write_atomic(as_table([trade(MIDNIGHT + 1, 100.0)] * 2), directory / "all_day.parquet")

    with pytest.raises(SystemExit, match="refusing to write"):
        fix.repair(tmp_path / "parquet", dry_run=True)


def test_the_script_will_not_delete_without_yes(capsys: pytest.CaptureFixture[str]) -> None:
    assert fix.main(["--data-dir", "/nonexistent"]) == 1
    assert "--yes" in capsys.readouterr().out
