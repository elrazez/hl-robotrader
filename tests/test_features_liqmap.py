"""The projected liquidation map: bands from anchors, their weights, and what a query sees.

The arithmetic is the only thing here that could be silently wrong and still look
plausible, so every band price in these tests is worked out by hand in the docstring rather
than recomputed the way the code does it.
"""

from __future__ import annotations

import polars as pl
import pytest

from liqfade.features.liqmap import band_weights, ladder_bands, map_features

KNOWN_FROM = 1_700_000_780_000
ANCHOR_TS = 1_700_000_000_000


def _levels(*, kind: str, px: float, coin: str = "BTC", **over: object) -> pl.DataFrame:
    """One row shaped exactly as `features.levels.swing_levels` returns."""
    row: dict[str, object] = {
        "coin": coin,
        "ts_ms": ANCHOR_TS,
        "px": px,
        "kind": kind,
        "known_from": KNOWN_FROM,
        "source": "swing",
    }
    row.update(over)
    return pl.DataFrame({key: [value] for key, value in row.items()})


def test_a_resistance_anchor_projects_its_ladder_below_itself() -> None:
    """A swing high traps longs, so the prices they are force-sold at sit below it.
    At 10x with a 0.5% maintenance margin that is 1/10 - 0.005 = 9.5% down from 100,
    which is 90.5."""
    bands = ladder_bands(
        _levels(kind="resistance", px=100.0), ladder=[10.0], mm_frac=0.005, max_dist_bps=1200.0
    )
    assert bands.height == 1
    band = bands.row(0, named=True)
    assert band["px"] == pytest.approx(90.5)
    assert band["side"] == "long_liq"
    assert band["anchor_px"] == 100.0
    assert band["leverage"] == 10.0
    assert band["known_from"] == KNOWN_FROM


def test_a_support_anchor_projects_its_ladder_above_itself() -> None:
    """A swing low traps shorts, and buying them back is what lifts price: 100 at 10x is
    liquidated 9.5% up, at 109.5."""
    bands = ladder_bands(
        _levels(kind="support", px=100.0), ladder=[10.0], mm_frac=0.005, max_dist_bps=1200.0
    )
    assert bands.height == 1
    band = bands.row(0, named=True)
    assert band["px"] == pytest.approx(109.5)
    assert band["side"] == "short_liq"


def test_a_rung_further_out_than_the_cap_is_dropped() -> None:
    """3x sits 1/3 - 0.005 = 32.8% away, which is 3283 bps and past a 1000 bps cap. 10x
    sits at 950 and stays. The cap is what stops a low rung projecting a band on the
    other side of the market."""
    bands = ladder_bands(
        _levels(kind="resistance", px=100.0),
        ladder=[3.0, 10.0],
        mm_frac=0.005,
        max_dist_bps=1000.0,
    )
    assert bands["leverage"].to_list() == [10.0]


def test_a_rung_inside_the_maintenance_margin_projects_nothing() -> None:
    """At 200x the margin is gone before the position is: 1/200 = 0.5% is exactly the
    maintenance margin, so there is no distance to project and no band to draw."""
    bands = ladder_bands(
        _levels(kind="resistance", px=100.0),
        ladder=[200.0, 10.0],
        mm_frac=0.005,
        max_dist_bps=1200.0,
    )
    assert bands["leverage"].to_list() == [10.0]


def test_known_from_is_the_anchors_own_and_is_never_recomputed() -> None:
    """The band exists exactly when its anchor became knowable, not when price reached it.
    Recomputing it here would reintroduce the lookahead levels.py exists to prevent."""
    levels = pl.concat(
        [
            _levels(kind="resistance", px=100.0, known_from=111),
            _levels(kind="support", px=50.0, known_from=222),
        ]
    )
    bands = ladder_bands(levels, ladder=[10.0, 20.0], mm_frac=0.005, max_dist_bps=1200.0)
    assert bands.filter(pl.col("anchor_px") == 100.0)["known_from"].to_list() == [111, 111]
    assert bands.filter(pl.col("anchor_px") == 50.0)["known_from"].to_list() == [222, 222]


def _oi(start: float, end: float, coin: str = "BTC") -> pl.DataFrame:
    """Two `asset_ctx` samples: open interest at the anchor bar, and at its confirmation."""
    return pl.DataFrame(
        {
            "coin": [coin, coin],
            "ts_ms": [ANCHOR_TS, KNOWN_FROM],
            "open_interest": [start, end],
        }
    )


