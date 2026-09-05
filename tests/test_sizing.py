"""One test per binding cap, plus the two floors.

Each case makes exactly one cap the tightest and leaves the others slack, so the assertion
is about that cap and not about which happened to be smallest. The expected notionals are
worked out from `docs/STRATEGY_AND_RISK.md` section 4 by hand.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from liqfade.config.models import Settings
from liqfade.sizing import LiquidityStats, size_intent
from liqfade.strategy.types import AssetMeta, Intent

SettingsFactory = Callable[..., Settings]

EQUITY = 100_000.0
META = AssetMeta(coin="BTC", sz_decimals=5, max_leverage=40.0, tier="core", listing_days=900.0)


def intent(entry: float = 100.0, stop: float = 95.0, target: float = 102.0) -> Intent:
    """A long whose stop is 5% away, so the risk-based notional is 20x the risk budget.

    5% rather than 1% so that `risk_budget` (10,000 here) lands under `coin_exposure`
    (0.25 of 100,000 equity, so 25,000). A tighter stop would make the coin cap the binding
    one in every case and each test below would be asserting the same thing.
    """
    return Intent(
        coin="BTC",
        side="long",
        entry_px=entry,
        target_px=target,
        stop_px=stop,
        time_stop_s=900.0,
        urgency="normal",
        ts_ms=0,
    )


def loose(**over: float | None) -> LiquidityStats:
    """Liquidity so deep that no liquidity cap can bind."""
    values: dict[str, float | None] = {
        "traded_usd_window": 1e12,
        "depth_usd": 1e12,
    }
    values.update(over)
    return LiquidityStats(**values)  # type: ignore[arg-type]


class TestBindingCaps:
    def test_risk_budget_binds(self, settings_factory: SettingsFactory) -> None:
        """N_risk = risk_frac * E / stop_distance_frac = 0.005 * 100k / 0.05 = 10,000."""
        settings = settings_factory()
        decision = size_intent(intent(), equity=EQUITY, stats=loose(), meta=META, settings=settings)
        expected = settings.sizing.risk_frac * EQUITY / 0.05
        assert decision.notional_usd == pytest.approx(expected, rel=1e-4)
        assert decision.reason is None

    def test_volume_participation_binds(self, settings_factory: SettingsFactory) -> None:
        """2% of a 100,000 USD trailing window is 2,000, under the 10,000 risk notional."""
        settings = settings_factory()
        stats = loose(traded_usd_window=100_000.0)
        decision = size_intent(intent(), equity=EQUITY, stats=stats, meta=META, settings=settings)
        assert decision.notional_usd == pytest.approx(
            settings.sizing.vol_participation * 100_000.0, rel=1e-4
        )

    def test_depth_participation_binds(self, settings_factory: SettingsFactory) -> None:
        """25% of 20,000 USD of resting depth is 5,000, under the 10,000 risk notional."""
        settings = settings_factory()
        stats = loose(depth_usd=20_000.0)
        decision = size_intent(intent(), equity=EQUITY, stats=stats, meta=META, settings=settings)
        assert decision.notional_usd == pytest.approx(
            settings.sizing.depth_participation * 20_000.0, rel=1e-4
        )

    def test_coin_exposure_binds(self, settings_factory: SettingsFactory) -> None:
        """coin_exposure_frac 0.25 of 100k is 25,000; with 20,000 on, 5,000 is left."""
        settings = settings_factory()
        decision = size_intent(
            intent(),
            equity=EQUITY,
            stats=loose(),
            meta=META,
            settings=settings,
            coin_exposure_usd=20_000.0,
        )
        cap = settings.sizing.coin_exposure_frac * EQUITY - 20_000.0
        assert decision.notional_usd == pytest.approx(cap, rel=1e-4)

    def test_total_exposure_binds(self, settings_factory: SettingsFactory) -> None:
        settings = settings_factory(**{"sizing.total_exposure_frac": 0.3})
        decision = size_intent(
            intent(),
            equity=EQUITY,
            stats=loose(),
            meta=META,
            settings=settings,
            total_exposure_usd=28_000.0,
        )
        assert decision.notional_usd == pytest.approx(0.3 * EQUITY - 28_000.0, rel=1e-4)

    def test_venue_max_leverage_binds(self, settings_factory: SettingsFactory) -> None:
        """A backstop that cannot bind for a real asset under the current config bounds.

        `sizing.coin_exposure_frac` is a fraction bounded at 1.0, so one coin can never be
        sized above `equity` of notional, while `maxLeverage * equity` is at least 3x that
        for every perp Hyperliquid lists. The cap is kept because it is the venue's own
        limit and the config bound is ours: raising `coin_exposure_frac` past 1.0 later, or
        a venue publishing a sub-1x asset, must not need this module to be revisited.

        Exercised here with a synthetic 0.5x asset, which is the only way to make it the
        minimum. That the value is unrealistic is the point of the test.
        """
        settings = settings_factory(
            **{"sizing.coin_exposure_frac": 1.0, "sizing.total_exposure_frac": 10.0}
        )
        capped = AssetMeta(coin="BTC", sz_decimals=5, max_leverage=0.5, listing_days=900.0)
        decision = size_intent(
            intent(stop=99.99), equity=EQUITY, stats=loose(), meta=capped, settings=settings
        )
        assert decision.notional_usd == pytest.approx(0.5 * EQUITY, rel=1e-4)
        assert decision.leverage_implied == pytest.approx(0.5, rel=1e-4)

    def test_the_venue_leverage_cap_is_never_the_binding_one_for_a_real_asset(
        self, settings_factory: SettingsFactory
    ) -> None:
        """States the fact above as an assertion, so a config change that alters it is loud."""
        settings = settings_factory(**{"sizing.coin_exposure_frac": 1.0})
        real = AssetMeta(coin="BTC", sz_decimals=5, max_leverage=40.0, listing_days=900.0)
        decision = size_intent(
            intent(stop=99.99), equity=EQUITY, stats=loose(), meta=real, settings=settings
        )
        assert decision.notional_usd == pytest.approx(EQUITY, rel=1e-4), "coin exposure binds"

    def test_leverage_is_reported_never_targeted(self, settings_factory: SettingsFactory) -> None:
        settings = settings_factory()
        decision = size_intent(intent(), equity=EQUITY, stats=loose(), meta=META, settings=settings)
        assert decision.leverage_implied == pytest.approx(decision.notional_usd / EQUITY)


class TestBlocked:
    def test_below_the_exchange_minimum_returns_zero(
        self, settings_factory: SettingsFactory
    ) -> None:
        """The 10 USDC floor. Nothing under it can be an order, so it is not one."""
        settings = settings_factory()
        decision = size_intent(intent(), equity=1.0, stats=loose(), meta=META, settings=settings)
        assert decision.notional_usd == 0.0
        assert decision.size == 0
        assert decision.reason == "min_notional"

    def test_a_full_coin_book_returns_zero_naming_the_cap(
        self, settings_factory: SettingsFactory
    ) -> None:
        settings = settings_factory()
        decision = size_intent(
            intent(),
            equity=EQUITY,
            stats=loose(),
            meta=META,
            settings=settings,
            coin_exposure_usd=settings.sizing.coin_exposure_frac * EQUITY,
        )
        assert decision.notional_usd == 0.0
        assert decision.reason == "coin_exposure"

    def test_a_full_book_overall_returns_zero_naming_the_cap(
        self, settings_factory: SettingsFactory
    ) -> None:
        settings = settings_factory()
        decision = size_intent(
            intent(),
            equity=EQUITY,
            stats=loose(),
            meta=META,
            settings=settings,
            total_exposure_usd=settings.sizing.total_exposure_frac * EQUITY,
        )
        assert decision.notional_usd == 0.0
        assert decision.reason == "total_exposure"

    def test_no_traded_volume_blocks(self, settings_factory: SettingsFactory) -> None:
        """No observed volume is no participation budget, not an unlimited one."""
        decision = size_intent(
            intent(),
            equity=EQUITY,
            stats=loose(traded_usd_window=0.0),
            meta=META,
            settings=settings_factory(),
        )
        assert decision.notional_usd == 0.0
        assert decision.reason == "vol_cap"

    def test_a_zero_width_stop_blocks(self, settings_factory: SettingsFactory) -> None:
        """Dividing the risk budget by zero would size the whole account into one trade."""
        flat = intent(entry=100.0, stop=100.0)
        decision = size_intent(
            flat, equity=EQUITY, stats=loose(), meta=META, settings=settings_factory()
        )
        assert decision.notional_usd == 0.0
        assert decision.reason == "risk_budget"

    def test_no_equity_blocks(self, settings_factory: SettingsFactory) -> None:
        decision = size_intent(
            intent(), equity=0.0, stats=loose(), meta=META, settings=settings_factory()
        )
        assert decision.notional_usd == 0.0
        assert decision.reason == "risk_budget"


class TestNullLiquidity:
    def test_null_depth_does_not_apply_the_depth_cap(
        self, settings_factory: SettingsFactory
    ) -> None:
        """The historical year records no book at all.

        A null depth read as zero would size every backtest trade to nothing and the
        strategy would look untradeable for a reason that is about the archive rather than
        about the market. The cap is skipped and the report counts how often.
        """
        settings = settings_factory()
        decision = size_intent(
            intent(),
            equity=EQUITY,
            stats=loose(depth_usd=None),
            meta=META,
            settings=settings,
        )
        assert decision.notional_usd == pytest.approx(settings.sizing.risk_frac * EQUITY / 0.05)
        assert decision.depth_cap_applied is False

    def test_known_depth_marks_the_cap_as_applied(self, settings_factory: SettingsFactory) -> None:
        decision = size_intent(
            intent(),
            equity=EQUITY,
            stats=loose(depth_usd=1e9),
            meta=META,
            settings=settings_factory(),
        )
        assert decision.depth_cap_applied is True


class TestRounding:
    def test_size_is_rounded_to_the_assets_precision(
        self, settings_factory: SettingsFactory
    ) -> None:
        """And the notional is recomputed from the rounded size, not the other way round.

        The order carries the size, so the notional that is reported has to be the one the
        rounded size actually buys. Reporting the pre-rounding number would put a small
        permanent error into every PnL calculation downstream.
        """
        settings = settings_factory()
        coarse = AssetMeta(coin="PEPE", sz_decimals=0, max_leverage=10.0, listing_days=900.0)
        decision = size_intent(
            intent(entry=3.0, stop=2.85, target=3.1),
            equity=EQUITY,
            stats=loose(),
            meta=coarse,
            settings=settings,
        )
        assert decision.size == decision.size.to_integral_value()
        assert decision.notional_usd == pytest.approx(float(decision.size) * 3.0)

    def test_a_size_that_rounds_away_blocks_on_the_minimum(
        self, settings_factory: SettingsFactory
    ) -> None:
        """A coin priced above the whole budget rounds to zero size, which is no order."""
        settings = settings_factory()
        pricey = AssetMeta(coin="BTC", sz_decimals=0, max_leverage=10.0, listing_days=900.0)
        decision = size_intent(
            intent(entry=1_000_000.0, stop=950_000.0, target=1_020_000.0),
            equity=1_000.0,
            stats=loose(),
            meta=pricey,
            settings=settings,
        )
        assert decision.size == 0
        assert decision.reason == "min_notional"
