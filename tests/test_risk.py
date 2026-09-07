"""One test per risk gate, plus persistence across a restart and the per-cluster cap.

Every threshold comes from config through `settings_factory`. The state is put into the
shape a gate cares about directly rather than by trading into it, so each test exercises one
gate and the assertion says which.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import orjson
import pytest

from liqfade.config.models import Settings
from liqfade.risk.engine import AccountView, EntryRequest, RiskAction, RiskEngine
from liqfade.risk.state import RiskStateStore

SettingsFactory = Callable[..., Settings]

NOW = 1_772_323_200_000  # 2026-03-01T00:00:00Z, which is midnight, so no day roll fires
EQUITY = 100_000.0
MINUTE_MS = 60_000


# Every coin these tests trade, with SOL deliberately left out: `stale_data` treats a coin
# it has never received data for as staler than stale, and SOL is how that is asserted.
FED_COINS: tuple[str, ...] = ("BTC", "ETH", "A", "B", "X", "Y", *(f"C{n}" for n in range(50)))


def account(now: int = NOW, **over: object) -> AccountView:
    """A healthy account whose feeds are all current as of `now`.

    `now` has to be passed by any test that advances the clock. The staleness gates measure
    against the request's timestamp, so an account built at NOW looks dead an hour later,
    which would mask whichever gate the test was actually about.
    """
    values: dict[str, object] = {
        "equity": EQUITY,
        "open_sides": {},
        "coin_last_data_ms": dict.fromkeys(FED_COINS, now),
        "global_last_data_ms": now,
    }
    values.update(over)
    return AccountView(**values)  # type: ignore[arg-type]


def request(coin: str = "BTC", side: str = "long", notional: float = 1_000.0, now: int = NOW):
    return EntryRequest(coin=coin, side=side, notional_usd=notional, now_ms=now)  # type: ignore[arg-type]


@pytest.fixture
def engine(settings_factory: SettingsFactory, tmp_path: Path) -> RiskEngine:
    return RiskEngine(settings_factory(), path=tmp_path / "risk_state.json")


def prime(engine: RiskEngine) -> None:
    """Give the engine a started day and a peak, without tripping anything."""
    engine.on_equity(EQUITY, NOW)


class TestAllowed:
    def test_a_clean_account_may_enter(self, engine: RiskEngine) -> None:
        prime(engine)
        verdict = engine.check_entry(request(), account())
        assert verdict.allowed
        assert verdict.action is RiskAction.NONE


class TestGates:
    def test_kill_switch_blocks_and_flattens(
        self, settings_factory: SettingsFactory, tmp_path: Path
    ) -> None:
        """Outranks every other gate, and is checked first."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        settings = settings_factory(**{"paths.data_dir": str(data_dir)})
        engine = RiskEngine(settings, path=tmp_path / "risk_state.json")
        prime(engine)
        assert engine.check_entry(request(), account()).allowed

        (data_dir / "KILL").touch()
        verdict = engine.check_entry(request(), account())
        assert verdict.reason == "kill_switch"
        assert verdict.action is RiskAction.FLATTEN

    def test_daily_loss_blocks_until_the_next_day(self, engine: RiskEngine) -> None:
        prime(engine)
        limit = engine.settings.risk.max_daily_loss_frac * EQUITY
        engine.on_exit("ETH", -limit, NOW)
        assert engine.check_entry(request(), account()).reason == "daily_loss"

        # A new UTC day resets the budget and clears this halt specifically.
        tomorrow = NOW + 86_400_000
        assert engine.check_entry(request(now=tomorrow), account(tomorrow)).allowed

    def test_drawdown_blocks_flattens_and_needs_a_manual_resume(self, engine: RiskEngine) -> None:
        prime(engine)
        floor = (1.0 - engine.settings.risk.max_dd_frac) * EQUITY
        verdict = engine.check_entry(request(), account(equity=floor))
        assert verdict.reason == "drawdown"
        assert verdict.action is RiskAction.FLATTEN
        assert engine.state.halt_until_ms is None, "a drawdown halt must not expire on a timer"

        # Recovering equity does not clear it: a person has to look.
        later = NOW + 10 * 86_400_000
        assert engine.check_entry(request(now=later), account(later)).reason == "drawdown"
        assert engine.resume(NOW) == "drawdown"
        assert engine.check_entry(request(), account()).allowed

    def test_a_losing_streak_halts_for_the_configured_cooldown(self, engine: RiskEngine) -> None:
        prime(engine)
        risk = engine.settings.risk
        last_loss = NOW
        for step in range(risk.max_consecutive_losses):
            last_loss = NOW + step
            engine.on_exit("ETH", -1.0, last_loss)
        assert engine.check_entry(request(), account()).reason == "losing_streak"

        # The cooldown runs from the losing trade, not from the start of the test.
        after = last_loss + int(risk.cooldown_after_streak_min * MINUTE_MS) + 1
        assert engine.check_entry(request(now=after), account(after)).allowed
        assert engine.state.consecutive_losses == 0, "serving the cooldown clears the count"

    def test_a_zero_cooldown_turns_the_streak_gate_off(
        self, settings_factory: SettingsFactory, tmp_path: Path
    ) -> None:
        """`cooldown_after_streak_min: 0` is how a tuning run asks for the whole tape.

        The gate blocks for the length of the cooldown and then clears the count, so a
        cooldown of zero is served the instant it is imposed. It is the halt that blocks,
        not the counter: `config/backtest/v1_confluence.yaml` sets 0 for exactly this and
        would otherwise stop trading six losses into the year.
        """
        engine = RiskEngine(
            settings_factory(**{"risk.cooldown_after_streak_min": 0}),
            path=tmp_path / "risk_state.json",
        )
        prime(engine)
        for step in range(engine.settings.risk.max_consecutive_losses):
            engine.on_exit("ETH", -1.0, NOW + step)
        assert engine.state.consecutive_losses == engine.settings.risk.max_consecutive_losses

        after = NOW + engine.settings.risk.max_consecutive_losses
        assert engine.check_entry(request(now=after), account(after)).allowed
        assert engine.state.consecutive_losses == 0, "a zero cooldown still clears the count"

    def test_a_win_resets_the_streak(self, engine: RiskEngine) -> None:
        prime(engine)
        for step in range(engine.settings.risk.max_consecutive_losses - 1):
            engine.on_exit("ETH", -1.0, NOW + step)
        engine.on_exit("ETH", 5.0, NOW + 100)
        assert engine.state.consecutive_losses == 0

    def test_a_coin_cooldown_blocks_only_that_coin(self, engine: RiskEngine) -> None:
        prime(engine)
        engine.on_exit("BTC", -1.0, NOW)
        assert engine.check_entry(request("BTC"), account()).reason == "coin_cooldown"
        assert engine.check_entry(request("ETH"), account()).allowed

        after = NOW + int(engine.settings.risk.coin_cooldown_after_loss_min * MINUTE_MS) + 1
        assert engine.check_entry(request("BTC", now=after), account(after)).allowed

    def test_a_winning_exit_gets_the_shorter_cooldown(self, engine: RiskEngine) -> None:
        prime(engine)
        risk = engine.settings.risk
        engine.on_exit("BTC", 5.0, NOW)
        short = NOW + int(risk.coin_cooldown_after_exit_min * MINUTE_MS) + 1
        assert engine.check_entry(request("BTC", now=short), account(short)).allowed

    def test_max_concurrent_positions_blocks_a_new_coin(self, engine: RiskEngine) -> None:
        prime(engine)
        risk = engine.settings.risk
        held = {
            f"C{n}": ("long" if n % 2 else "short") for n in range(risk.max_concurrent_positions)
        }
        assert (
            engine.check_entry(request("BTC"), account(open_sides=held)).reason == "max_positions"
        )

    def test_an_open_coin_may_still_be_added_to(self, engine: RiskEngine) -> None:
        """Concurrency counts coins, not tickets.

        Refusing to add to an open position would leave one that could not be scaled into,
        and `sizing.coin_exposure_frac` already bounds how large it can get.
        """
        prime(engine)
        risk = engine.settings.risk
        held = {f"C{n}": "long" for n in range(risk.max_concurrent_positions)}
        held["BTC"] = "long"
        assert engine.check_entry(request("BTC"), account(open_sides=held)).allowed

    def test_max_positions_per_direction_blocks(self, engine: RiskEngine) -> None:
        prime(engine)
        risk = engine.settings.risk
        held = {f"C{n}": "long" for n in range(risk.max_positions_per_direction)}
        verdict = engine.check_entry(request("BTC", side="long"), account(open_sides=held))
        assert verdict.reason == "max_positions_per_direction"
        assert engine.check_entry(request("BTC", side="short"), account(open_sides=held)).allowed

    def test_stale_coin_data_blocks_that_coin(self, engine: RiskEngine) -> None:
        prime(engine)
        stale_ms = int(engine.settings.risk.stale_coin_s * 1000)
        view = account(coin_last_data_ms={"BTC": NOW - stale_ms, "ETH": NOW})
        assert engine.check_entry(request("BTC"), view).reason == "stale_coin_data"
        assert engine.check_entry(request("ETH"), view).allowed

    def test_a_coin_never_seen_is_staler_than_stale(self, engine: RiskEngine) -> None:
        prime(engine)
        assert engine.check_entry(request("SOL"), account()).reason == "stale_coin_data"

    def test_global_silence_cancels_then_flattens(self, engine: RiskEngine) -> None:
        prime(engine)
        risk = engine.settings.risk
        cancel = engine.check_entry(
            request(), account(global_last_data_ms=NOW - int(risk.stale_global_cancel_s * 1000))
        )
        assert cancel.reason == "global_feed_stale"
        assert cancel.action is RiskAction.CANCEL_RESTING

        flatten = engine.check_entry(
            request(), account(global_last_data_ms=NOW - int(risk.stale_global_flatten_s * 1000))
        )
        assert flatten.reason == "global_feed_dead"
        assert flatten.action is RiskAction.FLATTEN

    def test_a_reconnect_storm_halts_entries(self, engine: RiskEngine) -> None:
        prime(engine)
        for step in range(engine.settings.risk.max_reconnects_10min + 1):
            engine.on_reconnect(NOW + step)
        assert engine.check_entry(request(), account()).reason == "reconnect_storm"

    def test_a_reject_storm_halts_entries(self, engine: RiskEngine) -> None:
        prime(engine)
        for step in range(engine.settings.risk.max_rejects_10min + 1):
            engine.on_reject(NOW + step)
        assert engine.check_entry(request(), account()).reason == "reject_storm"

    def test_storms_age_out_of_their_ten_minute_window(self, engine: RiskEngine) -> None:
        prime(engine)
        for step in range(engine.settings.risk.max_rejects_10min + 1):
            engine.on_reject(NOW + step)
        aged = NOW + 11 * MINUTE_MS
        assert engine.check_entry(request(now=aged), account(aged)).allowed


