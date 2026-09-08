"""Hyperliquid WebSocket parsing and subscription frames.

Five channels per coin: `trades`, `bbo`, `l2Book`, `candle` (1m) and `activeAssetCtx`,
feeding the `trades`, `bbo`, `l2`, `candles_1m` and `asset_ctx` tables. Payload shapes
were captured live with `scripts/probe/ws_payloads.py` on 2026-08-30, not taken from a
doc, and the parser tests embed those exact bytes.

`make_parser()` rather than a module level `parse`, as on every other venue: two channels
need per connection state, the open bar of each coin and the trade ids already recorded.
See its docstring for why.

Two things the schema doc does not say and the wire does:

- `activeAssetCtx` carries no timestamp, so `ts_ms` is set to `rx_ms` for that table. It
  is a snapshot of a slowly moving context, not an event, so the receive time is the
  honest answer.
- `side` on a trade is the aggressor: `B` is a buy, `A` is a sell. Anything else drops
  the row: this is the one field whose sign the whole strategy hangs on.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import orjson

from liqfade.recorder.streams.base import ParsedRow, Parser

# channel -> canonical table it feeds
CHANNEL_TABLES = {
    "trades": "trades",
    "bbo": "bbo",
    "l2Book": "l2",
    "candle": "candles_1m",
    "activeAssetCtx": "asset_ctx",
}
TABLES = tuple(CHANNEL_TABLES.values())
VENUE = "hyperliquid"
CANDLE_INTERVAL = "1m"
L2_LEVELS = 20  # what the l2Book subscription returns per side
# Trade ids remembered per coin, so a trade Hyperliquid replays is recognised rather than
# written a second time. Measured at the 22:42 UTC reconnect of 2026-09-05: the subscribe
# answer carries the last ~30 trades of each coin, the same count on every coin whatever
# its rate. 512 is that with room to spare, and it is per coin on purpose: DOGE prints 3
# times a minute against PONS's 176, so one shared ring would be drained by PONS long
# before DOGE's six minutes of snapshot arrived. 512 ids across even a 200 coin universe
# is nothing to hold.
SEEN_TIDS_PER_COIN = 512

PING_FRAME = {"method": "ping"}
PONG_MARKER = b'"pong"'

# The aggressor of a trade. Explicit rather than `"buy" if x == "B" else "sell"`: an
# unknown token has to drop the row, because guessing it writes the opposite side.
TRADE_SIDES = {"B": "buy", "A": "sell"}

# (coin, notional_usd, ts_ms, buyer, seller) for one trade the lake has just taken.
OnUsers = Callable[[str, float, int, str, str], None]


def _subscription(channel: str, coin: str) -> dict[str, Any]:
    sub: dict[str, Any] = {"type": channel, "coin": coin}
    if channel == "candle":
        sub["interval"] = CANDLE_INTERVAL
    return sub


def _frames(method: str, coins: Sequence[str]) -> list[dict[str, Any]]:
    return [
        {"method": method, "subscription": _subscription(channel, coin)}
        for coin in coins
        for channel in CHANNEL_TABLES
    ]


def on_connect(symbols: Sequence[str]) -> list[dict[str, Any]]:
    """One subscribe frame per coin per channel. HL allows 1000 subscriptions."""
    return _frames("subscribe", symbols)


def on_universe_change(added: Sequence[str], removed: Sequence[str]) -> list[dict[str, Any]]:
    return _frames("subscribe", added) + _frames("unsubscribe", removed)


def _f(value: Any) -> float | None:
    """HL sends every number as a string. None and "" become null rather than 0.0."""
    if value is None or value == "":
        return None
    return float(value)


def make_parser(*, on_users: OnUsers | None = None) -> Parser:
    """A parser bound to one connection, holding the open 1m bar and seen trades per coin.

    **Trades.** Hyperliquid answers a `trades` subscribe with a snapshot of the last ~30
    trades of that coin, and every reconnect resubscribes. The recorder wrote all of them
    again: 2,336 duplicate `(coin, tid)` rows on 2026-09-05, and at the 22:42 reconnect
    every one of the 458 replayed rows was already in the lake. It also made a thin coin
    look minutes behind, because 30 trades back is six minutes on DOGE and three seconds
    on BTC, which is the whole of the "DOGE arrives 356s late" report.

    So a trade whose `(coin, tid)` this connection has already emitted is dropped. Not the
    whole snapshot: the same replay is what fills the hole a reconnect leaves, and the
    trades that happened while we were away are exactly the ones we have not seen. The
    memory is per coin and bounded, and it lives as long as the process rather than as
    long as the socket, which is what makes it survive the reconnect it exists for.

    The one case it cannot cover is a process restart, which starts with an empty memory
    and so writes one snapshot per coin that the previous run already recorded. That is
    about 30 rows per coin per restart against 2,336 a day from reconnects, and telling it
    from a genuine first subscribe would mean reading the lake from inside a parser.
    docs/DECISIONS.md, 2026-09-06.

    **Candles.** HL pushes the
    in-progress bar over and over: the recorder used to write every push, so a partition
    held many rows per `(coin, ts_ms)`. `candles_1m` has no `rx_ms` by design and
    compaction re-sorts by `ts_ms`, so afterwards nothing could tell which of them was
    the closed bar, and the wrong one has partial `c`, `v` and `n`.

    So a bar is held per coin and emitted when `t` advances, which is the only signal HL
    gives that the previous bar is done. The version emitted is the last one seen, which
    is the closed bar; the prompt's alternative (keep the first, drop the repeats) would
    have kept an in-progress bar instead. A bar older than the one held is dropped, which
    is what makes the subscribe snapshot after a reconnect stop rewriting bars already in
    the lake; on a restart there is no held bar to compare against, so the snapshot seeds
    the coin instead of being folded (see `_snapshot`). Costs: the newest bar per coin is
    written up to a minute late and is lost if the process stops before the next one
    opens, and the bars from a restart's downtime are not written at all. `candles_1m` is
    derivable from `trades` (`liqfade derive candles`), and both a duplicated bar and an
    in-progress bar in the lake are worse than a missing one.

    **Addresses.** Every public HL trade carries `users: [buyer, seller]` and the `trades`
    table drops it: that table is in `MULTI_VENUE_TABLES`, shared with Bybit, OKX and
    Binance rows that have no such field, so the addresses cannot become a column on it.
    With `on_users` set they instead go to a callback, once per trade that passes
    `_first_sight`, and the recorder feeds them to `AddressBook` in memory. A replayed
    trade must not be counted twice, or a reconnect would inflate whoever happened to be
    trading through it. With no callback this parser behaves exactly as it did before,
    which is what `live/loop.py` gets: it wants the rows and not the addresses.
    """
    open_bars: dict[str, tuple[int, dict[str, Any]]] = {}
    # coin -> its recent trade ids, oldest first. A dict rather than a deque plus a set:
    # it is insertion ordered, so it is both the membership test and the eviction order.
    seen_tids: dict[str, dict[int, None]] = {}

    def parse(msg: bytes, rx_ms: int) -> list[ParsedRow]:
        """Turn one HL frame into canonical rows. Unknown channels yield nothing."""
        obj = orjson.loads(msg)
        channel = obj.get("channel")
        data = obj.get("data")
        if channel == "trades":
            rows: list[ParsedRow] = []
            for raw in data:
                parsed = _trade(raw, rx_ms)
                if not _first_sight(parsed, seen_tids):
                    continue
                rows.append(parsed)
                if on_users is not None:
                    _note_users(raw, parsed[1], on_users)
            return rows
        if channel == "bbo":
            return [_bbo(data, rx_ms)]
        if channel == "l2Book":
            return _l2(data, rx_ms)
        if channel == "candle":
            # A snapshot on subscribe arrives as a list, live updates as one object.
            if isinstance(data, list):
                return _snapshot(data, open_bars)
            return _candle(data, open_bars)
        if channel == "activeAssetCtx":
            return [_asset_ctx(data, rx_ms)]
        return []

    return parse


def _trade(row: dict[str, Any], rx_ms: int) -> ParsedRow:
    side = TRADE_SIDES.get(str(row.get("side")))
    if side is None:
        return ("trades", None)
    return (
        "trades",
        {
            "venue": VENUE,
            "coin": row["coin"],
            "ts_ms": int(row["time"]),
            "rx_ms": rx_ms,
            "px": _f(row["px"]),
            "sz": _f(row["sz"]),
            "side": side,
            "tid": int(row["tid"]),
        },
    )


def _note_users(raw: dict[str, Any], row: dict[str, Any] | None, on_users: OnUsers) -> None:
    """Report a trade's two counterparties and what it was worth. Never raises.

    A row the parser refused (`row is None`) reports nothing: it is not in the lake, so
    it is not a trade. `users` is documented on every public trade, but a payload without
    it, or with anything other than the two addresses, is skipped rather than guessed at.
    """
    users = raw.get("users")
    px, sz = (row or {}).get("px"), (row or {}).get("sz")
    if row is None or px is None or sz is None:
        return
    if not isinstance(users, list) or len(users) != 2:
        return
    on_users(str(row["coin"]), px * sz, int(row["ts_ms"]), str(users[0]), str(users[1]))


def _first_sight(parsed: ParsedRow, seen_tids: dict[str, dict[int, None]]) -> bool:
    """True for a trade this connection has not emitted before. Records it on the way.

    A refused row (`row is None`) passes through so the parse error is still counted; it
    carries no id to remember. `tid` is Hyperliquid's own trade id and is unique per
    trade, so the same one twice is the same trade twice.
    """
    row = parsed[1]
    if row is None:
        return True
    ids = seen_tids.setdefault(str(row["coin"]), {})
    tid = int(row["tid"])
    if tid in ids:
        return False
    ids[tid] = None
    while len(ids) > SEEN_TIDS_PER_COIN:
        del ids[next(iter(ids))]
    return True


def _level(levels: Any, index: int) -> dict[str, Any]:
    """One side of a book level, or an empty dict when that side is thin or absent."""
    if not isinstance(levels, list) or index >= len(levels) or not levels[index]:
        return {}
    level = levels[index]
    return level if isinstance(level, dict) else {}


def _bbo(data: dict[str, Any], rx_ms: int) -> ParsedRow:
    bid, ask = _level(data.get("bbo"), 0), _level(data.get("bbo"), 1)
    return (
        "bbo",
        {
            "coin": data["coin"],
            "ts_ms": int(data["time"]),
            "rx_ms": rx_ms,
            "bid_px": _f(bid.get("px")),
            "bid_sz": _f(bid.get("sz")),
            "ask_px": _f(ask.get("px")),
            "ask_sz": _f(ask.get("sz")),
        },
    )


def _l2(data: dict[str, Any], rx_ms: int) -> list[ParsedRow]:
    levels = data.get("levels") or [[], []]
    bids, asks = [*levels, [], []][:2]
    depth = min(L2_LEVELS, max(len(bids), len(asks)))
    coin, ts_ms = data["coin"], int(data["time"])
    rows: list[ParsedRow] = []
    for index in range(depth):
        bid, ask = _level(bids, index), _level(asks, index)
        rows.append(
            (
                "l2",
                {
                    "coin": coin,
                    "ts_ms": ts_ms,
                    "rx_ms": rx_ms,
                    "level": index,
                    "bid_px": _f(bid.get("px")),
                    "bid_sz": _f(bid.get("sz")),
                    "ask_px": _f(ask.get("px")),
                    "ask_sz": _f(ask.get("sz")),
                },
            )
        )
    return rows


def _bar_row(update: dict[str, Any]) -> dict[str, Any]:
    """One `candles_1m` row from one candle push. No `rx_ms`: the table has none."""
    return {
        "coin": update["s"],
        "ts_ms": int(update["t"]),
        "o": _f(update["o"]),
        "h": _f(update["h"]),
        "l": _f(update["l"]),
        "c": _f(update["c"]),
        "v": _f(update["v"]),
        "n": int(update.get("n") or 0),
    }


def _snapshot(
    updates: list[dict[str, Any]], open_bars: dict[str, tuple[int, dict[str, Any]]]
) -> list[ParsedRow]:
    """Fold a subscribe snapshot. A coin we hold no bar for is seeded, never replayed.

    The snapshot carries the last bars of every coin subscribed, so folding it the way a
    live push is folded emits all of them but the newest. Across a reconnect that is
    right: a bar is held for the coin, so everything older is dropped and the held bar is
    closed by the first entry past it. Across a process *restart* there is no held bar,
    and folding wrote every bar in the snapshot again, into a table with no `rx_ms` and
    no dedupe. Those bars are already in the lake from the previous run, so anything
    summing `v` or `n` over a day counted a restart twice.

    A coin with no held bar therefore takes the snapshot's newest bar as its open one and
    emits nothing. The cost is the bars from a downtime window, which are not written by
    the recorder at all: `liqfade derive candles` rebuilds them exactly from `trades`,
    which is the canonical source anyway. A missing bar is fillable and a duplicated one
    is silently wrong, and this table has already chosen missing once (see `make_parser`).
    """
    seeding = {update["s"] for update in updates} - set(open_bars)
    rows: list[ParsedRow] = []
    for update in updates:
        if update["s"] in seeding:
            _seed(update, open_bars)
        else:
            rows.extend(_candle(update, open_bars))
    return rows


def _seed(update: dict[str, Any], open_bars: dict[str, tuple[int, dict[str, Any]]]) -> None:
    """Hold a coin's newest snapshot bar without emitting the ones before it."""
    bar_ms = int(update["t"])
    held = open_bars.get(update["s"])
    if held is None or bar_ms > held[0]:
        open_bars[update["s"]] = (bar_ms, _bar_row(update))


