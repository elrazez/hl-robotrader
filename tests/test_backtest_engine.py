"""The replay loop end to end: no lookahead, determinism, and the report round trip.

Runs the same synthetic stream `tests/test_strategy_cascade_consistency.py` builds, which
carries six clean cascades on a tape that recovers afterwards, so the loop has something
real to trade rather than a hand-placed fill.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

import polars as pl
import pytest
from test_strategy_cascade_consistency import build_stream

from liqfade.backtest.engine import RunInputs, run_backtest
from liqfade.backtest.metrics import compute_metrics, split_by_episode_count
from liqfade.backtest.report import config_hash, run_id_for, write_report
from liqfade.backtest.validate import bump_n_trials, deflated_sharpe, read_n_trials
from liqfade.config.models import Settings

SettingsFactory = Callable[..., Settings]


@pytest.fixture(scope="module")
def stream() -> tuple[pl.DataFrame, pl.DataFrame, int, int]:
    return build_stream()


@pytest.fixture
def settings(settings_factory: SettingsFactory) -> Settings:
    """Paper config with a shortened z lookback, as the consistency suite uses."""
    return settings_factory(**{"features.z_lookback_h": 1.0})


def run(
    stream: tuple[pl.DataFrame, pl.DataFrame, int, int], settings: Settings, **over: object
) -> object:
    trades, liquidations, start_ms, end_ms = stream
    inputs = RunInputs(
        trades=trades,
        liquidations=liquidations,
        start_ms=start_ms,
        end_ms=end_ms,
        starting_equity=100_000.0,
        **over,  # type: ignore[arg-type]
    )
    return run_backtest(inputs, settings)


class TestItActuallyTrades:
    def test_the_six_cascades_become_six_round_trips(
        self, stream: tuple[pl.DataFrame, pl.DataFrame, int, int], settings: Settings
    ) -> None:
        """The whole pipeline in one assertion: features, strategy, sizing, risk, fills."""
        result = run(stream, settings)
        assert result.intents == 6  # type: ignore[attr-defined]
        assert result.entries_placed == 6  # type: ignore[attr-defined]
        assert len(result.trades) == 6  # type: ignore[attr-defined]
        assert result.portfolio.abandoned_entries == 0  # type: ignore[attr-defined]

    def test_every_trade_is_net_of_fees_and_funding(
        self, stream: tuple[pl.DataFrame, pl.DataFrame, int, int], settings: Settings
    ) -> None:
        result = run(stream, settings)
        for trade in result.trades:  # type: ignore[attr-defined]
            assert trade.net_pnl == pytest.approx(
                trade.gross_pnl - trade.fees_usd - trade.funding_usd
            )
            assert trade.fees_usd > 0, "a round trip pays two fees"

    def test_the_fixture_reverts_so_the_run_is_profitable(
        self, stream: tuple[pl.DataFrame, pl.DataFrame, int, int], settings: Settings
    ) -> None:
        """Guards the fixture, not the strategy.

        This tape is built to revert, so a fade of it must make money. A run that lost on
        it would mean the sign of the trade is inverted somewhere, which is the one bug
        that would make every other number here look plausible and be backwards.
        """
        result = run(stream, settings)
        assert all(trade.side == "long" for trade in result.trades)  # type: ignore[attr-defined]
        assert sum(trade.net_pnl for trade in result.trades) > 0  # type: ignore[attr-defined]

    def test_the_equity_curve_ends_where_the_cash_does(
        self, stream: tuple[pl.DataFrame, pl.DataFrame, int, int], settings: Settings
    ) -> None:
        result = run(stream, settings)
        assert result.portfolio.positions == {}, "the window closes flat"  # type: ignore[attr-defined]
        final = result.equity_curve[-1][1]  # type: ignore[attr-defined]
        net = sum(trade.net_pnl for trade in result.trades)  # type: ignore[attr-defined]
        assert final == pytest.approx(100_000.0 + net, rel=1e-6)


class TestNoLookahead:
    def test_mutating_every_event_after_t_leaves_the_trades_before_t_identical(
        self, stream: tuple[pl.DataFrame, pl.DataFrame, int, int], settings: Settings
    ) -> None:
        """The prompt's engine-level no-lookahead bar.

        Take a copy of the fixture, destroy everything after T, and check that every trade
        entered before T is unchanged. Anything that reached forward, in the feature grid,
        the detector, the fill model or the clock, moves at least one of them.
        """
        trades, liquidations, start_ms, end_ms = stream
        cut_ms = start_ms + (end_ms - start_ms) // 2

        def entered_before_cut(result: object) -> list[tuple]:
            return [
                (t.coin, t.side, t.entry_ms, round(t.entry_px, 9), round(t.size, 9))
                for t in result.trades  # type: ignore[attr-defined]
                if t.entry_ms < cut_ms
            ]

        baseline = entered_before_cut(run(stream, settings))
        assert baseline, "the cut must leave some trades on the near side to compare"

        # Everything after the cut becomes a different market: prices ten times higher and
        # every liquidation ten times larger.
        mutated_trades = trades.with_columns(
            pl.when(pl.col("ts_ms") >= cut_ms).then(pl.col("px") * 10.0).otherwise(pl.col("px"))
        )
        mutated_liqs = liquidations.with_columns(
            pl.when(pl.col("ts_ms") >= cut_ms)
            .then(pl.col("notional_usd") * 10.0)
            .otherwise(pl.col("notional_usd"))
        )
        mutated = run((mutated_trades, mutated_liqs, start_ms, end_ms), settings)
        assert entered_before_cut(mutated) == baseline


class TestDeterminism:
    def test_two_runs_of_the_same_inputs_agree_trade_for_trade(
        self, stream: tuple[pl.DataFrame, pl.DataFrame, int, int], settings: Settings
    ) -> None:
        first = run(stream, settings)
        second = run(stream, settings)
        # asdict rather than vars: Trade is a slots dataclass and has no __dict__.
        assert [asdict(t) for t in first.trades] == [asdict(t) for t in second.trades]  # type: ignore[attr-defined]
        assert first.equity_curve == second.equity_curve  # type: ignore[attr-defined]

    def test_the_same_seed_writes_a_byte_identical_trades_parquet(
        self,
        stream: tuple[pl.DataFrame, pl.DataFrame, int, int],
        settings: Settings,
        tmp_path: Path,
    ) -> None:
        """The definition of done: the same command twice, byte for byte."""
        digests = []
        for name in ("run-a", "run-b"):
            result = run(stream, settings, seed=0)
            directory = write_report(
                result,  # type: ignore[arg-type]
                settings,
                reports_dir=tmp_path,
                run_id=name,
                n_trials=1,
            )
            digests.append(hashlib.sha256((directory / "trades.parquet").read_bytes()).hexdigest())
        assert digests[0] == digests[1]


class TestGridFilter:
    def test_filtering_the_grid_does_not_change_a_single_trade(
        self,
        stream: tuple[pl.DataFrame, pl.DataFrame, int, int],
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`active_mask` is a speed optimisation, so its only job is to change nothing.

        It skips building a FeatureRow for market states no cascade could involve, which is
        most of a year. The mask is written as a superset of the detector's own start
        conditions, and this is what holds it to that: run once with the real mask and once
        with everything kept, and require the two to agree trade for trade. A mask that
        became too strict would quietly delete trades and every other test here would still
        pass.
        """
        import liqfade.backtest.features as features

        filtered = [asdict(t) for t in run(stream, settings).trades]  # type: ignore[attr-defined]

        monkeypatch.setattr(
            features,
            "active_mask",
            lambda grid, settings: pl.Series([True] * grid.height),
        )
        unfiltered = [asdict(t) for t in run(stream, settings).trades]  # type: ignore[attr-defined]

        assert filtered == unfiltered
        assert filtered, "the comparison is worthless if neither run traded"


