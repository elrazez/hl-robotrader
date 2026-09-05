"""Universe selection from a canned metaAndAssetCtxs payload."""

from __future__ import annotations

from typing import Any

import orjson

from liqfade.config import load_settings
from liqfade.config.models import Universe
from liqfade.recorder.universe import UniverseManager, select_universe

CFG: Universe = load_settings(
    "paper", environ={}
).universe  # environ={}: these thresholds are the test's, not the box's .env or overrides.yaml


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


# --- applying a refresh --------------------------------------------------------

NO_CORE = CFG.model_copy(update={"core_coins": ()})  # so the payload alone is the universe


class FakeInfo:
    """An httpx.AsyncClient stand-in that answers /info with one canned payload."""

    def __init__(self, coins: list[str]) -> None:
        self.coins = coins

    async def post(self, *_args: Any, **_kwargs: Any) -> Any:
        body = payload(*[(coin, BIG, OI, 100.0) for coin in self.coins])

        class Reply:
            @staticmethod
            def raise_for_status() -> None: ...

            content = orjson.dumps(body)

            @staticmethod
            def json() -> Any:
                return body

        return Reply()


async def test_a_refusal_to_apply_leaves_the_universe_unchanged_so_it_is_retried() -> None:
    """`self.coins` used to advance before on_change ran, so a half applied refresh
    looked done and the next tick returned False without re-trying the venues that
    missed it. They then under-recorded that coin until a restart."""
    taken: list[list[str]] = []

    async def refuse(coins: list[str]) -> bool:
        taken.append(list(coins))
        return False

    manager = UniverseManager(NO_CORE, "https://example.invalid", on_change=refuse)
    client = FakeInfo(["BTC", "ETH"])
    assert await manager.refresh(client) is False
    assert manager.coins == []
    # Still not applied, so the next refresh tries again rather than reporting no change.
    assert await manager.refresh(client) is False
    assert taken == [["BTC", "ETH"], ["BTC", "ETH"]]


async def test_a_refresh_every_stream_took_advances_the_shared_state() -> None:
    async def accept(_coins: list[str]) -> bool:
        return True

    manager = UniverseManager(NO_CORE, "https://example.invalid", on_change=accept)
    client = FakeInfo(["BTC", "ETH"])
    assert await manager.refresh(client) is True
    assert manager.coins == ["BTC", "ETH"]
    assert await manager.refresh(client) is False  # nothing changed, nothing to send
