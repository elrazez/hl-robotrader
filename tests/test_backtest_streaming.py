"""The streaming backtest: one day at a time, and only where the cascades are.

Three things are under test here and they are separable on purpose.

**The day loop never reads a range.** `test_the_engine_never_reads_more_than_one_day`
patches `polars.read_parquet` to fail outright and `polars.scan_parquet` to refuse a scan
whose files span two date partitions. That is the memory rule expressed as a test rather
than as a comment: the year run died because `load_inputs` handed one `scan_parquet` every
trades file in the window, and a rule nothing enforces is a rule that comes back.

**The window maths.** `cascade_windows` decides what fraction of the tape is replayed at
all, so it is tested directly on a grid rather than only through a run.

**Nothing moved.** The multi-day golden lake writes `days` independent copies of the
one-day fixture, so a three day run must produce exactly three times the trades, at the
same prices, as the one day run that `tests/test_backtest_golden.py` derives on paper.
The reference test at the bottom is the same claim against real data.
"""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from liqfade.backtest.data import load_day, utc_days
from liqfade.backtest.engine import RunInputs, run_backtest
from liqfade.backtest.features import (
    candidates,
    cascade_windows,
    iter_grid_rows,
    row_columns,
)
from liqfade.backtest.source import LakeSource, feature_hash
from liqfade.config.models import Settings

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import golden_lake

SettingsFactory = Callable[..., Settings]

EQUITY = 100_000.0
LAKE_DAYS = 3


@pytest.fixture(scope="module")
def day_lake(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, int, int]:
    """Three days of the golden tape in a real lake. `(data_dir, start_ms, end_ms)`."""
    root = tmp_path_factory.mktemp("day_lake")
    start_ms, end_ms = golden_lake.write_lake(root, LAKE_DAYS)
    return root, start_ms, end_ms


def _whole_window(lake: tuple[Path, int, int]) -> RunInputs:
    """The same lake as frames, so a streamed run can be checked against an unstreamed one.

    Only ever used on a fixture. The real lake does not fit in memory, which is the whole
    reason `LakeSource` exists.
    """
    data_dir, start_ms, end_ms = lake
    read = {
        table: [
            frame
            for day in utc_days(start_ms, end_ms)
            if not (frame := load_day(data_dir, table, day)).is_empty()
        ]
        for table in ("trades", "liquidations")
    }
    return RunInputs(
        trades=pl.concat(read["trades"]),
        liquidations=pl.concat(read["liquidations"]),
        start_ms=start_ms,
        end_ms=end_ms,
        starting_equity=EQUITY,
    )


@pytest.fixture
def fresh_lake(day_lake: tuple[Path, int, int], tmp_path: Path) -> tuple[Path, int, int]:
    """A private copy of the lake, for tests that write a cache into it.

    The lake itself is built once for the module because building it is the slow part;
    anything that leaves a cache behind would change what the next test sees.
    """
    _, start_ms, end_ms = day_lake
    shutil.copytree(day_lake[0], tmp_path / "lake")
    return tmp_path / "lake", start_ms, end_ms


@pytest.fixture
def settings(settings_factory: SettingsFactory) -> Settings:
    """The golden config: only the z lookback is shortened, as in the golden test."""
    return settings_factory(**{"features.z_lookback_h": 1.0})