class TestClusterCap:
    """The gate Phase 3 asked for by name: a sweep across forty coins is one bet."""

    def test_forty_intents_in_one_minute_bind_after_the_first_few(self, engine: RiskEngine) -> None:
        risk = engine.settings.risk
        prime(engine)
        cap = risk.max_cluster_exposure_frac * EQUITY
        per_trade = 5_000.0  # 5% of equity each, well inside every per-coin cap

        allowed, blocked = [], []
        for n in range(40):
            proposal = request(f"C{n}", "long", per_trade, NOW + n * 1000)
            verdict = engine.check_entry(proposal, account(proposal.now_ms))
            if verdict.allowed:
                allowed.append(proposal.coin)
                engine.on_entry(proposal)
            else:
                blocked.append(verdict.reason)

        assert len(allowed) == int(cap // per_trade), "the cap binds after the first few"
        assert blocked, "the rest are refused"
        assert set(blocked) == {"cluster_exposure"}, "and the reason names the cluster"

    def test_the_cluster_is_one_sided(self, engine: RiskEngine) -> None:
        """Forty longs into a sweep lose together; a short in the same minute does not."""
        prime(engine)
        cap = engine.settings.risk.max_cluster_exposure_frac * EQUITY
        for n in range(5):
            proposal = request(f"C{n}", "long", cap / 5, NOW + n * 1000)
            assert engine.check_entry(proposal, account(proposal.now_ms)).allowed
            engine.on_entry(proposal)

        assert engine.check_entry(request("X", "long", 1.0), account()).reason == "cluster_exposure"
        assert engine.check_entry(request("Y", "short", cap / 5), account()).allowed

    def test_the_window_rolls(self, engine: RiskEngine) -> None:
        prime(engine)
        risk = engine.settings.risk
        cap = risk.max_cluster_exposure_frac * EQUITY
        first = request("A", "long", cap, NOW)
        assert engine.check_entry(first, account()).allowed
        engine.on_entry(first)
        assert engine.check_entry(request("B", "long", 1.0), account()).reason == "cluster_exposure"

        later = NOW + int(risk.cluster_window_s * 1000) + 1
        assert engine.check_entry(request("B", "long", cap, later), account(later)).allowed

    def test_no_equity_blocks_rather_than_dividing_by_it(self, engine: RiskEngine) -> None:
        prime(engine)
        assert not engine.check_entry(request(), account(equity=0.0)).allowed


class TestPersistence:
    def test_state_survives_a_restart(
        self, settings_factory: SettingsFactory, tmp_path: Path
    ) -> None:
        """A limit a restart clears is not a limit.

        Reconstructs the engine from the file rather than reusing the object, which is what
        a process restart actually does.
        """
        path = tmp_path / "risk_state.json"
        settings = settings_factory()
        first = RiskEngine(settings, path=path)
        first.on_equity(EQUITY, NOW)
        for step in range(settings.risk.max_consecutive_losses):
            first.on_exit("BTC", -100.0, NOW + step)

        restarted = RiskEngine(settings, path=path)
        assert restarted.state.consecutive_losses == settings.risk.max_consecutive_losses
        assert restarted.state.daily_realized_pnl == pytest.approx(
            -100.0 * settings.risk.max_consecutive_losses
        )
        assert restarted.state.peak_equity == EQUITY
        assert restarted.check_entry(request(), account()).reason == "losing_streak"

    def test_the_file_is_written_on_every_mutation(
        self, settings_factory: SettingsFactory, tmp_path: Path
    ) -> None:
        path = tmp_path / "risk_state.json"
        engine = RiskEngine(settings_factory(), path=path)
        assert not path.exists()
        engine.on_equity(EQUITY, NOW)
        assert path.exists()
        assert RiskStateStore(path).state.peak_equity == EQUITY

    def test_the_file_is_not_world_readable(
        self, settings_factory: SettingsFactory, tmp_path: Path
    ) -> None:
        """It carries equity and the halt state, so it gets the same 0600 as state.json."""
        path = tmp_path / "risk_state.json"
        engine = RiskEngine(settings_factory(), path=path)
        engine.on_equity(EQUITY, NOW)
        assert path.stat().st_mode & 0o077 == 0

    def test_the_entry_log_is_pruned_so_the_file_cannot_grow_forever(
        self, settings_factory: SettingsFactory, tmp_path: Path
    ) -> None:
        settings = settings_factory()
        engine = RiskEngine(settings, path=tmp_path / "risk_state.json")
        engine.on_equity(EQUITY, NOW)
        span_ms = int(settings.risk.cluster_window_s * 1000)
        for n in range(200):
            engine.on_entry(request(f"C{n}", "long", 1.0, NOW + n * span_ms))
        assert len(engine.state.entries) <= 2, "only the current window is kept"

    def test_a_corrupt_file_raises_rather_than_starting_fresh(
        self, settings_factory: SettingsFactory, tmp_path: Path
    ) -> None:
        """Starting over would clear a halt nobody has resolved."""
        path = tmp_path / "risk_state.json"
        path.write_text("{not json")
        with pytest.raises(orjson.JSONDecodeError):
            RiskEngine(settings_factory(), path=path)


class TestResume:
    def test_resume_refuses_while_the_kill_file_is_present(
        self, settings_factory: SettingsFactory, tmp_path: Path
    ) -> None:
        """`/resume` must not be a way to trade through a kill switch."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "KILL").touch()
        settings = settings_factory(**{"paths.data_dir": str(data_dir)})
        engine = RiskEngine(settings, path=tmp_path / "risk_state.json")
        engine.on_equity(EQUITY, NOW)
        engine.check_entry(request(), account())
        assert engine.resume(NOW) is None
        assert engine.check_entry(request(), account()).reason == "kill_switch"