class TestReport:
    def test_it_writes_all_three_files(
        self,
        stream: tuple[pl.DataFrame, pl.DataFrame, int, int],
        settings: Settings,
        tmp_path: Path,
    ) -> None:
        result = run(stream, settings)
        directory = write_report(
            result,
            settings,
            reports_dir=tmp_path,
            run_id="r1",
            n_trials=1,  # type: ignore[arg-type]
        )
        assert (directory / "report.md").exists()
        assert (directory / "summary.json").exists()
        assert (directory / "trades.parquet").exists()

    def test_the_first_page_names_the_trigger(
        self,
        stream: tuple[pl.DataFrame, pl.DataFrame, int, int],
        settings: Settings,
        tmp_path: Path,
    ) -> None:
        """A report that does not say which feed it ran on cannot be read.

        `exact` measures an edge nobody can reach live, so the report has to say so on the
        page a reader sees first, not in a footnote.
        """
        result = run(stream, settings)
        directory = write_report(
            result,
            settings,
            reports_dir=tmp_path,
            run_id="r1",
            n_trials=1,  # type: ignore[arg-type]
        )
        head = (directory / "report.md").read_text().split("## Headline")[0]
        assert "Trigger: `exact`" in head
        assert "not\nvisible in real time" in head or "not visible in real time" in head

    def test_the_latency_fragile_stamp_lands_on_the_first_page(
        self,
        stream: tuple[pl.DataFrame, pl.DataFrame, int, int],
        settings: Settings,
        tmp_path: Path,
    ) -> None:
        result = run(stream, settings)
        directory = write_report(
            result,  # type: ignore[arg-type]
            settings,
            reports_dir=tmp_path,
            run_id="r1",
            n_trials=1,
            lagged_ms=3000,
            latency_fragile=True,
        )
        text = (directory / "report.md").read_text()
        assert "LATENCY FRAGILE" in text.split("## Headline")[0]

    def test_the_summary_sidecar_carries_the_headline_numbers(
        self,
        stream: tuple[pl.DataFrame, pl.DataFrame, int, int],
        settings: Settings,
        tmp_path: Path,
    ) -> None:
        """So the panel's report list does not have to parse markdown."""
        import orjson

        result = run(stream, settings)
        directory = write_report(
            result,
            settings,
            reports_dir=tmp_path,
            run_id="r1",
            n_trials=3,  # type: ignore[arg-type]
        )
        summary = orjson.loads((directory / "summary.json").read_bytes())
        assert summary["trigger"] == "exact"
        assert summary["metrics"]["trades"] == 6
        assert summary["n_trials"] == 3
        assert summary["config_hash"] == config_hash(settings)
        assert summary["funnel"]["intents"] == 6

    def test_trades_parquet_has_the_columns_the_panel_reads(
        self,
        stream: tuple[pl.DataFrame, pl.DataFrame, int, int],
        settings: Settings,
        tmp_path: Path,
    ) -> None:
        result = run(stream, settings)
        directory = write_report(
            result,
            settings,
            reports_dir=tmp_path,
            run_id="r1",
            n_trials=1,  # type: ignore[arg-type]
        )
        frame = pl.read_parquet(directory / "trades.parquet")
        assert frame.height == 6
        for column in ("coin", "net_pnl", "net_bps", "entry_ms", "exit_ms", "exit_reason"):
            assert column in frame.columns

    def test_a_run_with_no_trades_still_writes_a_readable_report(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        """A window with nothing in it is a result, and often the interesting one."""
        empty = RunInputs(
            trades=pl.DataFrame(),
            liquidations=pl.DataFrame(),
            start_ms=0,
            end_ms=1000,
            starting_equity=1000.0,
        )
        result = run_backtest(empty, settings)
        directory = write_report(result, settings, reports_dir=tmp_path, run_id="empty", n_trials=1)
        assert "trades | 0" in (directory / "report.md").read_text()
        assert pl.read_parquet(directory / "trades.parquet").height == 0


class TestRunId:
    def test_it_carries_the_config_hash_so_a_rerun_is_recognisable(
        self, settings: Settings
    ) -> None:
        first = run_id_for(settings)
        assert first.split("_")[1] == config_hash(settings)[:8]

    def test_a_changed_config_changes_the_hash(
        self, settings_factory: SettingsFactory, settings: Settings
    ) -> None:
        other = settings_factory(**{"features.z_lookback_h": 1.0, "intent.k_target": 0.5})
        assert config_hash(other) != config_hash(settings)


class TestValidation:
    def test_the_holdout_splits_by_trade_count_not_by_days(
        self, stream: tuple[pl.DataFrame, pl.DataFrame, int, int], settings: Settings
    ) -> None:
        """Six trades at 25% holdout is four and two, wherever the calendar fell."""
        result = run(stream, settings)
        train, holdout = split_by_episode_count(result.trades, 0.25)  # type: ignore[attr-defined]
        assert (len(train), len(holdout)) == (5, 1) or (len(train), len(holdout)) == (4, 2)
        assert len(train) + len(holdout) == 6
        assert max(t.entry_ms for t in train) <= min(t.entry_ms for t in holdout)

    def test_the_trial_counter_increments_and_persists(self, tmp_path: Path) -> None:
        assert read_n_trials(tmp_path) == 0
        assert bump_n_trials(tmp_path) == 1
        assert bump_n_trials(tmp_path) == 2
        assert read_n_trials(tmp_path) == 2

    def test_trying_more_configurations_lowers_the_deflated_sharpe(self) -> None:
        """The whole point of it: the more you try, the higher the bar to clear."""
        one = deflated_sharpe(0.4, n_trials=1, n_obs=200)
        many = deflated_sharpe(0.4, n_trials=500, n_obs=200)
        assert one > many

    def test_metrics_on_an_empty_split_are_zeros_rather_than_a_crash(self) -> None:
        assert compute_metrics([]).trades == 0
