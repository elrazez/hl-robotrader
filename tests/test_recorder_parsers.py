"""Per venue parser tests against payloads captured from the live feeds.

Every byte string here came off the wire on 2026-08-30 through
`scripts/probe/ws_payloads.py`, trimmed only in the number of entries so the file stays
readable. Testing against a doc rather than against the wire is how a recorder quietly
writes nulls for a month, so these are the real shapes.

Every parsed row is also pushed through the canonical schema, which catches a parser that
invents a column or a type the lake cannot hold.
"""

from __future__ import annotations

from typing import Any

import pytest

from liqfade.data.schemas import SOURCE_RECORDER, rows_to_table, validate_table
from liqfade.recorder.streams import binance, bybit, hl, okx

RX = 1788098890000

HL_TRADES = (
    b'{"channel":"trades","data":[{"coin":"BTC","side":"A","px":"78862.0","sz":"0.16711",'
    b'"time":1788098879496,"hash":"0xf1a7f7bdc8ea175af32104434f781502026d00a363ed362d9570a31087edf145",'
    b'"tid":493181723320635,"users":["0x49dfb8ddbd3ba96c0699a3f120e71f2516c345d8",'
    b'"0x3a62fea9cd2832f639f13d6a5bc5584f62c470e4"]},'
    b'{"coin":"BTC","side":"B","px":"78863.0","sz":"0.00018","time":1788098879500,'
    b'"hash":"0xf1a7","tid":375972075729353,"users":["0x4c69","0x3a62"]}]}'
)
HL_BBO = (
    b'{"channel":"bbo","data":{"coin":"BTC","time":1788098889667,"bbo":['
    b'{"px":"78862.0","sz":"0.99945","n":17},{"px":"78863.0","sz":"8.72967","n":24}]}}'
)
HL_L2 = (
    b'{"channel":"l2Book","data":{"coin":"BTC","time":1788098889119,"levels":[['
    b'{"px":"78862.0","sz":"0.99945","n":17},{"px":"78861.0","sz":"0.15357","n":3},'
    b'{"px":"78860.0","sz":"0.8284","n":3}],['
    b'{"px":"78863.0","sz":"8.16618","n":21},{"px":"78864.0","sz":"0.82483","n":3},'
    b'{"px":"78866.0","sz":"0.17726","n":3}]]}}'
)
HL_CANDLE = (
    b'{"channel":"candle","data":{"t":1788098880000,"T":1788098939999,"s":"BTC","i":"1m",'
    b'"o":"78862.0","c":"78863.0","h":"78863.0","l":"78862.0","v":"0.81106","n":21}}'
)
HL_ASSET_CTX = (
    b'{"channel":"activeAssetCtx","data":{"coin":"BTC","ctx":{"funding":"0.0000125",'
    b'"openInterest":"37332.39986","prevDayPx":"77661.0","dayNtlVlm":"1224472984.7925646305",'
    b'"premium":"-0.0002484705","oraclePx":"78882.6","markPx":"78862.0","midPx":"78862.5",'
    b'"impactPxs":["78862.0","78863.0"],"dayBaseVlm":"15666.2623"}}}'
)
HL_SUB_ACK = (
    b'{"channel":"subscriptionResponse","data":{"method":"subscribe",'
    b'"subscription":{"type":"trades","coin":"BTC"}}}'
)
HL_PONG = b'{"channel":"pong"}'

BYBIT_LONG_LIQ = (
    b'{"topic":"allLiquidation.BTCUSDT","type":"snapshot","ts":1788099227482,'
    b'"data":[{"T":1788099227003,"s":"BTCUSDT","S":"Sell","v":"0.003","p":"79149.90"}]}'
)
BYBIT_SHORT_LIQ = (
    b'{"topic":"allLiquidation.SOLUSDT","type":"snapshot","ts":1788099600478,'
    b'"data":[{"T":1788099600033,"s":"SOLUSDT","S":"Buy","v":"46.6","p":"106.160"}]}'
)
BYBIT_ACK = b'{"success":true,"ret_msg":"","conn_id":"da7tn","req_id":"","op":"subscribe"}'

