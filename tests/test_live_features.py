"""The live feature engine, and the one test that matters: it agrees with the backtest.

CLAUDE.md's single most important design constraint is that strategy code runs against a
replay with zero changes. That only holds if the rows it sees live are the rows it would
have seen in a backtest, so the consistency test feeds one synthetic tape through both
pipelines and compares every field the enabled config path reads.
"""

from __future__ import annotations

import math
from typing import Any

import polars as pl
import pytest

from liqfade.backtest.features import build_feature_grid, iter_grid_rows
from liqfade.config import resolve
from liqfade.config.models import Settings
from liqfade.live.features import LiveFeatureEngine

STEP_MS = 1000
START = 1_700_000_000_000  # a multiple of 60_000, so minute samples land on the boundary


def crossvenue_settings(**dotted: Any) -> Settings:
    merged, _ = resolve("paper")
    merged["cascade"]["trigger"] = "crossvenue"
    for key, value in dotted.items():
        node = merged
        parts = key.split(".")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
    return Settings.model_validate(merged)


def make_tape(*, minutes: int = 12, coin: str = "ETH") -> dict[str, list[dict[str, Any]]]:
    """A deterministic tape: one trade and one quote a second, plus a burst of liquidations."""
    trades, bbo, liqs = [], [], []
    end = START + minutes * 60_000
    px = 3000.0
    for tick, ts in enumerate(range(START, end, 500), start=1):
        # A wobble with a sharp drop in the ninth minute, so ret and rv are not constant.
        px = 3000.0 + 4.0 * math.sin(tick / 9.0)
        if START + 8 * 60_000 <= ts < START + 9 * 60_000:
            px -= 40.0 * (ts - (START + 8 * 60_000)) / 60_000
        trades.append(
            {
                "venue": "hyperliquid",
                "coin": coin,
                "ts_ms": ts,
                "rx_ms": ts,
                "source": "recorder",
                "px": px,
                "sz": 0.5,
                "side": "buy" if tick % 3 else "sell",
                "tid": tick,
            }
        )
        bbo.append(
            {
                "coin": coin,
                "ts_ms": ts,
                "rx_ms": ts,
                "source": "recorder",
                "bid_px": px - 0.5,
                "bid_sz": 10.0,
                "ask_px": px + 0.5,
                "ask_sz": 12.0,
            }
        )
    for index in range(40):
        ts = START + 8 * 60_000 + index * 1_000
        liqs.append(
            {
                "venue": "bybit",
                "coin": coin,
                "venue_symbol": "ETHUSDT",
                "ts_ms": ts,
                "rx_ms": ts,
                "source": "recorder",
                "side": "long",
                "px": 2980.0,
                "sz": 30.0,
                "notional_usd": 90_000.0,
                "method": "market",
                "complete": True,
            }
        )
    # A trickle of shorts elsewhere, so the imbalance is not a constant 1.0.
    for index in range(6):
        ts = START + 3 * 60_000 + index * 10_000
        liqs.append(
            {
                "venue": "okx",
                "coin": coin,
                "venue_symbol": "ETH-USDT-SWAP",
                "ts_ms": ts,
                "rx_ms": ts,
                "source": "recorder",
                "side": "short",
                "px": 3010.0,
                "sz": 1.0,
                "notional_usd": 3_010.0,
                "method": "market",
                "complete": True,
            }
        )
    return {"trades": trades, "bbo": bbo, "liquidations": liqs}


def feed(
    engine: LiveFeatureEngine,
    tape: dict[str, list[dict[str, Any]]],
    until_ms: int,
    *,
    coin: str = "ETH",
) -> dict[int, Any]:
    """Replay the tape in receive order, ticking every grid step, capturing each row.

    Captured as it goes rather than read back afterwards: the engine answers about its
    newest closed bucket, so a row read at the end of the replay is the last row, whatever
    stamp it is asked for.
    """
    events: list[tuple[int, str, dict[str, Any]]] = []
    for table, rows in tape.items():
        for row in rows:
            events.append((row["rx_ms"], table, row))
    events.sort(key=lambda item: (item[0], item[1]))

    index = 0
    captured: dict[int, Any] = {}
    for now in range(START, until_ms + STEP_MS, STEP_MS):
        while index < len(events) and events[index][0] < now:
            _, table, row = events[index]
            engine.ingest(table, row.get("venue", "hyperliquid"), row)
            index += 1
        engine.advance(now)
        captured[now] = engine.row(coin, now)
    return captured


