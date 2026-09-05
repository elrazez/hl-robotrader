"""The fill model: when a simulated order fills, at what price, and how honestly.

Three ideas, all from `docs/STRATEGY_AND_RISK.md` section 6.

**Latency.** An intent at `t` becomes an order the market can see at `t + latency_ms`. Until
then it does not exist, and no print before that moment can fill it. This is the single
most load-bearing pessimism in the whole engine: the edge being measured lives in the
seconds after a cascade extreme, and an entry that fills a second early is measuring a
trade nobody could have made.

**Maker.** A resting `Alo` fills when the market trades *through* it, or when cumulative
volume printed at its own price since placement exceeds the size that was displayed there
when it was placed, times `maker_queue_pessimism`. That second clause is the queue model:
we assume everything shown ahead of us is real and ahead of us. Partial fills are allowed,
because a print of half our size fills half of it.

**Taker.** Walk the most recent `l2` snapshot from the touch and take the VWAP of the levels
consumed. Three degradations, in order of how much they cost us:

1. snapshot older than `stale_book_s`: add `slip_stale_bps`.
2. size exceeds the book: the remainder fills at the last level's price plus
   `slip_stale_bps`, rather than being refused. A book of twenty levels is not the whole
   book, and refusing would flatter the backtest by dropping exactly the trades that were
   too large.
3. **no snapshot at all**: fill at the last trade price plus `slip_stale_bps`, and count it
   in `no_book_fills`. This is the normal case for the historical year, where `l2` exists
   as hourly snapshots on four coins, so the report prints the share of fills that used it
   and the holdout comparison flags any split where the share differs by more than ten
   points. A number produced this way is an estimate with a name, not a measurement.

Stops trigger on mid and fill as taker, and **a gap through the stop fills at the gapped
price, not at the stop price**. Filling at the stop would be the single most flattering lie
a backtest can tell: it converts every overnight gap and every cascade into a bounded loss
that the market never offered.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum

from liqfade.config.models import Settings
from liqfade.strategy.types import Side

__all__ = [
    "BookState",
    "Fill",
    "OrderKind",
    "SimOrder",
    "fill_maker",
    "fill_taker",
    "walk_book",
]


class OrderKind(Enum):
    """`ALO` rests and never crosses; `IOC` takes what is there and cancels the rest."""

    ALO = "Alo"
    IOC = "Ioc"


@dataclass(frozen=True, slots=True)
class BookState:
    """What is known about one coin's book and tape at a moment.

    Args:
        bids: `(price, size)` outward from the touch, from the latest `l2` snapshot.
        asks: the same on the offer.
        snapshot_ms: when that snapshot was taken, or None when there has never been one.
        last_px: the last trade price, the fallback when no snapshot exists.
        last_trade_ms: when that print happened.
    """

    bids: tuple[tuple[float, float], ...] = ()
    asks: tuple[tuple[float, float], ...] = ()
    snapshot_ms: int | None = None
    last_px: float | None = None
    last_trade_ms: int | None = None

    def levels(self, side: Side) -> tuple[tuple[float, float], ...]:
        """The side a `side` entry takes liquidity from: a long lifts the asks."""
        return self.asks if side == "long" else self.bids

    def mid(self) -> float | None:
        """Book mid where there is a book, last trade price otherwise, None if neither."""
        if self.bids and self.asks:
            return (self.bids[0][0] + self.asks[0][0]) / 2.0
        return self.last_px


@dataclass(slots=True)
class SimOrder:
    """One simulated order. Mutable, because a resting order accumulates fills.

    Args:
        cloid: the client order id. Unique per order, as CLAUDE.md requires of every order
            including simulated ones, so a fill can always be traced to its intent.
        coin: the symbol.
        side: `long` to buy, `short` to sell.
        kind: `ALO` or `IOC`.
        px: the limit price.
        size: the size requested, in coin units.
        filled: how much has filled so far.
        visible_ms: when the market can first see it, which is `intent.ts_ms + latency_ms`.
        queue_ahead: size assumed to be ahead of us at `px` when we joined, for the maker
            model. Zero means the front of an empty queue.
        traded_at_px: cumulative volume printed at exactly `px` since placement.
        reduce_only: whether this order may only close.
        tag: `entry`, `target`, `stop` or `time_stop`, for the trade log.
    """

    cloid: str
    coin: str
    side: Side
    kind: OrderKind
    px: float
    size: float
    visible_ms: int
    filled: float = 0.0
    queue_ahead: float = 0.0
    traded_at_px: float = 0.0
    reduce_only: bool = False
    tag: str = "entry"

    @property
    def remaining(self) -> float:
        return max(self.size - self.filled, 0.0)

    @property
    def is_buy(self) -> bool:
        """A long entry buys; a short entry sells. Reduce-only orders invert elsewhere."""
        return self.side == "long"


@dataclass(frozen=True, slots=True)
class Fill:
    """One fill, and how it was priced.

    Args:
        cloid: the order it belongs to.
        coin: the symbol.
        ts_ms: when it happened.
        px: the fill price, VWAP where several levels were consumed.
        size: the size filled, in coin units.
        is_buy: direction, for the portfolio.
        maker: whether it earned the maker fee.
        no_book: whether the degraded no-snapshot model priced it. Counted into
            `no_book_fills` and printed per split by the report.
        slippage_bps: how far the fill landed from the price that was asked for, positive
            meaning worse. Stop slippage is reported separately from entry slippage.
        tag: the order's tag, carried through so the trade log can group by leg.
    """

    cloid: str
    coin: str
    ts_ms: int
    px: float
    size: float
    is_buy: bool
    maker: bool
    no_book: bool = False
    slippage_bps: float = 0.0
    tag: str = "entry"


@dataclass(slots=True)
class WalkResult:
    """The outcome of consuming a book: VWAP, how much was taken, and how degraded."""

    px: float
    size: float
    no_book: bool = False
    levels_consumed: int = 0
    exhausted_book: bool = False
    extras: dict[str, float] = field(default_factory=dict)


def walk_book(
    book: BookState,
    *,
    side: Side,
    size: float,
    now_ms: int,
    settings: Settings,
) -> WalkResult | None:
    """VWAP of taking `size` from the book, with every degradation applied.

    Args:
        book: the latest known book and tape for the coin.
        side: `long` consumes asks, `short` consumes bids.
        size: coin units wanted.
        now_ms: the moment of the take, for the staleness test.
        settings: resolved config, for `stale_book_s` and `slip_stale_bps`.

    Returns:
        A `WalkResult`, or None when the coin has neither a book nor a trade price and so
        cannot be priced at all. None means no fill, never a guessed one.
    """
    if size <= 0:
        return None
    backtest = settings.backtest
    slip = backtest.slip_stale_bps / 1e4
    direction = 1.0 if side == "long" else -1.0

    levels = book.levels(side)
    if not levels or book.snapshot_ms is None:
        # The historical year, almost everywhere. Priced off the tape and counted, so the
        # report can say what share of its fills were estimated rather than walked.
        if book.last_px is None:
            return None
        return WalkResult(px=book.last_px * (1.0 + direction * slip), size=size, no_book=True)

    stale = (now_ms - book.snapshot_ms) / 1000.0 > backtest.stale_book_s
    remaining, notional, consumed = size, 0.0, 0
    for level_px, level_sz in levels:
        if remaining <= 0:
            break
        take = min(remaining, level_sz)
        notional += take * level_px
        remaining -= take
        consumed += 1

    exhausted = remaining > 0
    if exhausted:
        # Twenty levels is not the whole book. Refusing the remainder would flatter the
        # result by dropping exactly the trades that were too big for what we can see, so
        # it fills at the last level plus the stale penalty instead.
        worst = levels[-1][0]
        notional += remaining * worst * (1.0 + direction * slip)

    vwap = notional / size
    if stale:
        vwap *= 1.0 + direction * slip
    return WalkResult(px=vwap, size=size, levels_consumed=consumed, exhausted_book=exhausted)


def fill_taker(
    order: SimOrder,
    book: BookState,
    *,
    now_ms: int,
    settings: Settings,
) -> Fill | None:
    """Fill an `IOC` order against the book. Returns None when it cannot be priced."""
    if now_ms < order.visible_ms or order.remaining <= 0:
        return None
    walk = walk_book(book, side=order.side, size=order.remaining, now_ms=now_ms, settings=settings)
    if walk is None:
        return None
    order.filled += walk.size
    return Fill(
        cloid=order.cloid,
        coin=order.coin,
        ts_ms=now_ms,
        px=walk.px,
        size=walk.size,
        is_buy=order.is_buy,
        maker=False,
        no_book=walk.no_book,
        slippage_bps=_slippage_bps(order.px, walk.px, order.is_buy),
        tag=order.tag,
    )


def fill_maker(
    order: SimOrder,
    *,
    trade_px: float,
    trade_sz: float,
    trade_is_buy: bool,
    now_ms: int,
    settings: Settings,
) -> Fill | None:
    """Consider one print against a resting `Alo`. Returns a fill, a partial, or None.

    Two ways to fill, and the order matters:

    1. **Traded through.** The print crossed our price, which means everything resting at
       our price was consumed. We fill for the whole remainder at our price.
    2. **Queue exhausted.** The print was *at* our price. It counts against the queue we
       assumed was ahead of us; only once that is used up does any of it reach us, and then
       only for what is left of the print. Partial fills are the normal case here.

    A print on the same side as us never fills us: our bid is lifted by a seller, not by
    another buyer.
    """
    if now_ms < order.visible_ms or order.remaining <= 0:
        return None
    if trade_is_buy == order.is_buy:
        return None

    through = trade_px < order.px if order.is_buy else trade_px > order.px
    if through and settings.backtest.maker_requires_trade_through:
        size = order.remaining
    elif trade_px == order.px:
        order.traded_at_px += trade_sz
        queue = order.queue_ahead * settings.backtest.maker_queue_pessimism
        reached_us = order.traded_at_px - queue
        if reached_us <= 0:
            return None
        # Only the part of this print that landed after the queue drained is ours, and
        # never more than the print itself: a single trade cannot fill more than its size.
        size = min(order.remaining, reached_us, trade_sz)
    elif through:
        size = order.remaining
    else:
        return None

    if size <= 0:
        return None
    order.filled += size
    return Fill(
        cloid=order.cloid,
        coin=order.coin,
        ts_ms=now_ms,
        px=order.px,
        size=size,
        is_buy=order.is_buy,
        maker=True,
        slippage_bps=0.0,
        tag=order.tag,
    )


def stop_triggered(*, side: Side, stop_px: float, mid: float) -> bool:
    """Whether a stop for a `side` position has been breached by `mid`.

    Triggered on mid rather than on a trade print, which is what a venue's mark-price stop
    does and what `docs/STRATEGY_AND_RISK.md` section 6 specifies.
    """
    return mid <= stop_px if side == "long" else mid >= stop_px


def gapped_stop_order(order: SimOrder, *, mid: float) -> SimOrder:
    """Reprice a triggered stop to the market it actually gapped to.

    **The most important line in the fill model.** A stop at 100 with the market at 90 does
    not fill at 100. Filling it there would convert every gap and every cascade into a
    bounded loss the market never offered, which is the single most flattering error a
    backtest can make, and it would do so exactly in the tail this strategy trades.

    The order becomes an `IOC` at the gapped price, so it walks the book from there like
    any other take and the difference lands in `slippage_bps`.
    """
    return replace(order, kind=OrderKind.IOC, px=mid)


def _slippage_bps(asked_px: float, got_px: float, is_buy: bool) -> float:
    """How far the fill landed from the price asked for, positive meaning worse."""
    if asked_px <= 0:
        return 0.0
    direction = 1.0 if is_buy else -1.0
    return 1e4 * direction * (got_px - asked_px) / asked_px