def test_the_weight_is_the_interest_that_opened_at_the_anchor_split_by_rung() -> None:
    """Open interest rose 100 coins while the anchor was forming, at an anchor price of
    100, so $10,000 of positions opened there. The histogram says 40% of notional is at
    10x and 60% at 20x, so those two bands carry $4,000 and $6,000."""
    bands = ladder_bands(
        _levels(kind="resistance", px=100.0),
        ladder=[10.0, 20.0],
        mm_frac=0.005,
        max_dist_bps=1200.0,
    )
    weighted = band_weights(
        bands, _oi(500.0, 600.0), histogram={10.0: 0.4, 20.0: 0.6}, half_life_min=60.0
    )
    by_rung = dict(zip(weighted["leverage"], weighted["weight_usd"], strict=True))
    assert by_rung[10.0] == pytest.approx(4_000.0)
    assert by_rung[20.0] == pytest.approx(6_000.0)


def test_an_anchor_that_opened_no_new_interest_weighs_zero_not_null() -> None:
    """Open interest fell while the anchor formed, so nobody was trapped there. Zero, not
    null: a null would drop the band out of every sum it belongs in and quietly make the
    fuel ahead of a cascade look smaller than it is."""
    bands = ladder_bands(
        _levels(kind="resistance", px=100.0), ladder=[10.0], mm_frac=0.005, max_dist_bps=1200.0
    )
    weighted = band_weights(bands, _oi(500.0, 400.0), histogram={10.0: 1.0}, half_life_min=60.0)
    assert weighted["weight_usd"].to_list() == [0.0]


def _band(
    px: float,
    *,
    ts_ms: int = ANCHOR_TS,
    known_from: int = KNOWN_FROM,
    anchor_px: float = 100.0,
    leverage: float = 10.0,
    weight_usd: float = 0.0,
    half_life_min: float = 30.0,
    side: str = "long_liq",
    coin: str = "BTC",
) -> dict[str, object]:
    """One weighted band, built by hand so map_features is tested on its own."""
    return {
        "coin": coin,
        "ts_ms": ts_ms,
        "px": px,
        "side": side,
        "known_from": known_from,
        "anchor_px": anchor_px,
        "leverage": leverage,
        "source": "swing",
        "weight_usd": weight_usd,
        "half_life_min": half_life_min,
    }


def _query(px: float, *, ts_ms: int = KNOWN_FROM + 1, key: str = "q1") -> pl.DataFrame:
    return pl.DataFrame({"key": [key], "coin": ["BTC"], "ts_ms": [ts_ms], "px": [px]})


def test_the_nearest_band_each_side_is_reported_with_its_rung() -> None:
    """95 is 500 bps below 100 and 104 is 400 above, so the nearer band is the one above
    and its rung is the one a caller reads."""
    bands = pl.DataFrame([_band(95.0, leverage=10.0), _band(104.0, leverage=25.0)])
    found = map_features(bands, _query(100.0), band_width_bps=15.0, fuel_window_bps=600.0).row(
        0, named=True
    )
    assert found["dist_to_band_below_bps"] == pytest.approx(500.0)
    assert found["dist_to_band_above_bps"] == pytest.approx(400.0)
    assert found["in_band"] is False
    assert found["band_leverage"] == 25.0


def test_bands_stacked_counts_anchors_and_never_one_anchor_twice() -> None:
    """Confluence is the whole claim, so it has to be anchors agreeing rather than one
    anchor's own adjacent rungs. A resistance at 100 puts 20x at 95.5 and 25x at 96.5, both
    52 bps from a query at 96; a second anchor at 101 puts its 20x at 96.455. That is three
    bands inside the width and two anchors, so the answer is two, not three."""
    one = ladder_bands(
        _levels(kind="resistance", px=100.0),
        ladder=[20.0, 25.0],
        mm_frac=0.005,
        max_dist_bps=1200.0,
    )
    two = ladder_bands(
        _levels(kind="resistance", px=101.0, ts_ms=ANCHOR_TS + 1),
        ladder=[20.0, 25.0],
        mm_frac=0.005,
        max_dist_bps=1200.0,
    )
    weighted = band_weights(
        pl.concat([one, two]), _oi(0.0, 0.0), histogram={20.0: 0.5, 25.0: 0.5}, half_life_min=30.0
    )
    alone = map_features(
        band_weights(one, _oi(0.0, 0.0), histogram={20.0: 0.5, 25.0: 0.5}, half_life_min=30.0),
        _query(96.0),
        band_width_bps=60.0,
        fuel_window_bps=600.0,
    ).row(0, named=True)
    both = map_features(weighted, _query(96.0), band_width_bps=60.0, fuel_window_bps=600.0).row(
        0, named=True
    )
    assert alone["bands_stacked"] == 1
    assert both["bands_stacked"] == 2
    assert both["in_band"] is True