def test_mid_prefers_the_quote_and_falls_back_to_the_tape() -> None:
    engine = LiveFeatureEngine(crossvenue_settings())
    engine.ingest(
        "trades",
        "hyperliquid",
        {
            "coin": "ETH",
            "rx_ms": START + 100,
            "ts_ms": START,
            "px": 100.0,
            "sz": 1.0,
            "side": "buy",
        },
    )
    engine.advance(START + 1000)
    assert engine.row("ETH", START + 1000).mid == pytest.approx(100.0)

    engine.ingest(
        "bbo",
        "hyperliquid",
        {
            "coin": "ETH",
            "rx_ms": START + 1100,
            "ts_ms": START,
            "bid_px": 99.0,
            "ask_px": 101.0,
            "bid_sz": 1.0,
            "ask_sz": 1.0,
        },
    )
    engine.advance(START + 2000)
    assert engine.row("ETH", START + 2000).mid == pytest.approx(100.0)


def test_a_stale_quote_still_beats_a_fresh_print() -> None:
    """build_grid coalesces book_mid over trade_mid however old the book is. Match it."""
    engine = LiveFeatureEngine(crossvenue_settings())
    engine.ingest(
        "bbo",
        "hyperliquid",
        {
            "coin": "ETH",
            "rx_ms": START,
            "ts_ms": START,
            "bid_px": 90.0,
            "ask_px": 110.0,
            "bid_sz": 1.0,
            "ask_sz": 1.0,
        },
    )
    engine.advance(START + 1000)
    engine.ingest(
        "trades",
        "hyperliquid",
        {
            "coin": "ETH",
            "rx_ms": START + 1500,
            "ts_ms": START,
            "px": 500.0,
            "sz": 1.0,
            "side": "buy",
        },
    )
    engine.advance(START + 2000)
    assert engine.row("ETH", START + 2000).mid == pytest.approx(100.0)


def test_liquidations_are_summed_over_the_half_open_window_and_weighted() -> None:
    settings = crossvenue_settings(**{"venues.bybit.liq_weight": 2.0})
    engine = LiveFeatureEngine(settings)
    for ts in (START + 500, START + 1_500):
        engine.ingest(
            "liquidations",
            "bybit",
            {"coin": "ETH", "rx_ms": ts, "ts_ms": ts, "side": "long", "notional_usd": 1_000.0},
        )
    engine.advance(START + 2_000)

    row = engine.row("ETH", START + 2_000)
    assert row.liq_long_usd_w == pytest.approx(4_000.0)  # both, at weight two
    assert row.liq_short_usd_w == 0.0
    assert row.liq_imbalance_w == pytest.approx(1.0)

    # Sixty one seconds later the window has passed over both of them.
    engine.advance(START + 63_000)
    assert engine.row("ETH", START + 63_000).liq_long_usd_w == 0.0
    assert engine.row("ETH", START + 63_000).liq_imbalance_w == 0.0


def test_a_liquidation_on_a_venue_the_trigger_excludes_is_ignored() -> None:
    """cascade.trigger crossvenue reads Bybit, OKX and Binance. Never Hyperliquid's own."""
    engine = LiveFeatureEngine(crossvenue_settings())
    engine.ingest(
        "liquidations",
        "hyperliquid",
        {"coin": "ETH", "rx_ms": START, "ts_ms": START, "side": "long", "notional_usd": 1e6},
    )
    engine.advance(START + 1_000)
    assert engine.row("ETH", START + 1_000).liq_long_usd_w == 0.0