BINANCE_LIQ = (
    b'{"e":"forceOrder","E":1788099227500,"o":{"s":"BTCUSDT","S":"SELL","o":"LIMIT",'
    b'"f":"IOC","q":"0.014","p":"79100.00","ap":"79095.10","X":"FILLED","l":"0.014",'
    b'"z":"0.014","T":1788099227480}}'
)
BINANCE_UNFILLED = (
    b'{"e":"forceOrder","E":1788099227500,"o":{"s":"1000PEPEUSDT","S":"BUY","o":"LIMIT",'
    b'"f":"IOC","q":"5000","p":"0.00812","ap":"0","X":"NEW","l":"0","z":"0",'
    b'"T":1788099227480}}'
)

OKX_LIQ = (
    b'{"arg":{"channel":"liquidation-orders","instType":"SWAP"},"data":[{"details":['
    b'{"bkLoss":"0","bkPx":"0.05613","ccy":"","posSide":"long","side":"sell","sz":"10",'
    b'"ts":"1788100208031"}],"instFamily":"ZKP-USDT","instId":"ZKP-USDT-SWAP",'
    b'"instType":"SWAP","uly":"ZKP-USDT"}]}'
)
OKX_INVERSE_LIQ = (
    b'{"arg":{"channel":"liquidation-orders","instType":"SWAP"},"data":[{'
    b'"details":[{"bkLoss":"0","bkPx":"79000.1","ccy":"BTC","posSide":"long","side":"sell",'
    b'"sz":"3","ts":"1788099600123"}],"instFamily":"BTC-USD","instId":"BTC-USD-SWAP",'
    b'"instType":"SWAP","uly":"BTC-USD"}]}'
)
OKX_ACK = (
    b'{"event":"subscribe","arg":{"channel":"liquidation-orders","instType":"SWAP"},'
    b'"connId":"59f84694"}'
)


def rows_for(parsed: list[tuple[str, dict[str, Any] | None]], table: str) -> list[dict[str, Any]]:
    """Rows for one table, after proving they fit the canonical schema.

    A `None` row is a row the parser refused, not a row; `_drops` is what asserts on those.
    """
    rows = [row for name, row in parsed if name == table and row is not None]
    if rows:
        validate_table(table, rows_to_table(table, rows, SOURCE_RECORDER))
    return rows


# --- hyperliquid ---------------------------------------------------------------


def test_hl_trades_carry_the_aggressor_side() -> None:
    rows = rows_for(hl.make_parser()(HL_TRADES, RX), "trades")
    assert len(rows) == 2
    assert rows[0] == {
        "venue": "hyperliquid",
        "coin": "BTC",
        "ts_ms": 1788098879496,
        "rx_ms": RX,
        "px": 78862.0,
        "sz": 0.16711,
        "side": "sell",  # A is the ask side, so the aggressor sold
        "tid": 493181723320635,
    }
    assert rows[1]["side"] == "buy"


def test_hl_bbo() -> None:
    (row,) = rows_for(hl.make_parser()(HL_BBO, RX), "bbo")
    assert row == {
        "coin": "BTC",
        "ts_ms": 1788098889667,
        "rx_ms": RX,
        "bid_px": 78862.0,
        "bid_sz": 0.99945,
        "ask_px": 78863.0,
        "ask_sz": 8.72967,
    }


def test_hl_l2_emits_one_row_per_level() -> None:
    rows = rows_for(hl.make_parser()(HL_L2, RX), "l2")
    assert [row["level"] for row in rows] == [0, 1, 2]
    assert rows[0]["bid_px"] == 78862.0
    assert rows[0]["ask_px"] == 78863.0
    assert rows[2]["ask_sz"] == 0.17726
    assert {row["ts_ms"] for row in rows} == {1788098889119}


def test_hl_l2_tolerates_one_empty_side() -> None:
    thin = (
        b'{"channel":"l2Book","data":{"coin":"BTC","time":1,'
        b'"levels":[[{"px":"1","sz":"2","n":1}],[]]}}'
    )
    (row,) = rows_for(hl.make_parser()(thin, RX), "l2")
    assert row["bid_px"] == 1.0
    assert row["ask_px"] is None


