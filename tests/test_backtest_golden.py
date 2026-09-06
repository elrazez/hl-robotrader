"""The golden run: a fixture with no noise, and expected results derived on paper.

Every other test here checks one part in isolation. This one pins the whole engine's
behaviour to numbers worked out from `config/default.yaml` and `tests/fixtures/golden_lake.py`
with a calculator, so a refactor that changes what the strategy does fails loudly even when
every unit test still passes.

The derivation, in full, so a future reader can check it rather than trust it:

- The fixture drops one coin from 100.00 to 98.00 over 30 seconds, three times, then
  recovers linearly to 100.00 over the following ten minutes.
- The **anchor is 99.667, not 100.00**. A cascade starts when
  `abs(ret_60s) / rv_5m` clears `cascade.ret_over_rv_min`, which on this slope takes about
  seven seconds; at the first liquidation print the price has not moved and the ratio is
  near zero. Mid at +7s is `100.00 - 2.00 * 7/30 = 99.667`. Anchoring earlier would mean
  using a price from before the detector knew anything was happening.
- `intent.k_target` is 0.8, so the target is `98.00 + 0.8 * 1.667 = 99.333`.
- `exhaustion.mode` is `fixed_delay` at 120s, so entry is about +127s. Mid there is
  `98.00 + 2.00 * (127 - 30) / 600 = 98.323`, and the fill pays `slip_stale_bps` on top
  because this lake records no book: `98.323 * 1.0005 = 98.372`.
- `intent.k_stop` is 1.0 and `rv_300s` on this path is well under 1%, so the stop sits a
  few tenths below 98.00 and the price never returns there. Every trade exits on target.
- The target is reached at `30 + 0.667 * 600 = 430` seconds after the start, so each hold
  is about `430 - 127 = 303` seconds.

If one of those assertions fails, the question to ask is which of the config values above
moved, not how to loosen the tolerance.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from liqfade.backtest.engine import RunInputs, run_backtest
from liqfade.config.models import Settings

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import golden_lake

SettingsFactory = Callable[..., Settings]

EQUITY = 100_000.0
# Every number below is derived in the module docstring, not read off a run.
EXPECTED_ANCHOR_PX = 99.667
EXPECTED_TARGET_PX = 99.333
EXPECTED_ENTRY_PX = 98.372
EXPECTED_HOLD_S = 303.0
HOLD_TOLERANCE_S = 15.0
PRICE_TOLERANCE = 0.02


@pytest.fixture(scope="module")
def lake() -> tuple[object, object, int, int]:
    return golden_lake.build()


@pytest.fixture
def settings(settings_factory: SettingsFactory) -> Settings:
    """Paper config, with only the z lookback shortened so the fixture need not be a day.

    Nothing that decides an entry or an exit is touched: `k_target`, `k_stop`,
    `fixed_delay_s`, the cascade thresholds and every fee are the shipped values, which is
    what makes the derivation above the derivation of this run.
    """
    return settings_factory(**{"features.z_lookback_h": 1.0})


@pytest.fixture
def result(lake: tuple[object, object, int, int], settings: Settings) -> object:
    trades, liquidations, start_ms, end_ms = lake
    return run_backtest(
        RunInputs(
            trades=trades,  # type: ignore[arg-type]
            liquidations=liquidations,  # type: ignore[arg-type]
            start_ms=start_ms,
            end_ms=end_ms,
            starting_equity=EQUITY,
        ),
        settings,
    )


def test_a_kill_file_on_disk_does_not_stop_a_backtest(
    lake: tuple[object, object, int, int],
    settings_factory: SettingsFactory,
    tmp_path: Path,
) -> None:
    """A replay places no orders, so there is nothing for a kill switch to stop.

    The gate itself is unchanged and still fails closed for the live loop, which is what
    `tests/test_risk.py` pins. What changed is that the backtest never asks it: on
    2026-09-06 a USB timeout on the lake's drive was read as a kill file by
    `Settings.kill_switch_present`, which fails closed on any OSError, and cost a research
    run an entry. Two runs of one command disagreed, which is the property a backtest most
    needs to keep.
    """
    kill = tmp_path / "KILL"
    kill.write_text("")
    settings = settings_factory(**{"features.z_lookback_h": 1.0, "paths.data_dir": str(tmp_path)})
    assert settings.kill_switch_present, "the fixture has to actually place the file"

    trades, liquidations, start_ms, end_ms = lake
    result = run_backtest(
        RunInputs(
            trades=trades,  # type: ignore[arg-type]
            liquidations=liquidations,  # type: ignore[arg-type]
            start_ms=start_ms,
            end_ms=end_ms,
            starting_equity=EQUITY,
        ),
        settings,
    )
    assert len(result.trades) == golden_lake.N_CASCADES
    assert "kill_switch" not in result.risk_blocks


class TestGoldenRun:
    def test_it_finds_exactly_the_cascades_the_fixture_contains(self, result: object) -> None:
        assert result.intents == golden_lake.N_CASCADES  # type: ignore[attr-defined]
        assert len(result.trades) == golden_lake.N_CASCADES  # type: ignore[attr-defined]

    def test_every_trade_fades_the_cascade_and_exits_on_target(self, result: object) -> None:
        for trade in result.trades:  # type: ignore[attr-defined]
            assert trade.side == "long", "a long liquidation is forced selling; the fade buys"
            assert trade.exit_reason == "target"

    def test_the_anchor_is_mid_at_detection_not_before_the_cascade(self, result: object) -> None:
        """The subtle one, and the reason the target is 99.333 rather than 99.60.

        A cascade starts when the move clears `ret_over_rv_min`, which takes a few seconds;
        the price at the first liquidation print has not moved and the detector cannot know
        anything is happening. Anchoring at 100.00 would be using a price from before it
        knew, which is lookahead wearing a friendly face.
        """
        for trade in result.trades:  # type: ignore[attr-defined]
            assert trade.explain["extreme_px"] == pytest.approx(
                golden_lake.EXTREME_PX, abs=PRICE_TOLERANCE
            )
            assert trade.explain["anchor_px"] == pytest.approx(
                EXPECTED_ANCHOR_PX, abs=PRICE_TOLERANCE
            )
            assert trade.explain["anchor_px"] < golden_lake.ANCHOR_PX

    def test_the_exit_price_is_the_target_the_config_defines(
        self, result: object, settings: Settings
    ) -> None:
        """`extreme + k_target * (anchor - extreme)`, from the anchor the detector saw."""
        for trade in result.trades:  # type: ignore[attr-defined]
            from_config = golden_lake.EXTREME_PX + settings.intent.k_target * (
                trade.explain["anchor_px"] - golden_lake.EXTREME_PX
            )
            assert from_config == pytest.approx(EXPECTED_TARGET_PX, abs=PRICE_TOLERANCE)
            assert trade.exit_px == pytest.approx(EXPECTED_TARGET_PX, abs=PRICE_TOLERANCE)

    def test_the_entry_lands_where_the_fixed_delay_puts_it(
        self, result: object, settings: Settings
    ) -> None:
        """+127s puts mid at 98.323, and a bookless fill pays slip_stale_bps on top."""
        slipped = 98.323 * (1 + settings.backtest.slip_stale_bps / 1e4)
        assert slipped == pytest.approx(EXPECTED_ENTRY_PX, abs=0.005)
        for trade in result.trades:  # type: ignore[attr-defined]
            assert trade.entry_px == pytest.approx(EXPECTED_ENTRY_PX, abs=PRICE_TOLERANCE)
            assert trade.no_book_entry is True, "this lake records no book"

    def test_the_hold_is_the_distance_from_entry_to_target(self, result: object) -> None:
        for trade in result.trades:  # type: ignore[attr-defined]
            assert trade.hold_s == pytest.approx(EXPECTED_HOLD_S, abs=HOLD_TOLERANCE_S)

    def test_the_three_trades_are_identical_because_the_fixture_repeats(
        self, result: object
    ) -> None:
        """No noise anywhere, so three identical cascades must produce three identical
        trades. Any divergence is state leaking between them."""
        prices = {round(trade.entry_px, 6) for trade in result.trades}  # type: ignore[attr-defined]
        exits = {round(trade.exit_px, 6) for trade in result.trades}  # type: ignore[attr-defined]
        assert len(prices) == 1, f"entries differ across identical cascades: {prices}"
        assert len(exits) == 1, f"exits differ across identical cascades: {exits}"

    def test_the_net_pnl_is_the_gross_move_less_two_fees(
        self, result: object, settings: Settings
    ) -> None:
        """Worked out per trade rather than asserted as a total.

        Entry at 98.372 and exit at 99.333 is about 0.96 a unit, and the round trip is one
        taker fee in (the entry is high urgency on this fixture, so it crosses) and one
        maker fee out at the resting target.
        """
        for trade in result.trades:  # type: ignore[attr-defined]
            gross = (trade.exit_px - trade.entry_px) * trade.size
            assert trade.gross_pnl == pytest.approx(gross)
            fees = settings.backtest.taker_fee_frac * trade.entry_px * trade.size
            fees += settings.backtest.maker_fee_frac * trade.exit_px * trade.size
            assert trade.fees_usd == pytest.approx(fees, rel=1e-6)
            assert trade.net_pnl == pytest.approx(
                trade.gross_pnl - trade.fees_usd - trade.funding_usd
            )

    def test_the_run_makes_money_and_the_cash_agrees(self, result: object) -> None:
        net = sum(trade.net_pnl for trade in result.trades)  # type: ignore[attr-defined]
        assert net > 0
        assert result.portfolio.cash == pytest.approx(EQUITY + net)  # type: ignore[attr-defined]
        assert result.portfolio.positions == {}  # type: ignore[attr-defined]

    def test_no_trade_is_stopped_out_on_this_path(self, result: object) -> None:
        """The stop sits below the extreme and the price never returns there."""
        assert all(trade.exit_reason != "stop" for trade in result.trades)  # type: ignore[attr-defined]

    def test_nothing_was_blocked(self, result: object) -> None:
        """A blocked intent here would mean a cap firing on a run designed not to trip one."""
        assert result.sizing_blocks == {}  # type: ignore[attr-defined]
        assert result.risk_blocks == {}  # type: ignore[attr-defined]
        assert result.portfolio.abandoned_entries == 0  # type: ignore[attr-defined]


class TestGoldenBaselineIsTheRuleFreeConfiguration:
    def test_enabling_a_rule_that_fails_removes_every_trade(
        self, lake: tuple[object, object, int, int], settings_factory: SettingsFactory
    ) -> None:
        """The golden numbers describe the all-rules-off configuration, so this pins it.

        Each cascade liquidates 6,000,000 USD, so a floor above that must kill all three.
        If enabling a rule left the trades in place, the rule layer would not be wired to
        anything and every per-rule table in every report would be fiction.
        """
        trades, liquidations, start_ms, end_ms = lake
        settings = settings_factory(
            **{
                "features.z_lookback_h": 1.0,
                "strategy.rules.min_cascade_notional.enabled": True,
                "strategy.rules.min_cascade_notional.min_notional_usd": 9_000_000.0,
            }
        )
        result = run_backtest(
            RunInputs(
                trades=trades,  # type: ignore[arg-type]
                liquidations=liquidations,  # type: ignore[arg-type]
                start_ms=start_ms,
                end_ms=end_ms,
                starting_equity=EQUITY,
            ),
            settings,
        )
        assert result.trades == []
        stats = result.strategy.stats.rules["min_cascade_notional"]
        assert stats.killed == golden_lake.N_CASCADES

    def test_a_rule_that_passes_leaves_the_golden_result_untouched(
        self,
        lake: tuple[object, object, int, int],
        settings_factory: SettingsFactory,
        result: object,
    ) -> None:
        """A rule that passes must not change a price, only the explain mapping."""
        trades, liquidations, start_ms, end_ms = lake
        settings = settings_factory(
            **{
                "features.z_lookback_h": 1.0,
                "strategy.rules.min_cascade_notional.enabled": True,
                "strategy.rules.min_cascade_notional.min_notional_usd": 1.0,
            }
        )
        with_rule = run_backtest(
            RunInputs(
                trades=trades,  # type: ignore[arg-type]
                liquidations=liquidations,  # type: ignore[arg-type]
                start_ms=start_ms,
                end_ms=end_ms,
                starting_equity=EQUITY,
            ),
            settings,
        )
        assert [t.net_pnl for t in with_rule.trades] == [
            t.net_pnl
            for t in result.trades  # type: ignore[attr-defined]
        ]
        assert all(
            "min_cascade_notional.cascade_notional_usd" in t.explain for t in with_rule.trades
        )


class TestGoldenLatencyMatters:
    def test_a_worse_latency_produces_a_worse_entry(
        self, lake: tuple[object, object, int, int], settings_factory: SettingsFactory
    ) -> None:
        """The latency model has to bite, or the fragility rerun means nothing.

        Price is recovering upwards while a long entry waits, so a later fill is a worse
        one. A latency that changed no fill price would mean the model was decorative.
        """
        trades, liquidations, start_ms, end_ms = lake

        def entries(latency_ms: int) -> list[float]:
            settings = settings_factory(
                **{"features.z_lookback_h": 1.0, "backtest.latency_ms": latency_ms}
            )
            outcome = run_backtest(
                RunInputs(
                    trades=trades,  # type: ignore[arg-type]
                    liquidations=liquidations,  # type: ignore[arg-type]
                    start_ms=start_ms,
                    end_ms=end_ms,
                    starting_equity=EQUITY,
                ),
                settings,
            )
            return [trade.entry_px for trade in outcome.trades]

        prompt = entries(1000)
        lagged = entries(30_000)
        assert prompt and len(prompt) == len(lagged)
        assert all(late > early for early, late in zip(prompt, lagged, strict=True)), (
            "a long entering later into a rising market must pay more"
        )