def test_book_features_come_from_the_last_snapshot_before_the_stamp() -> None:
    settings = crossvenue_settings()
    engine = LiveFeatureEngine(settings)
    for level, (bid, ask) in enumerate([(99.9, 100.1), (99.0, 101.0)]):
        engine.ingest(
            "l2",
            "hyperliquid",
            {
                "coin": "ETH",
                "rx_ms": START + 100,
                "ts_ms": START,
                "level": level,
                "bid_px": bid,
                "bid_sz": 10.0,
                "ask_px": ask,
                "ask_sz": 5.0,
            },
        )
    engine.advance(START + 1_000)

    row = engine.row("ETH", START + 1_000)
    assert row.spread_bps == pytest.approx(1e4 * 0.2 / 100.0, rel=1e-6)
    # Only the touch is within ten bps of a mid of 100; the far level is a whole point out.
    assert row.depth_bid == pytest.approx(99.9 * 10.0)
    assert row.depth_ask == pytest.approx(100.1 * 5.0)
    assert row.book_imbalance == pytest.approx((999.0 - 500.5) / (999.0 + 500.5))


def test_the_engine_agrees_with_the_backtest_grid_on_the_same_tape() -> None:
    """The constraint the whole design rests on, checked rather than asserted."""
    settings = crossvenue_settings()
    tape = make_tape(minutes=12)
    end = START + 12 * 60_000

    engine = LiveFeatureEngine(settings)
    live = feed(engine, tape, end)

    grid = build_feature_grid(
        settings=settings,
        trades=pl.DataFrame(tape["trades"]),
        liquidations=pl.DataFrame(tape["liquidations"]),
        bbo=pl.DataFrame(tape["bbo"]),
        # One step in, so both pipelines' first row is the same stamp. A grid starting
        # exactly on the tape's first millisecond gets a row (and, since START is a whole
        # minute, a z-score sample) at a moment a fresh live process has no history for.
        start_ms=START + STEP_MS,
        end_ms=end,
    )
    expected = {}
    for stamp, rows in iter_grid_rows(grid, settings, keep=pl.Series([True] * grid.height)):
        if "ETH" in rows:
            expected[stamp] = rows["ETH"]

    compared = 0
    for stamp, want in expected.items():
        got = live.get(stamp)
        # The grid's own first row exists before the engine has closed a bucket, which is
        # the one moment the two cannot agree: a fresh process has no history at all.
        if got is None or stamp <= START:
            continue
        compared += 1
        for field in (
            "mid",
            "liq_long_usd_w",
            "liq_short_usd_w",
            "liq_imbalance_w",
            "liq_long_usd_peak",
            "liq_short_usd_peak",
            "ret_w",
            "rv",
            "cvd_slope",
            "spread_bps",
            "depth_bid",
            "depth_ask",
            "book_imbalance",
            "traded_usd_vol_window",
            "liq_z_long_w",
            "liq_z_short_w",
        ):
            mine, theirs = getattr(got, field), getattr(want, field)
            if theirs is None:
                assert mine is None, f"{field} at {stamp}: live has {mine}, backtest has none"
            else:
                assert mine is not None, f"{field} at {stamp}: live has none, backtest {theirs}"
                assert mine == pytest.approx(theirs, rel=1e-9, abs=1e-9), f"{field} at {stamp}"
    assert compared > 300, f"only compared {compared} rows"


def test_the_z_is_none_until_the_lookback_holds_a_scale() -> None:
    engine = LiveFeatureEngine(crossvenue_settings())
    engine.advance(START + 1_000)
    assert engine.row("ETH", START + 1_000).liq_z_long_w is None


def test_the_engine_refuses_a_config_whose_features_it_cannot_supply() -> None:
    """Turning on oi_flush in paper must fail loudly, not silently block every intent."""
    settings = crossvenue_settings(**{"strategy.rules.oi_flush.enabled": True})
    reasons = LiveFeatureEngine.unsupported(settings)
    assert any("oi_flush" in reason for reason in reasons)
    with pytest.raises(ValueError, match="oi_flush"):
        LiveFeatureEngine(settings)


def test_the_default_crossvenue_config_is_fully_supported() -> None:
    assert LiveFeatureEngine.unsupported(crossvenue_settings()) == []