def test_hl_candle_uses_the_symbol_field_for_the_coin() -> None:
    parse = hl.make_parser()
    assert parse(HL_CANDLE, RX) == []  # held until the next bar proves this one closed
    next_bar = HL_CANDLE.replace(b'"t":1788098880000', b'"t":1788098940000')
    (row,) = rows_for(parse(next_bar, RX), "candles_1m")
    assert row == {
        "coin": "BTC",
        "ts_ms": 1788098880000,
        "o": 78862.0,
        "h": 78863.0,
        "l": 78862.0,
        "c": 78863.0,
        "v": 0.81106,
        "n": 21,
    }


def test_hl_asset_ctx_stamps_receive_time_because_the_venue_sends_none() -> None:
    (row,) = rows_for(hl.make_parser()(HL_ASSET_CTX, RX), "asset_ctx")
    assert row["ts_ms"] == RX == row["rx_ms"]
    assert row["mark_px"] == 78862.0
    assert row["oracle_px"] == 78882.6
    assert row["mid_px"] == 78862.5
    assert row["open_interest"] == 37332.39986
    assert row["premium"] == -0.0002484705


def test_hl_asset_ctx_carries_the_impact_prices_the_archive_also_has() -> None:
    """`impactPxs` is [bid, ask]: the same spread signal live and for the year."""
    (row,) = rows_for(hl.make_parser()(HL_ASSET_CTX, RX), "asset_ctx")
    assert (row["impact_bid_px"], row["impact_ask_px"]) == (78862.0, 78863.0)


@pytest.mark.parametrize("impact", [b'"impactPxs":[],', b'"impactPxs":["78862.0"],', b""])
def test_hl_asset_ctx_leaves_a_missing_impact_price_null_rather_than_zero(
    impact: bytes,
) -> None:
    """A coin with an empty side sends a short list or none at all."""
    payload = HL_ASSET_CTX.replace(b'"impactPxs":["78862.0","78863.0"],', impact)
    (row,) = rows_for(hl.make_parser()(payload, RX), "asset_ctx")
    assert row["impact_ask_px"] is None


@pytest.mark.parametrize("payload", [HL_SUB_ACK, HL_PONG])
def test_hl_control_frames_produce_no_rows(payload: bytes) -> None:
    assert hl.make_parser()(payload, RX) == []


def test_hl_subscribes_every_channel_for_every_coin() -> None:
    frames = hl.on_connect(["BTC", "ETH"])
    assert len(frames) == 2 * len(hl.CHANNEL_TABLES)
    assert {f["method"] for f in frames} == {"subscribe"}
    candle = next(f for f in frames if f["subscription"]["type"] == "candle")
    assert candle["subscription"] == {"type": "candle", "coin": "BTC", "interval": "1m"}


def test_hl_universe_change_subscribes_the_new_and_unsubscribes_the_dropped() -> None:
    frames = hl.on_universe_change(["ETH"], ["BTC"])
    methods = {(f["method"], f["subscription"]["coin"]) for f in frames}
    assert methods == {("subscribe", "ETH"), ("unsubscribe", "BTC")}


# --- bybit ---------------------------------------------------------------------


def test_bybit_sell_means_a_long_was_liquidated() -> None:
    (row,) = rows_for(bybit.make_parser(True)(BYBIT_LONG_LIQ, RX), "liquidations")
    assert row == {
        "venue": "bybit",
        "coin": "BTC",
        "venue_symbol": "BTCUSDT",
        "ts_ms": 1788099227003,
        "rx_ms": RX,
        "side": "long",
        "px": 79149.90,
        "sz": 0.003,
        "notional_usd": pytest.approx(237.4497),
        "method": "unknown",
        "complete": True,
    }


def test_bybit_buy_means_a_short_was_liquidated() -> None:
    (row,) = rows_for(bybit.make_parser(True)(BYBIT_SHORT_LIQ, RX), "liquidations")
    assert row["side"] == "short"
    assert row["coin"] == "SOL"


def test_bybit_ack_produces_no_rows() -> None:
    assert bybit.make_parser(True)(BYBIT_ACK, RX) == []


