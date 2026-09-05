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


def test_the_streaming_daily_grid_agrees_with_build_grid_bar_for_bar(research_lake: Path) -> None:
    """daily_grid restates build_grid's bar definition inside a Parquet scan, so it has to
    produce the identical frame. Without this test the memory fix is free to quietly change
    what a bar means, which would change every number in the phase."""
    from liqfade.features.grid import build_grid

    day = "2026-03-01"
    coins = ["BTC", "ETH", "PEPE"]
    streamed = lib.daily_grid(research_lake, day, coins)
    trades = lib.windowed_trades(
        research_lake, day, {coin: [(lib.ms(day), lib.ms(day) + 86_400_000)] for coin in coins}
    )
    dense = build_grid(
        trades,
        None,
        start_ms=lib.ms(day),
        end_ms=lib.ms(day) + 86_400_000,
        step_ms=lib.GRID_STEP_MS,
    )
    columns = ["coin", "ts_ms", "mid", "notional_usd", "volume", "signed_notional", "n_trades"]
    assert streamed.select(columns).sort("coin", "ts_ms").to_dicts() == (
        dense.select(columns).sort("coin", "ts_ms").to_dicts()
    )


def test_the_day_scan_ignores_the_appledouble_files_an_exfat_drive_leaves(
    tmp_path: Path,
) -> None:
    """macOS writes ._name.parquet beside every file on the owner's USB lake. Path.glob
    returns them, and four kilobytes of resource fork is not a Parquet file."""
    day = tmp_path / "parquet" / "trades" / "date=2026-03-01"
    day.mkdir(parents=True)
    (day / "all_day.parquet").write_bytes(b"")
    (day / "._all_day.parquet").write_bytes(b"\x00\x05\x16\x07")
    found = lib.day_files(tmp_path, "trades", "2026-03-01")
    assert [path.name for path in found] == ["all_day.parquet"]


def test_asking_for_a_year_of_trades_raises_instead_of_swapping_the_machine_to_death() -> None:
    """The lake holds 1.79 billion trade rows. The first run died in swap asking for them."""
    with pytest.raises(ValueError, match="too large"):
        lib.load_table("anywhere", "trades", "2025-09-01", "2026-08-31")


def test_overlapping_windows_collapse_into_one_span() -> None:
    """A busy day's episode windows overlap; one predicate per episode is one scan per
    episode over fifty million rows."""
    assert lib.merge_windows([(0, 10), (5, 20), (30, 40)]) == [(0, 20), (30, 40)]
    assert lib.merge_windows([]) == []


def test_the_episode_grid_only_reads_the_coins_and_windows_it_needs(research_lake: Path) -> None:
    """The memory fix in one assertion: the grid covers the episode windows and nothing else.

    A grid that quietly widened to whole days would still produce correct numbers and would
    still kill an 8 GB machine on a year of a table with 1.79 billion rows in it.
    """
    episodes = pl.DataFrame(
        {
            "coin": ["BTC"],
            "side": ["long"],
            "start_ts": [1_772_330_000_000],
            "end_ts": [1_772_330_030_000],
        }
    )
    grid = lib.episode_grid(research_lake, episodes)
    assert not grid.is_empty()
    assert grid["coin"].unique().to_list() == ["BTC"]
    assert grid["ts_ms"].min() >= 1_772_330_000_000 - lib.LEAD_IN_S * 1000
    assert grid["ts_ms"].max() <= 1_772_330_030_000 + lib.TRAIL_S * 1000


def test_the_tape_spread_does_not_grow_with_the_episode_count(research_lake: Path) -> None:
    """`_tape_spread` used to join episodes to the grid on coin alone and filter afterwards.

    That is a cross product, and on a real month it is 22,000 episodes against millions of
    grid rows. This asserts the result is one row per episode however many share a coin,
    which the cross-product version also satisfied, and that it returns at all, which it did
    not.
    """
    grid = lib.daily_grid(research_lake, "2026-03-01", ["BTC"])
    episodes = pl.DataFrame(
        {
            "key": [f"BTC/long/{index}" for index in range(200)],
            "coin": ["BTC"] * 200,
            "extreme_ts": [1_772_326_000_000 + index * 60_000 for index in range(200)],
        }
    )
    spread = lib._tape_spread(grid, episodes)
    assert spread.height <= episodes.height
    assert spread["key"].n_unique() == spread.height


def test_the_episode_cache_refuses_to_answer_a_different_window(
    tmp_path: Path, research_lake: Path
) -> None:
    """A March run and a year run sharing an output directory used to answer each other's
    question: the second read the first's episodes and reported them under its own dates."""
    from liqfade.config.loader import load_settings

    settings = load_settings()
    cache = tmp_path / "episodes.parquet"
    first = lib.build_episodes(research_lake, "2026-03-01", "2026-03-01", settings, cache=cache)
    assert cache.exists()
    again = lib.build_episodes(research_lake, "2026-03-01", "2026-03-01", settings, cache=cache)
    assert again.height == first.height  # same window, cache reused
    other = lib.build_episodes(research_lake, "2026-03-02", "2026-03-02", settings, cache=cache)
    assert other.is_empty()  # different window, cache ignored and that day has no lake


def test_the_universe_admits_the_lakes_perps_and_nothing_with_a_colon(
    research_lake: Path,
) -> None:
    """Builder-deployed HIP-3 markets are not the venue this strategy trades.

    29% of the first year's qualifying episodes were `cash:AMZN`, `flx:GOLD`, `xyz:TSM` and
    the like, none of which has an asset_ctx row. See docs/DECISIONS.md, 2026-09-05.
    """
    from liqfade.config.loader import load_settings

    settings = load_settings()
    universes, listings = lib.daily_universe(
        research_lake, "2026-03-01", "2026-03-01", settings.universe
    )
    admitted = universes["2026-03-01"]
    assert {"BTC", "ETH", "PEPE"} <= admitted
    assert not any(":" in coin for coin in admitted)
    assert set(listings) == {"BTC", "ETH", "PEPE"}


def test_listing_age_is_measured_from_the_lake_and_not_from_the_padding(
    research_lake: Path,
) -> None:
    """The bug this test exists for: reading listing age off the two days of padding the
    rest of the pipeline uses made every coin two days old, which excluded the entire
    universe and would have silently emptied the year."""
    from liqfade.config.loader import load_settings

    settings = load_settings()
    _, listings = lib.daily_universe(research_lake, "2026-03-01", "2026-03-01", settings.universe)
    start = lib.ms("2026-03-01")
    ages = {coin: (start - first) / 86_400_000 for coin, first in listings.items()}
    assert min(ages.values()) >= settings.universe.min_listing_days


def test_an_episode_in_a_coin_with_no_asset_ctx_is_dropped_not_nulled(
    research_lake: Path,
) -> None:
    from liqfade.config.loader import load_settings

    settings = load_settings()
    episodes = pl.DataFrame(
        {
            "coin": ["BTC", "xyz:TSM"],
            "side": ["long", "long"],
            "start_ts": [lib.ms("2026-03-01") + 3_600_000] * 2,
            "end_ts": [lib.ms("2026-03-01") + 3_630_000] * 2,
        }
    )
    kept = lib._apply_universe(research_lake, episodes, "2026-03-01", "2026-03-01", settings)
    assert kept["coin"].to_list() == ["BTC"]