def test_buffers_stay_bounded_over_a_long_run() -> None:
    """A 24/7 process cannot grow a list per print. Everything here is a bounded deque."""
    engine = LiveFeatureEngine(crossvenue_settings())
    for index in range(20_000):
        ts = START + index * 500
        engine.ingest(
            "trades",
            "hyperliquid",
            {"coin": "ETH", "rx_ms": ts, "ts_ms": ts, "px": 100.0, "sz": 1.0, "side": "buy"},
        )
        if index % 2 == 0:
            engine.advance(ts)
    assert engine.buffer_rows("ETH") < 5_000


def test_a_liquidation_with_no_notional_is_dropped_not_counted_as_zero() -> None:
    """OKX sends these for contracts it has no price for. Counting one as zero is a lie."""
    engine = LiveFeatureEngine(crossvenue_settings())
    engine.ingest(
        "liquidations",
        "okx",
        {"coin": "ETH", "rx_ms": START, "ts_ms": START, "side": "long", "notional_usd": None},
    )
    engine.ingest(
        "liquidations",
        "okx",
        {
            "coin": "ETH",
            "rx_ms": START + 100,
            "ts_ms": START,
            "side": "long",
            "notional_usd": 5_000.0,
        },
    )
    engine.advance(START + 1_000)

    assert engine.row("ETH", START + 1_000).liq_long_usd_w == pytest.approx(5_000.0)


HOUR_MS = 3_600_000


def make_long_tape(*, hours: int, coin: str = "ETH") -> dict[str, list[dict[str, Any]]]:
    """A sparse tape long enough for the z-score's 24 hour lookback to roll over.

    One quote and one trade a minute rather than two a second: the z-score samples once a
    minute and what is being exercised here is the lookback, not the tape. Liquidations
    land on a schedule whose sizes change across the day, so a window that dropped the
    wrong samples would score the same minute differently from the backtest.
    """
    trades, bbo, liqs = [], [], []
    end = START + hours * HOUR_MS
    for minute, ts in enumerate(range(START, end, 60_000)):
        px = 3000.0 + 20.0 * math.sin(minute / 37.0)
        trades.append(
            {
                "venue": "hyperliquid",
                "coin": coin,
                "ts_ms": ts,
                "rx_ms": ts,
                "source": "recorder",
                "px": px,
                "sz": 0.5,
                "side": "buy" if minute % 3 else "sell",
                "tid": minute + 1,
            }
        )
        bbo.append(
            {
                "coin": coin,
                "ts_ms": ts,
                "rx_ms": ts,
                "source": "recorder",
                "bid_px": px - 0.5,
                "bid_sz": 10.0,
                "ask_px": px + 0.5,
                "ask_sz": 12.0,
            }
        )
        # Every seventh minute, at a size that drifts across the day. A lookback that kept
        # or dropped the wrong end of the window would centre and scale on the wrong
        # distribution, which is exactly what this is looking for.
        if minute % 7 == 0:
            liqs.append(
                {
                    "venue": "bybit",
                    "coin": coin,
                    "venue_symbol": "ETHUSDT",
                    "ts_ms": ts + 100,
                    "rx_ms": ts + 100,
                    "source": "recorder",
                    "side": "long" if minute % 14 == 0 else "short",
                    "px": px - 20.0,
                    "sz": 1.0,
                    "notional_usd": 10_000.0 + 500.0 * (minute % 23),
                    "method": "market",
                    "complete": True,
                }
            )
    return {"trades": trades, "bbo": bbo, "liquidations": liqs}


