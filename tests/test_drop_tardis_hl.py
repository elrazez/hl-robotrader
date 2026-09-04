"""The one-off sweep that removes Tardis's Hyperliquid rows from the canonical tables."""

from __future__ import annotations

import importlib.util
from datetime import UTC, date, datetime
from datetime import time as dt_time
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from liqfade.data.ingest.common import write_partitioned
from liqfade.data.schemas import SOURCE_HL_S3, SOURCE_TARDIS
from liqfade.recorder.writer import partition_dir, write_atomic

SCRIPT = Path(__file__).parents[1] / "scripts" / "research" / "drop_tardis_hl.py"
DAY = date(2026, 9, 1)
NOON_MS = int(datetime.combine(DAY, dt_time(12), tzinfo=UTC).timestamp() * 1000)


def load() -> object:
    spec = importlib.util.spec_from_file_location("drop_tardis_hl", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


drop = load()


def trade(venue: str, tid: int) -> dict[str, object]:
    return {
        "venue": venue,
        "coin": "BTC",
        "ts_ms": NOON_MS,
        "rx_ms": 0,
        "px": 78_000.0,
        "sz": 1.0,
        "side": "buy",
        "tid": tid,
    }


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    """One `trades` day carrying both sources, as a Tardis free day does."""
    root = tmp_path / "parquet"
    write_partitioned(root, "trades", "hyperliquid", SOURCE_HL_S3, [trade("hyperliquid", 1)])
    write_partitioned(
        root,
        "trades",
        "tardis",
        SOURCE_TARDIS,
        [trade("hyperliquid", 1), trade("binance", 2), trade("okx", 3)],
    )
    return root


def sources_and_venues(root: Path) -> list[tuple[str, str]]:
    from liqfade.recorder.writer import lake_files

    pairs = []
    for path in sorted(lake_files(partition_dir(root, "trades", DAY.isoformat()))):
        table = pq.read_table(path, partitioning=None)
        pairs += list(
            zip(table.column("source").to_pylist(), table.column("venue").to_pylist(), strict=True)
        )
    return sorted(pairs)


def test_only_the_tardis_hyperliquid_rows_go(lake: Path) -> None:
    found = drop.sweep(lake, DAY, DAY, dry_run=False)
    assert found == [{"day": DAY.isoformat(), "table": "trades", "before": 4, "dropped": 1}]
    assert sources_and_venues(lake) == [
        (SOURCE_HL_S3, "hyperliquid"),
        (SOURCE_TARDIS, "binance"),
        (SOURCE_TARDIS, "okx"),
    ]


def test_a_dry_run_reports_the_same_and_changes_nothing(lake: Path) -> None:
    before = sources_and_venues(lake)
    found = drop.sweep(lake, DAY, DAY, dry_run=True)
    assert found[0]["dropped"] == 1
    assert sources_and_venues(lake) == before


def test_a_second_sweep_is_a_no_op(lake: Path) -> None:
    drop.sweep(lake, DAY, DAY, dry_run=False)
    assert drop.sweep(lake, DAY, DAY, dry_run=False) == []


def test_the_rederive_stamp_survives_the_rewrite(tmp_path: Path) -> None:
    """A stamped partition must stay stamped, or the next rederive redoes the day."""
    from liqfade.data.ingest.hl_s3 import REDERIVE_STAMP
    from liqfade.data.schemas import rows_to_table

    root = tmp_path / "parquet"
    directory = partition_dir(root, "trades", DAY.isoformat())
    directory.mkdir(parents=True)
    table = rows_to_table("trades", [trade("hyperliquid", 1)], SOURCE_TARDIS)  # type: ignore[arg-type]
    write_atomic(
        table.replace_schema_metadata(dict([REDERIVE_STAMP])), directory / "all_day.parquet"
    )

    drop.sweep(root, DAY, DAY, dry_run=False)
    meta = pq.read_metadata(directory / "all_day.parquet").metadata or {}
    assert meta.get(REDERIVE_STAMP[0]) == REDERIVE_STAMP[1]
    assert pq.read_metadata(directory / "all_day.parquet").num_rows == 0


def test_it_refuses_to_delete_without_yes(lake: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["--data-dir", str(lake.parent), "--start", str(DAY), "--end", str(DAY)]
    assert drop.main(argv) == 1
    assert "--yes to do it" in capsys.readouterr().out
    assert drop.main([*argv, "--yes"]) == 0
    assert "1 partitions, 1 rows dropped" in capsys.readouterr().out
