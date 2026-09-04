"""Open interest computed from `start_position`, over five hand-built fills.

Five fills is enough to cover every case that matters: a fresh long opened, a long closed,
a short opened, a position crossing through zero, and a spot row that must not count.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import duckdb
import pytest

from liqfade.data.schemas import SOURCE_HL_S3, rows_to_table
from liqfade.recorder.writer import partition_dir, write_atomic

SCRIPT = Path(__file__).parents[1] / "scripts" / "research" / "oi_from_fills.py"
DAY = "2026-03-01"
MINUTE = 1_772_323_200_000


def load() -> object:
    spec = importlib.util.spec_from_file_location("oi_from_fills", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


oi = load()


def fill(coin: str, side: str, sz: float, start: float, ts_ms: int = MINUTE) -> dict[str, object]:
    return {
        "coin": coin,
        "ts_ms": ts_ms,
        "rx_ms": 0,
        "block_time": ts_ms,
        "block_number": 1,
        "local_time": ts_ms,
        "user_address": "0x" + "1" * 40,
        "px": 100.0,
        "sz": sz,
        "side": side,
        "start_position": start,
        "dir": "",
        "closed_pnl": 0.0,
        "hash": "0x0",
        "oid": 1,
        "crossed": True,
        "fee": 0.0,
        "fee_token": "USDC",
        "tid": 1,
        "cloid": None,
        "builder": None,
        "builder_fee": None,
        "twap_id": None,
        "liq_user": None,
        "liq_mark_px": None,
        "liq_method": None,
    }


def write_fills(tmp_path: Path, rows: list[dict[str, object]]) -> list[str]:
    directory = partition_dir(tmp_path / "parquet", "fills_full", DAY)
    directory.mkdir(parents=True, exist_ok=True)
    write_atomic(rows_to_table("fills_full", rows, SOURCE_HL_S3), directory / "all_day.parquet")
    return oi.files_for(tmp_path / "parquet", "fills_full", DAY)


def deltas(tmp_path: Path, rows: list[dict[str, object]]) -> dict[tuple[str, int], float]:
    files = write_fills(tmp_path, rows)
    found = oi.oi_deltas(duckdb.connect(), files, None).fetchall()
    return {(coin, minute): round(delta, 9) for coin, minute, delta, _long, _fills in found}


def test_five_fills_give_the_exact_change_in_open_interest(tmp_path: Path) -> None:
    """Both sides count: an account contributes `abs(position)`, long or short.

    A long-exposure-only sum would give 3.5 here and disagree with the exchange by a
    factor of two; measured against the archive, the ratio is exactly 2.
    """
    rows = [
        fill("BTC", "B", 2.0, 0.0),  # opens a 2 long: +2
        fill("BTC", "A", 0.5, 2.0),  # closes half of it: -0.5
        fill("BTC", "A", 3.0, 0.0),  # opens a 3 short, which counts too: +3
        fill("BTC", "B", 5.0, -3.0),  # a 3 short to a 2 long, crossing zero: -1
        fill("@107", "B", 9.0, 0.0),  # spot: no open interest, and not a position
    ]
    assert deltas(tmp_path, rows) == {("BTC", MINUTE): 3.5}


def test_a_short_opened_raises_open_interest_as_much_as_a_long(tmp_path: Path) -> None:
    """The case a long-only sum reads as zero."""
    assert deltas(tmp_path, [fill("BTC", "A", 3.0, 0.0)]) == {("BTC", MINUTE): 3.0}


def test_each_minute_and_coin_is_counted_on_its_own(tmp_path: Path) -> None:
    rows = [
        fill("BTC", "B", 2.0, 0.0),
        fill("BTC", "A", 1.0, 2.0, ts_ms=MINUTE + 60_000),
        fill("ETH", "B", 7.0, 0.0),
    ]
    assert deltas(tmp_path, rows) == {
        ("BTC", MINUTE): 2.0,
        ("BTC", MINUTE + 60_000): -1.0,
        ("ETH", MINUTE): 7.0,
    }


def test_a_fill_with_no_start_position_is_left_out_rather_than_read_as_zero(
    tmp_path: Path,
) -> None:
    """A null there is an unknown position, and treating it as flat invents exposure."""
    rows = [fill("BTC", "B", 2.0, 0.0), fill("BTC", "B", 100.0, 0.0) | {"start_position": None}]
    assert deltas(tmp_path, rows) == {("BTC", MINUTE): 2.0}


def test_both_halves_of_a_trade_count_because_they_are_two_accounts(tmp_path: Path) -> None:
    """A taker opening a 2 long against a maker opening the matching 2 short is +4.

    Two accounts, `abs(position)` each, and Hyperliquid counts both sides. Summing over
    every fill is summing over every account, which is what makes this exact rather than
    an estimate.
    """
    taker = fill("BTC", "B", 2.0, 0.0)
    maker = fill("BTC", "A", 2.0, 0.0) | {"user_address": "0x" + "2" * 40, "crossed": False}
    assert deltas(tmp_path, [taker, maker]) == {("BTC", MINUTE): 4.0}


def write_archive(tmp_path: Path, coin: str, open_interest: list[float]) -> list[str]:
    """An `asset_ctx` day: one sample a minute, as the archive writes it."""
    from liqfade.data.schemas import SOURCE_HL_ARCHIVE

    rows = [
        {"coin": coin, "ts_ms": MINUTE + index * 60_000, "rx_ms": 0, "open_interest": value}
        for index, value in enumerate(open_interest)
    ]
    directory = partition_dir(tmp_path / "parquet", "asset_ctx", DAY)
    directory.mkdir(parents=True, exist_ok=True)
    write_atomic(
        rows_to_table("asset_ctx", rows, SOURCE_HL_ARCHIVE),  # type: ignore[arg-type]
        directory / "hyperliquid_day.parquet",
    )
    return oi.files_for(tmp_path / "parquet", "asset_ctx", DAY)


def test_the_archive_difference_is_labelled_with_the_minute_it_happened_in(
    tmp_path: Path,
) -> None:
    """The off-by-one that reads as a real disagreement.

    The archive stamps a sample with the instant it was taken, so `OI(M+1) - OI(M)` is
    the change during minute M, not minute M+1. Labelling it M+1 pairs every minute with
    its neighbour: measured on 2026-08-28, that is correlation 0.08 to 0.32 where the
    correct alignment is exactly 1.
    """
    files = write_archive(tmp_path, "BTC", [10.0, 12.0, 11.5])
    found = duckdb.connect().execute(oi.ARCHIVE_QUERY, {"files": files, "coins": None}).fetchall()
    assert [(row[1], round(row[2], 9)) for row in found] == [
        (MINUTE, 2.0),  # the rise from the first sample to the second happened in minute 0
        (MINUTE + 60_000, -0.5),
    ]


def test_the_two_sides_agree_when_the_fills_explain_the_archive(tmp_path: Path) -> None:
    """End to end: the alignment, the both-sides formula and the gate together.

    Fills come in maker and taker pairs, which is why the ratio is 2 in every minute and
    not only on average: each trade puts one account's `abs(position)` up by the size on
    each side of it.
    """
    other = "0x" + "2" * 40
    write_fills(
        tmp_path,
        [
            # minute 0: a 2 long opened against a 2 short. Both sides +4, long only +2.
            fill("BTC", "B", 2.0, 0.0),
            fill("BTC", "A", 2.0, 0.0) | {"user_address": other, "crossed": False},
            # minute 1: half of each closed. Both sides -1, long only -0.5.
            fill("BTC", "A", 0.5, 2.0, ts_ms=MINUTE + 60_000),
            fill("BTC", "B", 0.5, -2.0, ts_ms=MINUTE + 60_000)
            | {"user_address": other, "crossed": False},
        ],
    )
    write_archive(tmp_path, "BTC", [10.0, 14.0, 13.0])
    code = oi.main([DAY, "--data-dir", str(tmp_path), "--reports-dir", str(tmp_path / "reports")])
    assert code == 0

    text = (tmp_path / "reports" / "data_quality" / f"oi_from_fills_{DAY}.md").read_text()
    assert "**Gate: PASS.**" in text
    assert "| BTC | 2 | 1.000000 | 2.000000 | 0 | 0 |" in text


def test_a_one_sided_open_interest_fails_the_gate_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The failure that hid the first time: still correlation 1, every number halved.

    If Hyperliquid ever redefines `open_interest` to one side, the script has to say so
    rather than report a clean-looking comparison against a quantity that moved.
    """
    write_fills(
        tmp_path,
        [
            fill("BTC", "B", 2.0, 0.0),
            fill("BTC", "A", 2.0, 0.0) | {"user_address": "0x" + "2" * 40},
            fill("BTC", "A", 1.0, 2.0, ts_ms=MINUTE + 60_000),
        ],
    )
    # The fills move both-sides open interest by +4 then -1, and long-only by +2 then -1.
    # An archive that moved by the long-only amounts is a one-sided definition.
    write_archive(tmp_path, "BTC", [10.0, 12.0, 11.0])
    code = oi.main([DAY, "--data-dir", str(tmp_path), "--reports-dir", str(tmp_path / "reports")])

    assert code == 1
    assert "counting 1.0 sides of the book, not 2.0" in capsys.readouterr().err
    text = (tmp_path / "reports" / "data_quality" / f"oi_from_fills_{DAY}.md").read_text()
    assert "**Gate: FAIL.**" in text


def test_a_missing_archive_partition_says_what_to_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_fills(tmp_path, [fill("BTC", "B", 1.0, 0.0)])
    code = oi.main([DAY, "--data-dir", str(tmp_path), "--reports-dir", str(tmp_path / "reports")])
    assert code == 1
    assert "run `liqfade backfill asset-ctxs` first" in capsys.readouterr().err