def test_the_engine_agrees_with_the_grid_after_the_z_lookback_has_rolled_over() -> None:
    """Twelve minutes never reaches the 24 hour lookback, so nothing was ever dropped.

    `_trim` is the only code that decides which samples the z-score's centre and scale are
    computed from, and until the window is full it does nothing at all. This runs 25 hours
    so that it does, and compares the z against the backtest grid on the far side of the
    rollover, where the two pipelines disagree if they trim differently.
    """
    settings = crossvenue_settings()
    hours = 25
    end = START + hours * HOUR_MS
    tape = make_long_tape(hours=hours)
    assert settings.features.z_lookback_h < hours, "the tape has to outlast the lookback"

    engine = LiveFeatureEngine(settings)
    events = sorted(
        ((row["rx_ms"], table, row) for table, rows in tape.items() for row in rows),
        key=lambda item: (item[0], item[1]),
    )
    # The last hour only: the interesting rows are the ones whose lookback has already
    # dropped samples, and capturing all 90,000 would be a slow way to compare 60.
    from_ms = end - HOUR_MS
    index = 0
    live: dict[int, Any] = {}
    for now in range(START, end + STEP_MS, STEP_MS):
        while index < len(events) and events[index][0] < now:
            _, table, row = events[index]
            engine.ingest(table, row.get("venue", "hyperliquid"), row)
            index += 1
        engine.advance(now)
        if now >= from_ms and now % 60_000 == 0:
            live[now] = engine.row("ETH", now)

    grid = build_feature_grid(
        settings=settings,
        trades=pl.DataFrame(tape["trades"]),
        liquidations=pl.DataFrame(tape["liquidations"]),
        bbo=pl.DataFrame(tape["bbo"]),
        start_ms=START + STEP_MS,
        end_ms=end,
    )
    compared = 0
    for stamp, rows in iter_grid_rows(grid, settings, keep=pl.Series([True] * grid.height)):
        want = rows.get("ETH")
        got = live.get(stamp)
        if want is None or got is None:
            continue
        compared += 1
        for field in ("liq_z_long_w", "liq_z_short_w", "liq_long_usd_w", "liq_short_usd_w"):
            mine, theirs = getattr(got, field), getattr(want, field)
            if theirs is None:
                assert mine is None, f"{field} at {stamp}: live has {mine}, backtest none"
            else:
                assert mine is not None, f"{field} at {stamp}: live none, backtest {theirs}"
                assert mine == pytest.approx(theirs, rel=1e-9, abs=1e-9), f"{field} at {stamp}"
    assert compared >= 30, f"only compared {compared} rows past the rollover"
    # And the lookback really did roll: a full window is z_lookback_h * 60 samples, so a
    # deque holding every sample the run produced would be half as long again.
    samples = engine.coins["ETH"].samples["long"]
    assert len(samples) <= settings.features.z_lookback_h * 60 + 1
    assert len(samples) < hours * 60


def test_a_one_sided_level_still_counts_the_side_that_is_there() -> None:
    """A thin book runs out on one side, and the parser sends None for the missing half.

    `_l2` emits `max(len(bids), len(asks))` levels, so a book with twenty bids and three
    asks produces seventeen rows whose ask fields are None. `_ingest_level` used to call
    `float()` on all four unconditionally, so the whole level was dropped, taking the bid
    that *was* there with it. Depth came out understated on exactly the illiquid coins
    where the depth cap matters most, and on testnet, where it fired on every snapshot.
    """
    engine = LiveFeatureEngine(crossvenue_settings())
    both = {
        "coin": "ETH",
        "ts_ms": START,
        "rx_ms": START,
        "level": 0,
        "bid_px": 3000.0,
        "bid_sz": 10.0,
        "ask_px": 3001.0,
        "ask_sz": 10.0,
    }
    engine.ingest("l2", "hyperliquid", both)
    engine.ingest(
        "l2",
        "hyperliquid",
        {
            **both,
            "level": 1,
            "bid_px": 2999.5,
            "bid_sz": 7.0,
            "ask_px": None,
            "ask_sz": None,
        },
    )
    engine.advance(START + 2_000)
    ragged = engine.row("ETH", START + 2_000)

    engine2 = LiveFeatureEngine(crossvenue_settings())
    engine2.ingest("l2", "hyperliquid", both)
    engine2.advance(START + 2_000)
    touch_only = engine2.row("ETH", START + 2_000)

    # A long lifts the asks, so the ragged level adds nothing there and everything on the
    # short side, where its bid sits.
    assert ragged.depth("short") > touch_only.depth("short"), "the bid at level one is real"
    assert ragged.depth("long") == touch_only.depth("long")