def source(lake: tuple[Path, int, int], settings: Settings, **kwargs: object) -> LakeSource:
    data_dir, start_ms, end_ms = lake
    return LakeSource(
        data_dir,
        settings=settings,
        coins=[golden_lake.COIN],
        start_ms=start_ms,
        end_ms=end_ms,
        starting_equity=EQUITY,
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.fixture
def single_day_reads(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Fail any read of a date range, and record the days each scan touched."""
    scanned: list[str] = []
    real_scan = pl.scan_parquet

    def refuse_read(*args: object, **kwargs: object) -> pl.DataFrame:
        raise AssertionError("read_parquet: the backtest must scan lazily, one day at a time")

    def one_day_scan(source: object, *args: object, **kwargs: object) -> pl.LazyFrame:
        paths = [source] if isinstance(source, str | Path) else list(source)  # type: ignore[arg-type]
        days = {Path(path).parent.name for path in paths}
        assert len(days) <= 1, f"one scan spanned {sorted(days)}"
        scanned.extend(days)
        return real_scan(source, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pl, "read_parquet", refuse_read)
    monkeypatch.setattr(pl, "scan_parquet", one_day_scan)
    yield scanned


class TestTheLakeIsReadOneDayAtATime:
    def test_the_engine_never_reads_more_than_one_day(
        self,
        day_lake: tuple[Path, int, int],
        settings: Settings,
        single_day_reads: list[str],
    ) -> None:
        result = run_backtest(source(day_lake, settings, cache=False), settings)
        assert result.trades, "a run that read nothing would pass this test vacuously"
        assert len(set(single_day_reads)) > 1, "the fixture spans days; the run must too"


class TestARerunOnTheSameSourceDoesNotInheritItsGrid:
    """`--lagged-ms` replays one source twice. The second run must build its own features
    when it asks for different ones, and reuse them when it does not."""

    def test_a_lagged_rerun_reuses_the_grid_it_already_built(
        self, fresh_lake: tuple[Path, int, int], settings: Settings, settings_factory: Any
    ) -> None:
        """Latency is not a feature key, so the second replay reads the cache the first
        one wrote. Only the run's first day is rebuilt, because that one is never cached."""
        lake = source(fresh_lake, settings)
        run_backtest(lake, settings)
        built, cached = lake.grids_built, lake.grids_cached
        degraded = settings_factory(**{"features.z_lookback_h": 1.0, "backtest.latency_ms": 30_000})
        run_backtest(lake, degraded)
        assert lake.grids_built == built + 1, "only the uncacheable first day may rebuild"
        assert lake.grids_cached == cached + LAKE_DAYS - 1

    def test_a_rerun_with_different_features_builds_its_own(
        self, fresh_lake: tuple[Path, int, int], settings: Settings, settings_factory: Any
    ) -> None:
        lake = source(fresh_lake, settings)
        run_backtest(lake, settings)
        built = lake.grids_built
        other = settings_factory(**{"features.z_lookback_h": 2.0})
        run_backtest(lake, other)
        assert lake.grids_built == built + LAKE_DAYS, "every day must be rebuilt"


class TestTheReportSaysHowMuchWasReplayed:
    def test_the_coverage_line_names_the_share_and_the_windows(
        self, day_lake: tuple[Path, int, int], settings: Settings, tmp_path: Path
    ) -> None:
        """A reader has to be able to see that the run skipped most of the tape, and a
        run that stopped skipping has to be visible as a number rather than as a wall
        clock that quietly got worse."""
        from liqfade.backtest.report import write_report

        result = run_backtest(source(day_lake, settings, cache=False), settings)
        directory = write_report(result, settings, reports_dir=tmp_path, run_id="test")
        report = (directory / "report.md").read_text()
        summary = json.loads((directory / "summary.json").read_text())

        assert "% of the tape in" in report
        assert summary["coverage"]["windows"] == result.windows
        assert summary["coverage"]["replayed_ms"] == result.replayed_ms
        assert 0 < summary["coverage"]["replayed_frac"] < 0.5
        assert (
            summary["coverage"]["replayed_ms"] + summary["coverage"]["skipped_ms"]
            == result.end_ms - result.start_ms
        )


class TestACascadeThatStraddlesMidnight:
    """The day loop's one genuinely hard case, and the reason for the carry and the spill.

    Every window here opens at 23:45 and closes at 00:20 the next morning, so the grid
    rows the detector reads, the tape the fills come from, the funding the position accrues
    and the equity sample that prices it all sit on two sides of a day boundary.
    """

    @pytest.fixture(scope="class")
    def midnight_lake(self, tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, int, int]:
        root = tmp_path_factory.mktemp("midnight_lake")
        # 20h50m puts the three cascades of each day at 21:50, 22:50 and 23:50.
        start_ms, end_ms = golden_lake.write_lake(root, LAKE_DAYS, offset_ms=75_000_000)
        return root, start_ms, end_ms

    def test_the_cascade_at_2350_still_trades(
        self, midnight_lake: tuple[Path, int, int], settings: Settings
    ) -> None:
        """It is the last cascade of each day, so it is the one a naive day loop loses."""
        result = run_backtest(source(midnight_lake, settings, cache=False), settings)
        crossing = [
            trade
            for trade in result.trades
            if trade.entry_ms % golden_lake.DAY_MS > 84_000_000  # after 23:20
        ]
        assert crossing, "the cascade that starts at 23:50 produced no trade at all"
        assert all(trade.exit_reason == "target" for trade in crossing)

    def test_a_position_open_at_midnight_is_carried_rather_than_cut(
        self, midnight_lake: tuple[Path, int, int], settings: Settings
    ) -> None:
        """A window clipped at midnight must not flatten what it was holding.

        `window_end` is what `_close_out` stamps on a position the run ended holding. Any
        of those in the middle of the run would mean the day loop closed a position because
        the clock rolled over, which is not a decision the strategy made.
        """
        result = run_backtest(source(midnight_lake, settings, cache=False), settings)
        early = [
            trade
            for trade in result.trades
            if trade.exit_reason == "window_end" and trade.exit_ms < result.end_ms - 60_000
        ]
        assert not early, f"{len(early)} positions were closed by a day boundary"

    def test_the_carry_finds_the_same_trades_the_whole_window_would(
        self, midnight_lake: tuple[Path, int, int], settings: Settings
    ) -> None:
        """The day loop against the same tape held whole, on the hardest case for it.

        `RunInputs` builds one grid over the whole window; `LakeSource` builds a grid per
        day with the previous day carried in front of it. If the carry is short of what a
        rolling window or the 24h z-score lookback needs, the two disagree here first,
        because every cascade in this fixture sits within ten minutes of a boundary.
        """
        streamed = run_backtest(source(midnight_lake, settings, cache=False), settings)
        whole = run_backtest(_whole_window(midnight_lake), settings)
        assert whole.trades, "the baseline found nothing, so it proves nothing"
        assert [
            (trade.coin, trade.entry_ms, trade.entry_px, trade.exit_px, trade.exit_reason)
            for trade in streamed.trades
        ] == [
            (trade.coin, trade.entry_ms, trade.entry_px, trade.exit_px, trade.exit_reason)
            for trade in whole.trades
        ]


# --- the reference run --------------------------------------------------------

# The command that produced the reference, run once with the engine as it stood before
# this change, on the owner's lake:
#
#   env LIQFADE_DATA_DIR="/Volumes/<drive>/liqfade-data" \
#       LIQFADE_FEATURES__Z_ROBUST=false \
#       uv run liqfade backtest --start 2025-10-09 --end 2025-10-11 \
#           --coins BTC,ETH,SOL,HYPE
#
# Three days rather than the month the task asked for, and `z_robust` off rather than the
# shipped default, for two measured reasons. The old engine held the whole window in
# memory and `l2` is 12.6 million rows a day on these four coins, so a month needs about
# 50 GB and could not be run at all; three days peaked at 2.39 GB. And under the shipped
# `features.z_robust: true` the liquidation z-score is null on every row of real data (the
# 60s notional is zero in 82% of grid seconds, so its rolling median and therefore its MAD
# are zero), so the `exact` trigger never fires and a reference run under the default
# config would be an empty file proving nothing about fills. With the non-robust z the
# same window produces seven trades, entries and exits, stops and targets. That is a
# separate bug, recorded in docs/DECISIONS.md; it is not this change's to fix.
#
# The first attempt at this reference produced a different digest, and the difference was
# not the engine: `Settings.kill_switch_present` fails closed on any OSError from
# `os.stat`, and eight minutes of hammering the lake's USB drive produced one, which the
# risk engine recorded as a `kill_switch` block and which cost that run an entry. Re-run
# on the same commit it produced the digest below. A run whose funnel shows a
# `kill_switch` block with no KILL file on disk has hit the same thing and is not a
# reference.
#
# **Re-pinned on 2026-09-06, from seven trades to four.** The digest above it was taken
# before `Intent.edge_frac` was signed, and the reference run carried the same defect the
# 04c year run found: three of its seven trades had a target *behind* the entry, which
# `abs()` reported as edge and which `filters.min_edge_multiple` therefore let through. They
# were losses on the tick they opened. The streaming engine is still byte-identical to the
# engine it replaced; what changed is that three trades nothing should ever have taken are
# gone. A digest that had survived this change would have meant the fix did nothing.
# docs/DECISIONS.md, 2026-09-06.
REFERENCE = {
    "start": "2025-10-09",
    "end": "2025-10-11",
    "coins": "BTC,ETH,SOL,HYPE",
    "sha256": "ee18f6c2164c84e97e785439cecdd80f94d3099b98dcd5a01bd9979e93e10496",
    "trades": 4,
}


@pytest.mark.lake
class TestNothingMoved:
    def test_the_real_lake_reproduces_the_reference_trades_byte_for_byte(
        self, tmp_path: Path
    ) -> None:
        """The whole claim of this change, against real data.

        Streaming the lake, caching the grid and skipping the quiet tape are all supposed
        to be invisible: the same command must write the same `trades.parquet`, byte for
        byte, as the engine that read the whole window into memory.
        """
        import hashlib
        import os

        from liqfade.cli import main

        data_dir = os.environ.get("LIQFADE_DATA_DIR")
        if not data_dir or not (Path(data_dir) / "parquet" / "trades").is_dir():
            pytest.skip("no lake at LIQFADE_DATA_DIR")

        code = main(
            [
                "backtest",
                "--start",
                REFERENCE["start"],
                "--end",
                REFERENCE["end"],
                "--coins",
                REFERENCE["coins"],
                "--out",
                str(tmp_path),
                "--progress-file",
                "-",
            ]
        )
        assert code == 0
        (written,) = tmp_path.glob("*/trades.parquet")
        digest = hashlib.sha256(written.read_bytes()).hexdigest()
        assert pl.read_parquet(written).height == REFERENCE["trades"]
        assert digest == REFERENCE["sha256"], (
            "the streaming engine changed a number the old engine produced; "
            f"got {digest}, expected {REFERENCE['sha256']}"
        )


class TestTheCandidateSuperset:
    """`candidate_expr` must never filter away a row the detector would have acted on.

    It is the one optimisation in the streaming engine that can silently change a result:
    a row dropped here is a cascade that never happens, and nothing downstream would say
    so. The USD floor is the first condition whose threshold depends on the coin, and this
    expression cannot see which coin a row belongs to.
    """

    def grid(self) -> pl.DataFrame:
        """Three rows: a big cascade with a null z, a small one, and a quiet one."""
        return pl.DataFrame(
            {
                "coin": ["BTC", "BTC", "BTC"],
                "ts_ms": [1_000, 2_000, 3_000],
                "liq_long_usd_60s": [4_000_000.0, 5_000.0, 0.0],
                "liq_short_usd_60s": [0.0, 0.0, 0.0],
                "liq_long_usd_60s_z": [None, None, None],
                "liq_short_usd_60s_z": [None, None, None],
                "liq_imbalance_60s": [1.0, 1.0, 0.0],
                "ret_60s": [-0.01, -0.01, 0.0],
                "rv_300s": [0.002, 0.002, 0.002],
            },
            schema_overrides={"liq_long_usd_60s_z": pl.Float64, "liq_short_usd_60s_z": pl.Float64},
        )

    def test_a_null_z_and_a_notional_floor_still_finds_the_row(
        self, settings_factory: SettingsFactory
    ) -> None:
        settings = settings_factory(
            **{
                "cascade.liq_z_min": None,
                "cascade.liq_usd_min": {
                    "core": 1_000_000.0,
                    "major": 500_000.0,
                    "minor": 100_000.0,
                },
            }
        )
        found = candidates(self.grid(), settings)
        assert found.get_column("ts_ms").to_list() == [1_000]

    def test_the_most_permissive_tier_is_the_one_applied(
        self, settings_factory: SettingsFactory
    ) -> None:
        """A BTC row under the core floor but over the minor one must survive the filter.

        The expression cannot tell BTC from a minor coin, and a run whose universe held
        both would otherwise drop the minor coin's cascades.
        """
        settings = settings_factory(
            **{
                "cascade.liq_z_min": None,
                "cascade.liq_usd_min": {
                    "core": 9_000_000.0,
                    "major": 9_000_000.0,
                    "minor": 1_000.0,
                },
            }
        )
        assert candidates(self.grid(), settings).get_column("ts_ms").to_list() == [1_000, 2_000]

    def test_a_null_floor_leaves_the_z_score_expression_alone(
        self, settings_factory: SettingsFactory
    ) -> None:
        """The shipped default. Every z here is null, so nothing is a candidate."""
        assert candidates(self.grid(), settings_factory()).is_empty()


class TestTheRowMapping:
    """Grid column names to `FeatureRow` field names, the one place they meet.

    A column the grid computes and `_row_at` forgets is a feature the strategy can never
    read, and nothing else in the stack would say so: the field simply stays None and every
    rule that consults it quietly fails.
    """

    def test_the_open_interest_and_vwap_columns_reach_the_row(
        self, settings_factory: SettingsFactory
    ) -> None:
        settings = settings_factory()
        columns = row_columns(settings)
        for name in (
            "open_interest",
            "oi_delta_60s",
            "oi_delta_300s",
            "oi_delta_3600s",
            "disp_300s_bps",
            "disp_day_bps",
        ):
            assert name in columns, f"{name} is computed but never read"

        values: dict[str, list[object]] = {name: [None] for name in columns}
        values |= {
            "mid": [100.0],
            "open_interest": [1_000_000.0],
            "oi_delta_60s": [-0.004],
            "oi_delta_300s": [-0.03],
            "oi_delta_3600s": [0.07],
            "disp_300s_bps": [-45.0],
            "disp_day_bps": [-120.0],
        }
        grid = pl.DataFrame({"coin": ["BTC"], "ts_ms": [1_000], **values})
        keep = pl.Series("_needed", [True])
        ((_ts, batch),) = list(iter_grid_rows(grid, settings, keep=keep))
        row = batch["BTC"]
        assert row.open_interest == 1_000_000.0
        assert row.oi_delta_w == -0.004
        assert row.oi_delta_rv == -0.03
        assert row.oi_delta_build == 0.07
        assert row.disp_anchor_bps == -45.0
        assert row.disp_day_bps == -120.0
        # The resolved windows, so `explain()` can name them in seconds.
        assert (row.oi_build_window_s, row.vwap_anchor_s) == (3600, 300)


class TestTheWindowMaths:
    def test_a_window_opens_before_the_candidate_and_closes_past_the_time_stop(
        self, settings: Settings
    ) -> None:
        """The bound the flat-outside-a-window assertion rests on, stated once."""
        found = pl.DataFrame({"coin": ["GOLD"], "ts_ms": [1_000_000]})
        ((start, end),) = cascade_windows(found, settings)["GOLD"]
        assert start == 1_000_000 - int(settings.backtest.window_warmup_s * 1000)
        assert end == 1_000_000 + int(
            (
                settings.exhaustion.max_age_s
                + settings.intent.time_stop_s
                + settings.backtest.window_margin_s
            )
            * 1000
        )

    def test_overlapping_windows_become_one(self, settings: Settings) -> None:
        """Replaying an overlap twice would apply the same prints to the same book twice."""
        found = pl.DataFrame({"coin": ["GOLD", "GOLD"], "ts_ms": [1_000_000, 1_060_000]})
        assert len(cascade_windows(found, settings)["GOLD"]) == 1

    def test_windows_far_apart_stay_apart(self, settings: Settings) -> None:
        span = int(
            (
                settings.exhaustion.max_age_s
                + settings.intent.time_stop_s
                + settings.backtest.window_margin_s
                + settings.backtest.window_warmup_s
            )
            * 1000
        )
        found = pl.DataFrame({"coin": ["GOLD", "GOLD"], "ts_ms": [1_000_000, 1_000_000 + span + 1]})
        assert len(cascade_windows(found, settings)["GOLD"]) == 2

    def test_each_coin_gets_its_own_windows(self, settings: Settings) -> None:
        """A BTC cascade must not drag every other coin's tape into the replay."""
        found = pl.DataFrame({"coin": ["BTC", "ETH"], "ts_ms": [1_000_000, 9_000_000]})
        windows = cascade_windows(found, settings)
        assert set(windows) == {"BTC", "ETH"}
        assert windows["BTC"] != windows["ETH"]

    def test_the_run_replays_far_less_than_the_whole_clock(
        self, day_lake: tuple[Path, int, int], settings: Settings
    ) -> None:
        """The claim the report prints. Three cascades a day at half an hour apiece
        cannot add up to a day, and if this ever reached 100% the engine would be reading
        the whole tape again with extra steps."""
        result = run_backtest(source(day_lake, settings, cache=False), settings)
        _, start_ms, end_ms = day_lake
        assert 0 < result.replayed_ms < (end_ms - start_ms) * 0.5
        assert result.windows >= LAKE_DAYS


class TestTheGridCache:
    def test_a_second_run_reads_the_cache_and_builds_nothing(
        self, fresh_lake: tuple[Path, int, int], settings: Settings
    ) -> None:
        first = source(fresh_lake, settings)
        cold = run_backtest(first, settings)
        assert first.grids_built > 0

        second = source(fresh_lake, settings)
        warm = run_backtest(second, settings)
        # The run's first day is deliberately never cached: it has no previous day inside
        # the window, so its lookbacks are cold and its grid is not a function of the date.
        assert second.grids_built == 1
        assert second.grids_cached > 0
        assert [trade.net_pnl for trade in warm.trades] == [trade.net_pnl for trade in cold.trades]

    def test_no_cache_rebuilds_every_day_and_writes_nothing(
        self, fresh_lake: tuple[Path, int, int], settings: Settings
    ) -> None:
        data_dir, _, _ = fresh_lake
        run_backtest(source(fresh_lake, settings, cache=False), settings)
        assert not (data_dir / "cache").exists()

    def test_a_changed_stop_reuses_the_cache_and_a_changed_feature_does_not(
        self, settings_factory: SettingsFactory
    ) -> None:
        """The whole point of the hash: a sweep over the trade's shape is free, a change
        to what a feature column means is not."""
        base = settings_factory(**{"features.z_lookback_h": 1.0})
        for key, value in (
            ("intent.k_stop", 2.0),
            ("intent.time_stop_s", 600),
            ("sizing.risk_frac", 0.001),
            ("risk.max_dd_frac", 0.5),
            # Read by active_mask and cascade_windows, both of which run in the engine on
            # a grid that is already built. See the test below.
            ("exhaustion.max_age_s", 600.0),
        ):
            same = settings_factory(**{"features.z_lookback_h": 1.0, key: value})
            assert feature_hash(same) == feature_hash(base), f"{key} must not invalidate"
        for key, value in (
            ("features.z_lookback_h", 2.0),
            ("features.depth_bps", 20.0),
            ("cascade.liq_z_min", 2.0),
            ("cascade.trigger", "proxy"),
            ("backtest.feature_grid_ms", 250),
            ("sizing.vol_window_s", 600),
        ):
            other = settings_factory(**{"features.z_lookback_h": 1.0, key: value})
            assert feature_hash(other) != feature_hash(base), f"{key} must invalidate"

    def test_a_cache_written_for_fewer_coins_is_not_trusted_for_more(
        self, fresh_lake: tuple[Path, int, int], settings: Settings
    ) -> None:
        """A partial answer would silently drop the coins it never covered from the run.

        A coin with no prints leaves no rows, so "covered and empty" cannot be told from
        "never asked about" by looking at the rows; the coin list is stamped on the file.
        """
        data_dir, start_ms, end_ms = fresh_lake
        run_backtest(source(fresh_lake, settings), settings)
        wider = LakeSource(
            data_dir,
            settings=settings,
            coins=[golden_lake.COIN, "NOTINTHELAKE"],
            start_ms=start_ms,
            end_ms=end_ms,
            starting_equity=EQUITY,
        )
        run_backtest(wider, settings)
        assert wider.grids_cached == 0, "a narrower cache must not answer for a wider run"


class TestTheCarryIsAsWideAsTheFeatureThatReadsIt:
    """A day's grid is a function of the date, on a lake that has a book.

    `LakeSource._build` puts the previous day in front of the day being scored so that
    every rolling window is full. For the tape and the liquidations it reads that day
    whole; for the book tables it used to read only the last snapshot, on the argument that
    a book reaches the grid through a backward as-of join and nothing else. That argument
    holds for `l2`, whose four columns are joined onto the grid and never windowed, and it
    is false for `bbo`: `_with_mid` makes `mid` the last book midpoint, and `mid` is what
    `returns`, `realized_vol`, `cvd`, `displacement`, `session_vwap` and the 24h mark-mid
    percentile are all computed from. With only the last snapshot carried, the previous
    day's rows fall back to the last trade price, and every window on the scored day that
    reaches back across midnight reads a different series.

    The check is the definition: build day two through the streaming source, build it as
    part of one two-day window, and require every column to agree.
    """

    @pytest.fixture(scope="class")
    def book_lake(self, tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, int, int]:
        """Two days of the golden tape **with a quote tape**, which is what a lake has."""
        root = tmp_path_factory.mktemp("book_lake")
        # The same 20h50m offset the straddle fixture uses, so each day's tape runs up to
        # and across midnight. At the default offset the tape stops at 03:20 and every
        # window on the second day reaches back only into its own quiet hours, where a
        # carry of one snapshot and a carry of the whole day say the same thing.
        start_ms, end_ms = golden_lake.write_lake(root, 2, offset_ms=75_000_000, bbo=True)
        return root, start_ms, end_ms

    def _whole_window_grid(self, lake: tuple[Path, int, int], settings: Settings) -> pl.DataFrame:
        """The second day's grid, built from both days in one pass. The answer to match."""
        from liqfade.backtest.features import build_feature_grid
        from liqfade.backtest.source import DAY_MS, midnight_ms

        data_dir, start_ms, end_ms = lake
        days = utc_days(start_ms, end_ms)
        read = {
            table: pl.concat(
                frames
                if (
                    frames := [
                        f for day in days if not (f := load_day(data_dir, table, day)).is_empty()
                    ]
                )
                else [pl.DataFrame()]
            )
            for table in ("trades", "liquidations", "bbo")
        }
        second = midnight_ms(days[1])
        grid = build_feature_grid(
            settings=settings,
            trades=read["trades"],
            liquidations=read["liquidations"],
            start_ms=midnight_ms(days[0]),
            end_ms=second + DAY_MS,
            bbo=read["bbo"],
        )
        wanted = ("coin", "ts_ms", *row_columns(settings))
        keep = [name for name in wanted if name in grid.columns]
        return grid.filter(pl.col("ts_ms") >= second).select(keep)

    def test_the_streamed_grid_equals_the_whole_window_grid_on_every_column(
        self, book_lake: tuple[Path, int, int], settings: Settings
    ) -> None:
        from liqfade.backtest.source import LakeSource

        data_dir, start_ms, end_ms = book_lake
        source = LakeSource(
            data_dir,
            settings=settings,
            coins=[golden_lake.COIN],
            start_ms=start_ms,
            end_ms=end_ms,
            cache=False,
        )
        second = source.days()[1]
        ((_, streamed),) = source.grids(second, settings, first=False)
        expected = self._whole_window_grid(book_lake, settings)

        assert streamed.columns == expected.columns
        assert streamed.height == expected.height
        differing = [
            name
            for name in streamed.columns
            if not streamed.get_column(name).equals(expected.get_column(name))
        ]
        assert differing == [], (
            "the carried previous day is narrower than the windows that read it: "
            f"{differing} differ between the streamed and the whole-window grid"
        )


class TestTheWarmupReachesBackAcrossMidnight:
    """A window is `[cascade_start - window_warmup_s, ...]`, on both sides of a boundary.

    The day loop clamped every window's start to the day it belonged to, so a candidate
    inside the first `window_warmup_s` of a day got whatever warmup was left after midnight
    instead of the 300s the spec gives it. The book, the tape and `risk.stale_coin_s` are
    all colder than a whole-lake run would have made them, and within `stale_coin_s` of
    00:00 the entry is blocked for staleness the whole-window run never saw.

    The floor is the end of what the previous day already replayed, not midnight. That is
    what keeps the reach from feeding the same prints to the book twice: tape the previous
    day covered is behind the floor, and a window still open at midnight leaves the floor
    exactly at midnight, which is where a spill picks up.
    """

    def test_a_window_opening_before_midnight_keeps_its_reach(self) -> None:
        from liqfade.backtest.engine import _clip_to_day
        from liqfade.backtest.source import DAY_MS, Day, midnight_ms

        midnight = midnight_ms("2026-03-02")
        day = Day(date="2026-03-02", start_ms=midnight, end_ms=midnight + DAY_MS)
        # A candidate at 00:05 with the default 300s warmup: the window opens at 23:55.
        spans = {"GOLD": [(midnight - 300_000, midnight + 900_000)]}
        today, spill = _clip_to_day(spans, day, floor_ms=midnight - DAY_MS)
        assert today == {"GOLD": [(midnight - 300_000, midnight + 900_000)]}
        assert spill == {}

    def test_the_reach_stops_where_the_previous_day_stopped_replaying(self) -> None:
        """Tape the previous day already fed to the book is not fed to it again."""
        from liqfade.backtest.engine import _clip_to_day
        from liqfade.backtest.source import DAY_MS, Day, midnight_ms

        midnight = midnight_ms("2026-03-02")
        day = Day(date="2026-03-02", start_ms=midnight, end_ms=midnight + DAY_MS)
        spans = {"GOLD": [(midnight - 300_000, midnight + 900_000)]}
        # Yesterday replayed up to 23:58, so only the last two minutes are new.
        today, _ = _clip_to_day(spans, day, floor_ms=midnight - 120_000)
        assert today == {"GOLD": [(midnight - 120_000, midnight + 900_000)]}

    def test_a_window_still_open_at_midnight_leaves_no_room_to_reach_back(self) -> None:
        """The spill case. Yesterday replayed right up to 00:00, so the floor is 00:00."""
        from liqfade.backtest.engine import _clip_to_day
        from liqfade.backtest.source import DAY_MS, Day, midnight_ms

        midnight = midnight_ms("2026-03-02")
        day = Day(date="2026-03-02", start_ms=midnight, end_ms=midnight + DAY_MS)
        spans = {"GOLD": [(midnight - 300_000, midnight + 900_000)]}
        today, _ = _clip_to_day(spans, day, floor_ms=midnight)
        assert today == {"GOLD": [(midnight, midnight + 900_000)]}

    @pytest.fixture(scope="class")
    def after_midnight_lake(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> tuple[Path, int, int]:
        """A lake whose first cascade of each copy lands at 00:05, five minutes in.

        23h05m puts the tape's own start at 23:05 and its three cascades at 00:05, 01:05
        and 02:05 of the next morning, so the first one sits inside `window_warmup_s` of
        midnight and its window has to open on the previous partition.
        """
        root = tmp_path_factory.mktemp("after_midnight_lake")
        start_ms, end_ms = golden_lake.write_lake(root, LAKE_DAYS, offset_ms=83_100_000)
        return root, start_ms, end_ms

    def test_a_window_reaching_back_reads_the_previous_partition(
        self, after_midnight_lake: tuple[Path, int, int], settings: Settings
    ) -> None:
        """The tape before midnight is in a different file, and the source has to open it."""
        lake = source(after_midnight_lake, settings, cache=False)
        second = lake.days()[1]
        spans = {golden_lake.COIN: [(second.start_ms - 300_000, second.start_ms + 900_000)]}
        stamps = [event.ts_ms for event in lake.events(second, spans)]
        assert stamps, "the window read nothing at all"
        assert min(stamps) < second.start_ms, (
            "every event came from the scored day; the warmup never crossed midnight"
        )
        assert min(stamps) >= second.start_ms - 300_000, (
            "the previous partition was read wider than the window asked for"
        )

    def test_the_backward_reach_finds_the_same_trades_the_whole_window_would(
        self, after_midnight_lake: tuple[Path, int, int], settings: Settings
    ) -> None:
        """The day loop against the same tape held whole, straddling midnight forwards."""
        streamed = run_backtest(source(after_midnight_lake, settings, cache=False), settings)
        whole = run_backtest(_whole_window(after_midnight_lake), settings)
        assert whole.trades, "the baseline found nothing, so it proves nothing"
        assert [
            (trade.coin, trade.entry_ms, trade.entry_px, trade.exit_px, trade.exit_reason)
            for trade in streamed.trades
        ] == [
            (trade.coin, trade.entry_ms, trade.entry_px, trade.exit_px, trade.exit_reason)
            for trade in whole.trades
        ]


class TestTheCacheKeyCoversTheColumnsAndNoMore:
    """A key in `HASHED_KEYS` that changes no cached column throws grids away for nothing.

    `exhaustion.max_age_s` was in the list because `active_mask` reads it. `active_mask`
    runs in the engine, on a grid that is already built and already cached, so the cached
    day is the same under any value of it: what the key changes is how much of that day the
    engine keeps, which is decided after the read. A year of four coins is 6.8 GB of cache
    and a sweep over the exhaustion window was rebuilding all of it.
    """

    def test_the_built_grid_is_the_same_under_a_different_max_age(
        self, day_lake: tuple[Path, int, int], settings_factory: SettingsFactory
    ) -> None:
        from liqfade.backtest.source import LakeSource

        data_dir, start_ms, end_ms = day_lake
        grids = []
        for max_age in (settings_factory().exhaustion.max_age_s, 600.0):
            tuned = settings_factory(
                **{"features.z_lookback_h": 1.0, "exhaustion.max_age_s": max_age}
            )
            lake = LakeSource(
                data_dir,
                settings=tuned,
                coins=[golden_lake.COIN],
                start_ms=start_ms,
                end_ms=end_ms,
                cache=False,
            )
            second = lake.days()[1]
            ((_, grid),) = lake.grids(second, tuned, first=False)
            grids.append(grid)
        assert grids[0].equals(grids[1]), (
            "exhaustion.max_age_s changed a built grid, so it belongs in HASHED_KEYS"
        )