def _candle(
    update: dict[str, Any], open_bars: dict[str, tuple[int, dict[str, Any]]]
) -> list[ParsedRow]:
    """Fold one candle push into the coin's open bar. Emits the previous bar, if any.

    Four cases, in the order they happen: the first bar of a coin is held; a repeat of
    the held bar replaces it, because the latest push is the most complete; a newer `t`
    closes the held bar and emits it; an older `t` is a snapshot replay of a bar already
    written and is dropped.
    """
    coin = update["s"]
    bar_ms = int(update["t"])
    row = _bar_row(update)
    held = open_bars.get(coin)
    if held is not None and bar_ms < held[0]:
        return []
    open_bars[coin] = (bar_ms, row)
    if held is None or bar_ms == held[0]:
        return []
    return [("candles_1m", held[1])]


def _asset_ctx(data: dict[str, Any], rx_ms: int) -> ParsedRow:
    ctx = data.get("ctx") or {}
    # `impactPxs` is [bid, ask], the average fill price of a fixed notional against each
    # side of the book. A coin with an empty side sends a short list or none at all, so
    # the pair is read positionally and missing entries stay null rather than becoming 0.
    impact = ctx.get("impactPxs") or []
    return (
        "asset_ctx",
        {
            "coin": data["coin"],
            # activeAssetCtx carries no exchange timestamp; receive time is the truth.
            "ts_ms": rx_ms,
            "rx_ms": rx_ms,
            "mark_px": _f(ctx.get("markPx")),
            "oracle_px": _f(ctx.get("oraclePx")),
            "mid_px": _f(ctx.get("midPx")),
            "funding": _f(ctx.get("funding")),
            "open_interest": _f(ctx.get("openInterest")),
            "day_ntl_vlm": _f(ctx.get("dayNtlVlm")),
            "premium": _f(ctx.get("premium")),
            "impact_bid_px": _f(impact[0]) if len(impact) > 0 else None,
            "impact_ask_px": _f(impact[1]) if len(impact) > 1 else None,
        },
    )