def test_bybit_subscribes_in_batches() -> None:
    on_connect, on_change = bybit.make_frames(2)
    frames = on_connect(["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    assert [len(f["args"]) for f in frames] == [2, 1]
    assert frames[0]["args"] == ["allLiquidation.BTCUSDT", "allLiquidation.ETHUSDT"]
    assert [f["op"] for f in on_change(["XRPUSDT"], ["BTCUSDT"])] == ["subscribe", "unsubscribe"]


# --- binance -------------------------------------------------------------------


def test_binance_sell_means_a_long_was_liquidated_and_uses_the_average_price() -> None:
    (row,) = rows_for(binance.make_parser(False)(BINANCE_LIQ, RX), "liquidations")
    assert row["side"] == "long"
    assert row["coin"] == "BTC"
    assert row["venue_symbol"] == "BTCUSDT"
    assert row["ts_ms"] == 1788099227480
    assert row["px"] == 79095.10
    assert row["notional_usd"] == pytest.approx(0.014 * 79095.10)
    # The venue only publishes the largest order per symbol per second.
    assert row["complete"] is False


def test_binance_falls_back_to_the_order_price_before_a_fill() -> None:
    (row,) = rows_for(binance.make_parser(False)(BINANCE_UNFILLED, RX), "liquidations")
    assert row["px"] == 0.00812
    assert row["side"] == "short"
    assert row["coin"] == "kPEPE"


def test_binance_ignores_frames_that_are_not_liquidations() -> None:
    assert binance.make_parser(False)(b'{"e":"listenKeyExpired","E":1}', RX) == []


# --- okx -----------------------------------------------------------------------


def test_okx_uses_the_liquidated_position_side() -> None:
    (row,) = rows_for(okx.make_parser(False)(OKX_LIQ, RX), "liquidations")
    assert row["side"] == "long"
    assert row["coin"] == "ZKP"
    assert row["venue_symbol"] == "ZKP-USDT-SWAP"
    assert row["ts_ms"] == 1788100208031
    assert row["px"] == 0.05613


def test_okx_inverse_contracts_are_recorded_without_a_coin() -> None:
    """Their size is in USD contracts, so they must not join the linear numbers."""
    (row,) = rows_for(okx.make_parser(False)(OKX_INVERSE_LIQ, RX), "liquidations")
    assert row["coin"] is None
    assert row["venue_symbol"] == "BTC-USD-SWAP"
    assert row["side"] == "long"


def test_okx_one_way_mode_falls_back_to_the_order_side() -> None:
    net = OKX_LIQ.replace(b'"posSide":"long"', b'"posSide":"net"')
    (row,) = rows_for(okx.make_parser(False)(net, RX), "liquidations")
    assert row["side"] == "long"  # the engine sold, so it was closing a long


def test_okx_ack_and_pong_produce_no_rows() -> None:
    assert okx.make_parser(False)(OKX_ACK, RX) == []
    assert okx.make_parser(False)(b"pong", RX) == []


def test_okx_notional_is_null_because_sz_counts_contracts_not_coins() -> None:
    """`px * sz` on OKX is out by `ctVal`, up to 100x. Null beats a wrong currency."""
    (row,) = rows_for(okx.make_parser(False)(OKX_LIQ, RX), "liquidations")
    assert row["notional_usd"] is None
    assert (row["px"], row["sz"]) == (0.05613, 10.0)


# --- unknown side tokens are dropped, never flipped ----------------------------
#
# Every one of these used to be a two-branch default, so a venue re-casing or renaming
# its side token silently wrote the opposite side. A flipped sign inverts the strategy
# and survives into a backtest looking like signal, so the row is dropped and counted.


def _drops(parsed: list[tuple[str, dict[str, Any] | None]]) -> list[str]:
    """The tables a parser reported a dropped row for."""
    return [table for table, row in parsed if row is None]


def test_hl_drops_a_trade_whose_side_token_is_neither_b_nor_a() -> None:
    payload = HL_TRADES.replace(b'"side":"A"', b'"side":"Ask"')
    parsed = hl.make_parser()(payload, RX)
    assert _drops(parsed) == ["trades"]
    assert [row["side"] for row in rows_for(parsed, "trades")] == ["buy"]


def test_binance_drops_a_liquidation_whose_side_token_is_re_cased() -> None:
    payload = BINANCE_LIQ.replace(b'"S":"SELL"', b'"S":"Sell"')
    parsed = binance.make_parser(False)(payload, RX)
    assert _drops(parsed) == ["liquidations"]
    assert rows_for(parsed, "liquidations") == []


def test_bybit_drops_a_liquidation_whose_side_token_is_re_cased() -> None:
    payload = BYBIT_LONG_LIQ.replace(b'"S":"Sell"', b'"S":"SELL"')
    parsed = bybit.make_parser(True)(payload, RX)
    assert _drops(parsed) == ["liquidations"]
    assert rows_for(parsed, "liquidations") == []


def test_okx_drops_a_liquidation_whose_order_side_is_unknown() -> None:
    payload = OKX_LIQ.replace(b'"posSide":"long"', b'"posSide":"net"').replace(
        b'"side":"sell"', b'"side":"short"'
    )
    parsed = okx.make_parser(False)(payload, RX)
    assert _drops(parsed) == ["liquidations"]
    assert rows_for(parsed, "liquidations") == []


# --- candles: one row per bar, and it is the closed one ------------------------


def _candle_frame(t: int, close: float, volume: float) -> bytes:
    return (
        b'{"channel":"candle","data":{"t":%d,"T":%d,"s":"BTC","i":"1m",'
        b'"o":"78862.0","c":"%s","h":"78900.0","l":"78800.0","v":"%s","n":21}}'
        % (t, t + 59_999, str(close).encode(), str(volume).encode())
    )


def test_a_bar_pushed_repeatedly_yields_one_row_and_it_is_the_closed_one() -> None:
    """HL pushes the in-progress bar over and over. `candles_1m` has no `rx_ms`, and
    compaction re-sorts by `ts_ms`, so afterwards nothing could tell which row was the
    closed bar. The parser now holds a bar until `t` advances and emits the last version."""
    parse = hl.make_parser()
    assert parse(_candle_frame(1788098880000, 78863.0, 0.1), RX) == []
    assert parse(_candle_frame(1788098880000, 78870.0, 0.5), RX) == []
    assert parse(_candle_frame(1788098880000, 78880.0, 0.8), RX) == []

    # The next bar opening is what proves the previous one closed.
    (row,) = rows_for(parse(_candle_frame(1788098940000, 78881.0, 0.01), RX), "candles_1m")
    assert row["ts_ms"] == 1788098880000
    assert (row["c"], row["v"]) == (78880.0, 0.8)


def test_a_snapshot_replayed_after_a_reconnect_does_not_rewrite_bars_already_emitted() -> None:
    """The subscribe snapshot is a list of recent bars, so a reconnect resends them."""
    parse = hl.make_parser()
    parse(_candle_frame(1788098880000, 1.0, 1.0), RX)
    (row,) = rows_for(parse(_candle_frame(1788098940000, 2.0, 2.0), RX), "candles_1m")
    assert row["ts_ms"] == 1788098880000
    # Reconnect: the snapshot repeats the bar that has already been written.
    assert parse(_candle_frame(1788098880000, 1.0, 1.0), RX) == []


_SNAPSHOT = (
    b'{"channel":"candle","data":['
    b'{"t":1788098760000,"T":1788098819999,"s":"BTC","i":"1m","o":"1","c":"2",'
    b'"h":"3","l":"0","v":"9","n":1},'
    b'{"t":1788098820000,"T":1788098879999,"s":"BTC","i":"1m","o":"2","c":"3",'
    b'"h":"4","l":"1","v":"8","n":2},'
    b'{"t":1788098880000,"T":1788098939999,"s":"BTC","i":"1m","o":"3","c":"4",'
    b'"h":"5","l":"2","v":"7","n":3}]}'
)


def test_the_subscribe_snapshot_seeds_a_fresh_coin_and_writes_nothing() -> None:
    """Every restart resubscribes and HL replies with the last bars of every coin. Folded
    like live pushes they were all written again, into a table with no `rx_ms` and no
    dedupe, so a day summed over `v` or `n` counted each restart twice."""
    parse = hl.make_parser()
    assert parse(_SNAPSHOT, RX) == []
    # The newest bar of the snapshot is the one now held, so the next bar closes it.
    (row,) = rows_for(parse(_candle_frame(1788098940000, 9.0, 9.0), RX), "candles_1m")
    assert row["ts_ms"] == 1788098880000
    assert row["c"] == 4.0  # the snapshot's own values, not the frame that closed it


def test_the_snapshot_after_a_reconnect_still_closes_the_bar_it_interrupted() -> None:
    """Seeding is only for a coin with no held bar. Mid-run the held bar is real and the
    snapshot has to close it, or a reconnect would eat the minute it happened in."""
    parse = hl.make_parser()
    parse(_candle_frame(1788098820000, 1.0, 1.0), RX)  # holding the 08:47 bar
    rows = rows_for(parse(_SNAPSHOT, RX), "candles_1m")
    assert [row["ts_ms"] for row in rows] == [1788098820000]
    assert rows[0]["c"] == 3.0  # the snapshot's version, which is the complete one


def test_candles_are_held_per_coin() -> None:
    parse = hl.make_parser()
    parse(_candle_frame(1788098880000, 1.0, 1.0), RX)
    eth = _candle_frame(1788098880000, 2.0, 2.0).replace(b'"s":"BTC"', b'"s":"ETH"')
    assert parse(eth, RX) == []  # a different coin's first bar, not BTC's advance
    (row,) = rows_for(parse(_candle_frame(1788098940000, 3.0, 3.0), RX), "candles_1m")
    assert row["coin"] == "BTC"


# --- the subscribe snapshot Hyperliquid replays on every resubscribe -------------


def _hl_trade(coin: str, tid: int, ts_ms: int) -> bytes:
    return (
        b'{"channel":"trades","data":[{"coin":"' + coin.encode() + b'","side":"B",'
        b'"px":"0.21","sz":"100.0","time":' + str(ts_ms).encode() + b',"hash":"0xab",'
        b'"tid":' + str(tid).encode() + b',"users":["0x01","0x02"]}]}'
    )


def test_a_trade_hyperliquid_replays_after_a_resubscribe_is_not_recorded_twice() -> None:
    """Measured on the box at the 22:42 UTC reconnect of 2026-09-05: Hyperliquid answers
    a `trades` subscribe with the last ~30 trades of each coin, and the recorder wrote
    every one of them again. All 458 replayed rows of that reconnect were already in the
    lake under the same `(coin, tid)`, and the day held 2,336 duplicate trades in total.
    """
    parse = hl.make_parser()
    live = parse(_hl_trade("DOGE", 1084082034831336, 1788648000000), RX)
    assert rows_for(live, "trades")

    # The resubscribe two seconds later, replaying the same trade.
    replay = parse(_hl_trade("DOGE", 1084082034831336, 1788648000000), RX + 2_000)
    assert replay == []


def test_a_trade_that_only_the_snapshot_carries_is_still_recorded() -> None:
    """The replay is also how the 18s hole a reconnect leaves gets filled. Dropping the
    whole snapshot would throw away the trades that happened while we were away; only the
    ones already in the lake are duplicates."""
    parse = hl.make_parser()
    parse(_hl_trade("DOGE", 111, 1788648000000), RX)
    # Happened during the hole, so we have never seen it.
    fresh = rows_for(parse(_hl_trade("DOGE", 222, 1788648009000), RX + 18_000), "trades")
    assert [row["tid"] for row in fresh] == [222]


def test_the_same_trade_id_on_two_coins_is_two_trades() -> None:
    parse = hl.make_parser()
    assert rows_for(parse(_hl_trade("DOGE", 7, 1788648000000), RX), "trades")
    assert rows_for(parse(_hl_trade("BTC", 7, 1788648000000), RX), "trades")


def test_the_replay_memory_is_bounded_per_coin() -> None:
    """A busy coin must not push a thin coin's ids out: DOGE prints 3 times a minute and
    its 30-trade snapshot reaches back six minutes, while PONS prints 176 times a minute.
    One shared ring would be drained by PONS before DOGE's snapshot arrived."""
    parse = hl.make_parser()
    parse(_hl_trade("DOGE", 1, 1788648000000), RX)
    for tid in range(2, 5_000):
        parse(_hl_trade("PONS", tid, 1788648000000), RX)

    assert parse(_hl_trade("DOGE", 1, 1788648000000), RX + 60_000) == []
