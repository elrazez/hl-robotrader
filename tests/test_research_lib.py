"""The two pieces of scripts/research/_lib.py that every DECISION in Phase 3 rests on.

A bug in the cluster bootstrap or the cost model does not make a report fail; it makes it
confidently report an edge that is not there. So those two get real tests with hand
computed answers, while the arg parsing and the report writer are covered by the scripts'
own smoke tests.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import polars as pl
import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "research" / "_lib.py"


def load() -> object:
    spec = importlib.util.spec_from_file_location("research_lib", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lib = load()
MINUTE = 60_000
DAY = 1_772_323_200_000


def test_a_cluster_is_one_side_inside_one_five_minute_window_across_every_coin() -> None:
    """A market-wide sweep liquidates hundreds of coins in a minute and is one observation."""
    episodes = pl.DataFrame(
        {
            "coin": ["BTC", "ETH", "PEPE", "SOL", "BTC"],
            "side": ["long", "long", "long", "short", "long"],
            "start_ts": [DAY, DAY + MINUTE, DAY + 4 * MINUTE, DAY + MINUTE, DAY + 6 * MINUTE],
        }
    )
    clusters = lib.with_clusters(episodes, window_s=300)["cluster"].to_list()
    assert clusters[0] == clusters[1] == clusters[2]  # same side, same five minutes
    assert clusters[3] != clusters[0]  # the other side is never the same observation
    assert clusters[4] != clusters[0]  # the next five minute window


def test_the_bootstrap_resamples_whole_clusters_and_not_episodes() -> None:
    """Two clusters of fifty, one all +10 and one all -10, mean zero.

    Resampling clusters can draw two of the same one, so the interval has to reach the
    full plus and minus ten. Resampling episodes would concentrate near zero with a
    standard error of about 1 bp and give an interval ten times too narrow, which is
    exactly the false confidence the preregistration bans.
    """
    values = np.array([10.0] * 50 + [-10.0] * 50)
    clusters = np.array([0] * 50 + [1] * 50)
    estimate = lib.cluster_bootstrap(values, clusters, confidence=0.95, draws=2000, seed=7)
    assert estimate.mean == pytest.approx(0.0)
    assert estimate.n == 100
    assert estimate.n_clusters == 2
    assert estimate.hi - estimate.lo > 15.0


def test_the_bootstrap_narrows_as_clusters_are_added() -> None:
    """Forty independent clusters of one episode each: the interval is a standard error wide."""
    rng = np.random.default_rng(3)
    values = rng.normal(20.0, 10.0, 400)
    estimate = lib.cluster_bootstrap(
        values, np.arange(400) // 10, confidence=0.95, draws=2000, seed=7
    )
    assert estimate.lo > 0.0
    assert estimate.hi - estimate.lo < 12.0


def test_the_three_cost_columns_on_one_hand_computed_trade() -> None:
    """Fade a long liquidation: buy the extreme at 100, sell at 101.

    Gross is 100 bps both ways. Net-9bps takes the round trip fee off. Net-stress enters a
    second late and pays the fee plus one full effective spread per side, 5 bps each way.
    The two sides do not land on the same number because the delayed entry is also the
    denominator: buying at 100.2 for an exit at 101 is 0.8 / 100.2, while selling at 99.8
    for an exit at 99 is 0.8 / 99.8.
    """
    trades = pl.DataFrame(
        {
            "side": ["long", "short"],
            "entry_px": [100.0, 100.0],
            "entry_px_delayed": [100.2, 99.8],
            "exit_px": [101.0, 99.0],
            "spread_bps": [5.0, 5.0],
        }
    )
    priced = lib.with_cost_columns(trades)
    assert priced["gross_bps"].to_list() == pytest.approx([100.0, 100.0])
    assert priced["net_9bps"].to_list() == pytest.approx([91.0, 91.0])
    assert priced["net_stress_bps"].to_list() == pytest.approx([60.8403, 61.1603], abs=1e-3)


def test_a_fade_that_keeps_going_the_wrong_way_is_reported_as_a_loss() -> None:
    """The sign rule, in the one direction a mistake would flatter every table."""
    trades = pl.DataFrame(
        {
            "side": ["long"],
            "entry_px": [100.0],
            "entry_px_delayed": [100.0],
            "exit_px": [99.0],
            "spread_bps": [0.0],
        }
    )
    priced = lib.with_cost_columns(trades)
    assert priced["gross_bps"][0] == pytest.approx(-100.0)
    assert priced["net_stress_bps"][0] == pytest.approx(-109.0)


def test_the_wider_of_the_two_spread_estimates_wins() -> None:
    """The archive samples once a minute; a cascade minute can be far worse than that."""
    frame = pl.DataFrame(
        {"archive_spread_bps": [3.0, 12.0, None], "trade_spread_bps": [8.0, 4.0, 6.0]}
    )
    assert lib.effective_spread_bps(frame)["spread_bps"].to_list() == [8.0, 12.0, 6.0]


def test_removing_the_top_clusters_drops_whole_clusters_not_the_best_episodes() -> None:
    """The mandatory rerun: if the headline lives in five sweeps, it is not an edge."""
    frame = pl.DataFrame(
        {
            "cluster": [0, 0, 1, 1, 2, 2],
            "net_stress_bps": [100.0, 90.0, 1.0, -1.0, 2.0, -2.0],
        }
    )
    trimmed = lib.drop_top_clusters(frame, "net_stress_bps", count=1)
    assert sorted(trimmed["cluster"].unique().to_list()) == [1, 2]
    assert trimmed.height == 4
