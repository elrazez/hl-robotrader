"""Universe selection from a canned metaAndAssetCtxs payload."""

from __future__ import annotations

from typing import Any

from liqfade.config import load_settings
from liqfade.config.models import Universe
from liqfade.recorder.universe import select_universe

CFG: Universe = load_settings("paper").universe


def payload(*assets: tuple[str, float, float, float]) -> list[Any]:
    """(name, dayNtlVlm, openInterest in coin units, markPx) -> a metaAndAssetCtxs reply."""
    meta = {"universe": [{"name": name, "szDecimals": 3} for name, _, _, _ in assets]}
    ctxs = [
        {"dayNtlVlm": str(vlm), "openInterest": str(oi), "markPx": str(mark)}
        for _, vlm, oi, mark in assets
    ]
    return [meta, ctxs]


BIG = 1_000_000_000.0  # comfortably over both floors
OI = 1_000_000.0  # coin units, times markPx 100 = 100m USD


def test_top_n_by_volume_wins_and_the_core_is_always_in() -> None:
    cfg = CFG.model_copy(update={"top_n_by_volume": 2, "core_coins": ("BTC", "HYPE")})
    chosen = select_universe(
        payload(
            ("BTC", BIG * 3, OI, 100.0),
            ("ETH", BIG * 2, OI, 100.0),
            ("SOL", BIG, OI, 100.0),
            ("HYPE", BIG / 100, OI, 100.0),
        ),
        cfg,
    )
    assert chosen == ["BTC", "ETH", "HYPE"]  # SOL ranked third, HYPE is core


def test_the_volume_floor_excludes_a_thin_coin() -> None:
    cfg = CFG.model_copy(update={"core_coins": (), "min_day_ntl_vlm_usd": 20_000_000.0})
    chosen = select_universe(
        payload(("BTC", BIG, OI, 100.0), ("THIN", 1_000_000.0, OI, 100.0)), cfg
    )
    assert chosen == ["BTC"]


def test_the_open_interest_floor_is_measured_in_usd() -> None:
    cfg = CFG.model_copy(update={"core_coins": (), "min_open_interest_usd": 5_000_000.0})
    # 100 coins at $1 is $100 of OI, nowhere near the floor, however big the volume.
    chosen = select_universe(payload(("BTC", BIG, OI, 100.0), ("TINY", BIG, 100.0, 1.0)), cfg)
    assert chosen == ["BTC"]


def test_exclude_beats_everything_including_the_core_list() -> None:
    cfg = CFG.model_copy(update={"core_coins": ("BTC", "ETH"), "exclude": ("ETH",)})
    chosen = select_universe(payload(("BTC", BIG, OI, 100.0), ("ETH", BIG, OI, 100.0)), cfg)
    assert chosen == ["BTC"]


def test_a_delisted_asset_is_never_selected() -> None:
    cfg = CFG.model_copy(update={"core_coins": ()})
    data = payload(("BTC", BIG, OI, 100.0), ("DEAD", BIG * 5, OI, 100.0))
    data[0]["universe"][1]["isDelisted"] = True
    assert select_universe(data, cfg) == ["BTC"]


def test_missing_context_fields_do_not_crash_the_refresh() -> None:
    cfg = CFG.model_copy(update={"core_coins": ("BTC",)})
    data = [{"universe": [{"name": "BTC"}, {"name": "WEIRD"}]}, [{}, {"dayNtlVlm": None}]]
    assert select_universe(data, cfg) == ["BTC"]


def test_the_result_is_sorted_so_a_reorder_is_not_a_universe_change() -> None:
    cfg = CFG.model_copy(update={"core_coins": ("SOL", "BTC"), "top_n_by_volume": 1})
    chosen = select_universe(payload(("ETH", BIG, OI, 100.0)), cfg)
    assert chosen == sorted(chosen) == ["BTC", "ETH", "SOL"]