def test_a_band_whose_anchor_is_not_yet_knowable_is_invisible() -> None:
    """The band exists when its anchor does, not when its price is reached. A query one
    millisecond before `known_from` must see nothing at all."""
    bands = pl.DataFrame([_band(99.9, known_from=KNOWN_FROM)])
    found = map_features(
        bands, _query(100.0, ts_ms=KNOWN_FROM - 1), band_width_bps=15.0, fuel_window_bps=600.0
    ).row(0, named=True)
    assert found["dist_to_band_below_bps"] is None
    assert found["in_band"] is False
    assert found["bands_stacked"] == 0
    assert found["fuel_below_usd"] == 0.0


def test_nothing_after_the_query_can_change_what_the_query_saw() -> None:
    """The no-lookahead property test: mutate every row whose anchor bar is at or after
    `t` and the answer at `t` must be byte-identical.

    A band becomes visible at its anchor's `known_from` inclusive, which is the same
    boundary `levels.zone_features` uses, and an anchor bar at `t` is confirmed strictly
    later than `t` because confirmation costs `fractal_n` more bars.
    """
    at_t = KNOWN_FROM + 5
    visible = [_band(99.0, weight_usd=1_000.0), _band(101.0, weight_usd=2_000.0)]
    later = [
        _band(99.5, ts_ms=at_t, known_from=at_t + 780_000, weight_usd=9e9),
        _band(100.2, ts_ms=at_t + 60_000, known_from=at_t + 840_000, weight_usd=9e9),
    ]
    before = map_features(
        pl.DataFrame(visible + later),
        _query(100.0, ts_ms=at_t),
        band_width_bps=15.0,
        fuel_window_bps=600.0,
    )
    mutated = [row | {"px": row["px"] * 1.5, "weight_usd": 1.0} for row in later]
    after = map_features(
        pl.DataFrame(visible + mutated),
        _query(100.0, ts_ms=at_t),
        band_width_bps=15.0,
        fuel_window_bps=600.0,
    )
    assert before.equals(after)


def test_fuel_is_the_decayed_weight_inside_the_window_on_each_side() -> None:
    """A band twice its half life old carries a quarter of its weight: $1,000 opened at an
    anchor 60 minutes ago, at a 30 minute half life, is $250 of fuel. The band 700 bps away
    is outside a 600 bps window and contributes nothing at all."""
    old = KNOWN_FROM
    now = old + 60 * 60_000
    bands = pl.DataFrame(
        [
            _band(99.0, known_from=old, weight_usd=1_000.0, half_life_min=30.0),
            _band(107.0, known_from=old, weight_usd=5_000.0, half_life_min=30.0),
        ]
    )
    found = map_features(
        bands, _query(100.0, ts_ms=now), band_width_bps=15.0, fuel_window_bps=600.0
    ).row(0, named=True)
    assert found["fuel_below_usd"] == pytest.approx(250.0)
    assert found["fuel_above_usd"] == 0.0


def test_a_band_confirmed_in_the_very_millisecond_of_the_query_is_visible() -> None:
    """The boundary, pinned so it cannot drift: `known_from` is when the level first
    exists, so a query at exactly that millisecond may use it. One millisecond earlier it
    may not, which the invisible-anchor test above pins from the other side."""
    bands = pl.DataFrame([_band(99.0, known_from=KNOWN_FROM, weight_usd=1_000.0)])
    found = map_features(
        bands, _query(100.0, ts_ms=KNOWN_FROM), band_width_bps=15.0, fuel_window_bps=600.0
    ).row(0, named=True)
    assert found["dist_to_band_below_bps"] == pytest.approx(100.0)
