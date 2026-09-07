"""The replay loop: events update state, ticks make decisions, orders become fills.

One pass over the merged stream from `backtest/data.py`, interleaved with decision ticks
from `backtest/clock.py`. Events maintain the book and tape per coin; a tick reads the
precomputed feature grid, asks the strategy, sizes, checks risk, and places orders. Orders
fill against the events that follow them, never against the ones that preceded them.

The order of operations inside a tick matters and is fixed:

1. mark equity from the current mids, and tell the risk engine
2. expire and reprice resting entries whose `entry_ttl_s` has elapsed
3. check exits on every open position: target, stop, time stop
4. ask the strategy for intents
5. size, gate, and place

Exits are checked before entries so that a position closed on this tick frees its slot for
an entry on the same tick, which is what live would do, and so that the risk engine sees
the post-exit book when it counts concurrent positions.

**Determinism is a hard requirement, not an aspiration.** Nothing here reads a clock, and
the only source of randomness is the `--seed` the CLI passes, which currently nothing
consumes: the model has no stochastic component. Coins are iterated in sorted order and the
event stream breaks ties on a fixed key, so the same command twice writes byte-identical
`trades.parquet`. `tests/test_backtest_engine.py` asserts it.
"""

from __future__ import annotations

import random
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field

import polars as pl

from liqfade.backtest.clock import Tick, merge, tick_times
from liqfade.backtest.data import (
    L2,
    AssetCtx,
    Bbo,
    Event,
    Liq,
    Trade,
    iter_events,
    span_filter,
    utc_days,
)
from liqfade.backtest.features import (
    active_mask,
    build_feature_grid,
    candidates,
    cascade_windows,
    iter_grid_rows,
    merge_spans,
)
from liqfade.backtest.fills import (
    BookState,
    Fill,
    OrderKind,
    SimOrder,
    fill_maker,
    fill_taker,
    gapped_stop_order,
    stop_triggered,
)
from liqfade.backtest.portfolio import Portfolio
from liqfade.backtest.portfolio import Trade as PortfolioTrade
from liqfade.backtest.progress import ProgressWriter
from liqfade.backtest.source import DAY_MS, Day, LakeSource, feature_hash, midnight_ms
from liqfade.config.models import Settings
from liqfade.features.grid import effective_ts
from liqfade.log import get_logger
from liqfade.risk.engine import AccountView, EntryRequest, RiskEngine
from liqfade.sizing import LiquidityStats, size_intent
from liqfade.strategy.engine import Rejection, StrategyEngine
from liqfade.strategy.types import AssetMeta, FeatureRow, Intent

log = get_logger(__name__)

# What `run_backtest` accepts. Two implementations, not an interface with one: the lake and
# the frames a test builds. Both answer `days`, `grids`, `events` and `funding`, and both
# carry the run's window, coins, starting equity and seed.
type Source = LakeSource | RunInputs

__all__ = ["BacktestResult", "RunInputs", "Source", "run_backtest"]

# szDecimals and maxLeverage when `meta.universe` was not supplied. Five decimals is the
# finest Hyperliquid uses, so it rounds least, and 3x is its most conservative leverage
# tier. Both are deliberately the choice that constrains the backtest most: a run that
# invented a generous asset spec would report sizes the venue would have refused.
DEFAULT_SZ_DECIMALS = 5
DEFAULT_MAX_LEVERAGE = 3.0

# How often the equity curve is sampled, in milliseconds. Every decision tick would be
# 126 million points over a year for a curve whose only consumers are the drawdown and a
# chart, neither of which can use that resolution.
EQUITY_SAMPLE_MS = 60_000

# How often the equity curve is sampled through tape the run never replays. The portfolio
# is flat there so the value does not move; the samples exist to keep the drawdown series
# continuous and to give the risk engine a mark on every UTC day.
HOUR_MS = 3_600_000


@dataclass
class RunInputs:
    """Everything one replay reads, as frames a test built rather than a lake.

    The in-memory twin of `backtest/source.py`'s `LakeSource`: it answers the same three
    questions (which days, this day's grid, the events inside these windows) off frames it
    already holds. `run_backtest` is therefore still a pure function of its inputs plus the
    config, which is what makes the determinism and no-lookahead tests possible.

    Nothing here streams, and nothing needs to: a fixture is minutes of tape. The real lake
    goes through `LakeSource`, which never holds more than one day of one coin.
    """

    trades: pl.DataFrame
    liquidations: pl.DataFrame
    start_ms: int
    end_ms: int
    bbo: pl.DataFrame | None = None
    l2: pl.DataFrame | None = None
    asset_ctx: pl.DataFrame | None = None
    levels: pl.DataFrame | None = None
    meta: Mapping[str, AssetMeta] = field(default_factory=dict)
    starting_equity: float = 10_000.0
    seed: int = 0
    _grid: pl.DataFrame | None = field(default=None, repr=False, compare=False)
    _grid_hash: str = field(default="", repr=False, compare=False)

    @property
    def coins(self) -> tuple[str, ...]:
        if self.trades.is_empty():
            return ()
        return tuple(sorted(self.trades["coin"].unique().to_list()))

    def days(self) -> list[Day]:
        return [
            Day(
                date=day,
                start_ms=max(midnight_ms(day), self.start_ms),
                end_ms=min(midnight_ms(day) + DAY_MS, self.end_ms),
            )
            for day in utc_days(self.start_ms, self.end_ms)
        ]

    def grids(
        self, day: Day, settings: Settings, *, first: bool
    ) -> Iterator[tuple[str, pl.DataFrame]]:
        """One coin at a time, like the lake, but built over the whole window at once.

        A fixture is minutes of tape, so splitting it by day would give every rolling
        feature a cold start at midnight and move numbers the golden test derives on paper.
        Built once and sliced per day instead, which is exactly the grid the non-streaming
        engine had. `first` is therefore unused here; the lake needs it because its days
        are built independently.

        Rebuilt when the settings that decide a feature column change, because one source
        is replayed twice by `--lagged-ms` and the second run must not inherit the first
        run's grid if it asked for different features.
        """
        digest = feature_hash(settings)
        if self._grid is None or self._grid_hash != digest:
            self._grid_hash = digest
            self._grid = build_feature_grid(
                settings=settings,
                trades=self.trades,
                liquidations=self.liquidations,
                start_ms=self.start_ms,
                end_ms=self.end_ms,
                bbo=self.bbo,
                l2=self.l2,
                asset_ctx=self.asset_ctx,
                levels=self.levels,
            )
        if self._grid.is_empty():
            return
        for coin in self.coins:
            rows = self._grid.filter(
                (pl.col("coin") == coin)
                & pl.col("ts_ms").is_between(day.start_ms, day.end_ms, closed="left")
            )
            if not rows.is_empty():
                yield coin, rows

    def events(self, day: Day, spans: Mapping[str, Sequence[tuple[int, int]]]) -> Iterator[Event]:
        return iter_events(
            trades=_within(self.trades, spans),
            liquidations=_within(self.liquidations, spans),
            bbo=_or_none(_within(self.bbo, spans)),
            l2=_or_none(_within(self.l2, spans)),
            asset_ctx=_or_none(_within(self.asset_ctx, spans)),
        )

    def funding(self, day: Day) -> pl.DataFrame:
        if self.asset_ctx is None:
            return pl.DataFrame()
        return self.asset_ctx.filter(
            effective_ts(None).is_between(day.start_ms, day.end_ms, closed="left")
        )


@dataclass
class BacktestResult:
    """What a run produced, before any of it is written down."""

    trades: list[PortfolioTrade]
    equity_curve: list[tuple[int, float]]
    portfolio: Portfolio
    strategy: StrategyEngine
    risk_blocks: dict[str, int]
    rejections: list[Rejection]
    intents: int = 0
    entries_placed: int = 0
    sizing_blocks: dict[str, int] = field(default_factory=dict)
    depth_capped_entries: int = 0
    start_ms: int = 0
    end_ms: int = 0
    coins: tuple[str, ...] = ()
    last_equity_sample_ms: int = 0
    # How much of the clock was actually replayed, and how many cascade windows it took.
    # The report prints the share, which is the whole claim of the streaming engine: a year
    # in which the strategy is idle 98% of the time should read about 3% of the tape.
    replayed_ms: int = 0
    windows: int = 0


class _CoinState:
    """Per-coin book, tape and the orders working against them."""

    __slots__ = ("book", "entry", "exit_order", "intent", "last_data_ms", "reprices")

    def __init__(self) -> None:
        self.book = BookState()
        self.entry: SimOrder | None = None
        self.exit_order: SimOrder | None = None
        self.intent: Intent | None = None
        self.last_data_ms: int | None = None
        self.reprices = 0


def run_backtest(
    source: Source, settings: Settings, progress: ProgressWriter | None = None
) -> BacktestResult:
    """Replay one window and return everything it produced.

    Walks the source one UTC day at a time. For each day it builds (or reads from cache)
    that day's feature grid one coin at a time, works out which stretches of tape a cascade
    could involve, and replays only those. Outside them the portfolio is flat and no order
    is working, which is asserted at every window end rather than assumed, so the tape there
    holds nothing a decision could have used.

    Args:
        source: a `LakeSource` or a `RunInputs`. Anything answering `days`, `grids`,
            `events` and `funding`.
        settings: resolved config. `cascade.trigger` picks the liquidation feed;
            `backtest.latency_ms` sets how late every order is.
        progress: where to report the replay clock once a second, for `liqfade watch`.
            None runs silent.

    Returns:
        A `BacktestResult`. The trade list is the run's output; everything else exists so
        the report can say why there were not more of them.
    """
    # Seeded even though nothing currently draws from it: the fill model has no
    # stochastic component. It exists so that adding one later cannot silently break the
    # determinism guarantee, and it is not a cryptographic use.
    random.Random(source.seed)  # noqa: S311

    meta = _resolve_meta(source.meta, source.coins, settings)
    portfolio = Portfolio(settings, starting_equity=source.starting_equity)
    strategy = StrategyEngine(settings, meta)
    # In memory: a replay starts from no halts and no cooldowns, and must never
    # read or write the live account's risk state.
    # A replay places no orders, so the one control that outranks everything else has
    # nothing to stop, and asking it costs a `stat` of `data/KILL` on every entry check.
    # `Settings.kill_switch_present` fails closed on any OSError, which is correct for a
    # live account and wrong here: a USB timeout on the lake's drive is not a kill switch,
    # and it made two runs of the same command disagree twice on 2026-09-06.
    risk = RiskEngine(settings, path=None, kill_switch=lambda: None)
    state: dict[str, _CoinState] = {}
    cloid = _CloidCounter()
    result = BacktestResult(
        trades=portfolio.trades,
        equity_curve=[],
        portfolio=portfolio,
        strategy=strategy,
        risk_blocks={},
        rejections=[],
        start_ms=source.start_ms,
        end_ms=source.end_ms,
    )
    clock = _GridClock(settings)
    if progress is not None:
        progress.set(
            coins=list(source.coins),
            window_start_ms=source.start_ms,
            window_end_ms=source.end_ms,
            starting_equity=source.starting_equity,
            equity=source.starting_equity,
        )

    seen: set[str] = set()
    # The previous day's cascade candidates, so a cascade that started at 23:59 keeps its
    # rows and its window across midnight instead of being cut in half by the day loop.
    carry: pl.DataFrame | None = None
    spill: dict[str, list[tuple[int, int]]] = {}

    for index, day in enumerate(source.days()):
        if progress is not None:
            progress.set(stage="features", sim_ms=day.start_ms)
        rows, spans, found, traded = _day_rows(source, day, settings, carry=carry, first=index == 0)
        carry = found
        # Every coin that had a grid, not only the ones that cascaded. It is what the
        # report's coin list has always meant: the universe the run actually covered.
        seen.update(traded)
        spans = _with_spill(spans, spill)
        spans, spill = _clip_to_day(spans, day)
        _load_funding(portfolio, source.funding(day))

        clock.day(rows, settings)
        for start_ms, end_ms, closes in _segments(spans, day):
            if progress is not None:
                progress.set(stage="replaying", sim_ms=start_ms)
            result.replayed_ms += end_ms - start_ms
            result.windows += 1
            _replay(
                source.events(day, _clip_spans(spans, start_ms, end_ms)),
                start_ms,
                end_ms,
                clock=clock,
                state=state,
                portfolio=portfolio,
                strategy=strategy,
                risk=risk,
                result=result,
                meta=meta,
                settings=settings,
                cloid=cloid,
                progress=progress,
            )
            if closes:
                _assert_flat(end_ms, state, portfolio)
        _sample_idle(day, spans, portfolio, risk, result, progress)

    _close_out(source.end_ms, state, portfolio, result, settings)
    result.rejections = strategy.take_rejections()
    result.coins = tuple(sorted(seen))
    # A day appends its in-window samples first and its idle hours afterwards, so the
    # curve is built out of order. The drawdown walks it forward and cares.
    result.equity_curve.sort()
    if progress is not None:
        progress.set(
            stage="replaying",
            sim_ms=source.end_ms,
            trades=len(portfolio.trades),
            net=sum(trade.net_pnl for trade in portfolio.trades),
            intents=result.intents,
        )
    return result


# --- the day loop -------------------------------------------------------------


def _day_rows(
    source: Source,
    day: Day,
    settings: Settings,
    *,
    carry: pl.DataFrame | None,
    first: bool,
) -> tuple[dict[str, pl.DataFrame], dict[str, list[tuple[int, int]]], pl.DataFrame, list[str]]:
    """One day's grid, reduced to the rows a cascade could involve, and its windows.

    Per coin, so a day of 42 coins is never one three million row frame. Each coin's grid
    is built, masked down to the rows the detector could act on, and the rest let go before
    the next coin is read; what survives is a few thousand rows across the whole universe.
    """
    kept: dict[str, pl.DataFrame] = {}
    found: list[pl.DataFrame] = []
    traded: list[str] = []
    for coin, grid in source.grids(day, settings, first=first):
        traded.append(coin)
        coin_carry = (
            None if carry is None or carry.is_empty() else carry.filter(pl.col("coin") == coin)
        )
        mask = active_mask(grid, settings, carry=coin_carry)
        rows = grid.sort("coin", "ts_ms").filter(mask)
        if not rows.is_empty():
            kept[coin] = rows
        found.append(candidates(grid, settings))
    day_candidates = (
        pl.concat(found).sort("ts_ms", "coin")
        if found
        else pl.DataFrame(schema={"coin": pl.String, "ts_ms": pl.Int64})
    )
    return kept, cascade_windows(day_candidates, settings), day_candidates, traded


def _with_spill(
    spans: dict[str, list[tuple[int, int]]], spill: Mapping[str, list[tuple[int, int]]]
) -> dict[str, list[tuple[int, int]]]:
    """Fold yesterday's unfinished windows into today's, merging where they meet."""
    if not spill:
        return spans
    out = {coin: list(coin_spans) for coin, coin_spans in spans.items()}
    for coin, coin_spans in spill.items():
        out.setdefault(coin, []).extend(coin_spans)
    return {coin: merge_spans(coin_spans) for coin, coin_spans in sorted(out.items())}


def _clip_to_day(
    spans: Mapping[str, list[tuple[int, int]]], day: Day
) -> tuple[dict[str, list[tuple[int, int]]], dict[str, list[tuple[int, int]]]]:
    """Split each coin's windows into the part this day replays and the part that spills.

    A window that runs past midnight is not shortened, it is continued: tomorrow's pass
    picks up its remainder with tomorrow's grid and tomorrow's funding in hand, which is
    the only order in which a position open at midnight can be priced correctly.
    """
    today: dict[str, list[tuple[int, int]]] = {}
    spill: dict[str, list[tuple[int, int]]] = {}
    for coin, coin_spans in spans.items():
        for start, end in coin_spans:
            start = max(start, day.start_ms)
            if end <= day.start_ms:
                continue
            if start >= day.end_ms:
                spill.setdefault(coin, []).append((start, end))
                continue
            today.setdefault(coin, []).append((start, min(end, day.end_ms)))
            if end > day.end_ms:
                spill.setdefault(coin, []).append((day.end_ms, end))
    return today, spill


def _segments(spans: Mapping[str, list[tuple[int, int]]], day: Day) -> list[tuple[int, int, bool]]:
    """The stretches of clock to replay today, merged across coins.

    Returns `(start_ms, end_ms, closes)`. `closes` is false when midnight cut the segment
    short, because the window continues tomorrow and a position it opened is meant to still
    be there. Only a segment that genuinely ends may be asserted flat.
    """
    flat = [span for coin_spans in spans.values() for span in coin_spans]
    return [(start, end, end < day.end_ms) for start, end in merge_spans(flat)]


def _clip_spans(
    spans: Mapping[str, list[tuple[int, int]]], start_ms: int, end_ms: int
) -> dict[str, list[tuple[int, int]]]:
    """Each coin's windows, restricted to one segment of the clock."""
    out: dict[str, list[tuple[int, int]]] = {}
    for coin, coin_spans in spans.items():
        clipped = [
            (max(start, start_ms), min(end, end_ms))
            for start, end in coin_spans
            if start < end_ms and end > start_ms
        ]
        if clipped:
            out[coin] = clipped
    return out


class _GridClock:
    """The rows the strategy has most recently seen, carried across segments and days.

    The engine used to hold one list of grid rows for the whole run and an index into it.
    The day loop rebuilds that list every day, so the *index* is per day but `latest` and
    `active_until` are not: a cascade whose window straddles midnight must keep reading the
    row it had at 23:59:59 until the next one arrives.
    """

    def __init__(self, settings: Settings) -> None:
        self.latest: dict[str, FeatureRow] = {}
        self.active_until = -1
        self.step_ms = settings.backtest.feature_grid_ms
        self.rows: list[tuple[int, dict[str, FeatureRow]]] = []
        self.next = 0

    def day(
        self, kept: Mapping[str, pl.DataFrame], settings: Settings
    ) -> list[tuple[int, dict[str, FeatureRow]]]:
        """Merge this day's per-coin rows into one time-ordered list and start on it."""
        frames = [rows for rows in kept.values() if not rows.is_empty()]
        if not frames:
            self.rows, self.next = [], 0
            return self.rows
        merged = pl.concat(frames)
        # Already masked, coin by coin, with the previous day's candidates carried in;
        # `iter_grid_rows` must not mask it a second time off a frame that cannot see them.
        keep = pl.repeat(True, merged.height, eager=True)
        self.rows = list(iter_grid_rows(merged, settings, keep=keep))
        self.next = 0
        return self.rows

    def at(self, now_ms: int) -> Mapping[str, FeatureRow]:
        """The rows in force at `now_ms`, or an empty mapping outside a cascade."""
        while self.next < len(self.rows) and self.rows[self.next][0] <= now_ms:
            self.latest.update(self.rows[self.next][1])
            self.active_until = self.rows[self.next][0] + self.step_ms
            self.next += 1
        return self.latest if now_ms < self.active_until else {}


def _replay(
    events: Iterator[Event],
    start_ms: int,
    end_ms: int,
    *,
    clock: _GridClock,
    state: dict[str, _CoinState],
    portfolio: Portfolio,
    strategy: StrategyEngine,
    risk: RiskEngine,
    result: BacktestResult,
    meta: Mapping[str, AssetMeta],
    settings: Settings,
    cloid: _CloidCounter,
    progress: ProgressWriter | None,
) -> None:
    """One segment: events update state, ticks decide, in the fixed interleaved order."""
    ticks = tick_times(start_ms, end_ms, settings.features.decision_cadence_ms)
    for item in merge(events, ticks):
        if isinstance(item, Tick):
            if progress is not None:
                progress.tick(
                    item.ts_ms,
                    trades=len(portfolio.trades),
                    net=sum(trade.net_pnl for trade in portfolio.trades),
                    equity=result.equity_curve[-1][1]
                    if result.equity_curve
                    else portfolio.starting_equity,
                    intents=result.intents,
                )
            _on_tick(
                item.ts_ms,
                state,
                clock.at(item.ts_ms),
                portfolio,
                strategy,
                risk,
                result,
                meta,
                settings,
                cloid,
            )
        else:
            _on_event(item, state, portfolio, result, settings)


def _assert_flat(end_ms: int, state: Mapping[str, _CoinState], portfolio: Portfolio) -> None:
    """Nothing may survive a window. The bound on a window's end is
    `exhaustion.max_age_s + intent.time_stop_s + backtest.window_margin_s` after the
    candidate that opened it, which is past the last tick the cascade can signal on plus
    the longest a position it opened can live. If this ever fires, the window maths and the
    strategy's own limits have drifted apart and the skipped tape is no longer empty of
    decisions, which is the one way this optimisation could silently change a result.
    """
    open_positions = sorted(portfolio.positions)
    working = sorted(coin for coin, coin_state in state.items() if coin_state.entry is not None)
    assert not open_positions and not working, (
        f"window ending {end_ms} left {open_positions} open and {working} working; "
        f"raise backtest.window_margin_s or check intent.time_stop_s"
    )


def _sample_idle(
    day: Day,
    spans: Mapping[str, list[tuple[int, int]]],
    portfolio: Portfolio,
    risk: RiskEngine,
    result: BacktestResult,
    progress: ProgressWriter | None,
) -> None:
    """One equity sample an hour through the tape this day never replayed.

    The portfolio is flat outside a window, so equity there is cash and does not move; the
    samples exist so the drawdown series is continuous rather than jumping from one cascade
    to the next, and so the risk engine sees at least one mark per UTC day and rolls its
    daily budget at midnight rather than at the first cascade of the day.
    """
    covered = merge_spans([span for coin_spans in spans.values() for span in coin_spans])
    index = 0
    for mark in range(day.start_ms, day.end_ms, HOUR_MS):
        while index < len(covered) and covered[index][1] <= mark:
            index += 1
        if index < len(covered) and covered[index][0] <= mark < covered[index][1]:
            continue
        risk.on_equity(portfolio.cash, mark)
        result.equity_curve.append((mark, portfolio.cash))
        result.last_equity_sample_ms = mark
    if progress is not None:
        progress.set(sim_ms=day.end_ms)


# --- events -------------------------------------------------------------------


def _on_event(
    event: Event,
    state: dict[str, _CoinState],
    portfolio: Portfolio,
    result: BacktestResult,
    settings: Settings,
) -> None:
    """Apply one market event: update the book, then let it fill resting orders."""
    coin = event.coin
    coin_state = state.get(coin)
    if coin_state is None:
        coin_state = state[coin] = _CoinState()
    coin_state.last_data_ms = event.ts_ms

    if isinstance(event, L2):
        coin_state.book = BookState(
            bids=event.bids,
            asks=event.asks,
            snapshot_ms=event.ts_ms,
            last_px=coin_state.book.last_px,
            last_trade_ms=coin_state.book.last_trade_ms,
        )
    elif isinstance(event, Bbo):
        # A quote is a one-level book. It is better than the last trade price and worse
        # than a full snapshot, and the walk degrades naturally when it runs out of levels.
        coin_state.book = BookState(
            bids=((event.bid_px, event.bid_sz),),
            asks=((event.ask_px, event.ask_sz),),
            snapshot_ms=event.ts_ms,
            last_px=coin_state.book.last_px,
            last_trade_ms=coin_state.book.last_trade_ms,
        )
    elif isinstance(event, Trade):
        coin_state.book = BookState(
            bids=coin_state.book.bids,
            asks=coin_state.book.asks,
            snapshot_ms=coin_state.book.snapshot_ms,
            last_px=event.px,
            last_trade_ms=event.ts_ms,
        )
        _fill_against_print(event, coin_state, portfolio, result, settings)
    elif isinstance(event, AssetCtx):
        portfolio.funding.add_sample(coin, event.ts_ms, event.funding)
    elif isinstance(event, Liq):
        # Liquidations only feed the feature grid, which was built before the loop. They
        # still stamp `last_data_ms`, because a liquidation print is data arriving.
        pass


def _fill_against_print(
    trade: Trade,
    coin_state: _CoinState,
    portfolio: Portfolio,
    result: BacktestResult,
    settings: Settings,
) -> None:
    """Offer one print to whichever resting maker orders this coin has."""
    is_buy = trade.side == "buy"
    entry = coin_state.entry
    if entry is not None and entry.kind is OrderKind.ALO:
        fill = fill_maker(
            entry,
            trade_px=trade.px,
            trade_sz=trade.sz,
            trade_is_buy=is_buy,
            now_ms=trade.ts_ms,
            settings=settings,
        )
        if fill is not None and coin_state.intent is not None:
            portfolio.apply_entry(fill, coin_state.intent)
            if entry.remaining <= 0:
                coin_state.entry = None

    exit_order = coin_state.exit_order
    if exit_order is not None and exit_order.kind is OrderKind.ALO:
        fill = fill_maker(
            exit_order,
            trade_px=trade.px,
            trade_sz=trade.sz,
            trade_is_buy=is_buy,
            now_ms=trade.ts_ms,
            settings=settings,
        )
        if fill is not None:
            _book_exit(fill, exit_order, coin_state, portfolio, result)


# --- ticks --------------------------------------------------------------------


def _on_tick(
    now_ms: int,
    state: dict[str, _CoinState],
    rows: Mapping[str, FeatureRow],
    portfolio: Portfolio,
    strategy: StrategyEngine,
    risk: RiskEngine,
    result: BacktestResult,
    meta: Mapping[str, AssetMeta],
    settings: Settings,
    cloid: _CloidCounter,
) -> None:
    """One decision tick, in the fixed order the module docstring names.

    Marking to market is skipped entirely while the book is flat. With nothing open,
    equity is cash and re-deriving it costs a dict comprehension over every coin for an
    answer that has not moved. A month of ten coins is ten million ticks and almost all of
    them are idle, so this is the difference between the speed target being met and not.
    The equity curve is sampled rather than recorded per tick for the same reason: 250ms
    resolution over a year is 126 million points, and the drawdown it feeds does not need
    them.
    """
    if portfolio.positions:
        mids = {coin: coin_state.book.mid() for coin, coin_state in state.items()}
        equity = portfolio.equity(mids)
        _cross_taker_entries(now_ms, state, portfolio, settings)
        _expire_entries(now_ms, state, portfolio, result, settings)
        _check_exits(now_ms, state, mids, portfolio, result, settings, cloid)
    else:
        mids = {}
        equity = portfolio.cash
        if _has_working_orders(state):
            _cross_taker_entries(now_ms, state, portfolio, settings)
            _expire_entries(now_ms, state, portfolio, result, settings)

    risk.on_equity(equity, now_ms)
    if now_ms - result.last_equity_sample_ms >= EQUITY_SAMPLE_MS:
        result.equity_curve.append((now_ms, equity))
        result.last_equity_sample_ms = now_ms

    if not rows:
        return
    intents = strategy.on_tick(now_ms, rows)
    result.intents += len(intents)
    if not intents:
        return
    if not mids:
        mids = {coin: coin_state.book.mid() for coin, coin_state in state.items()}
    for intent in intents:
        _place_entry(
            intent, now_ms, state, rows, mids, portfolio, risk, result, meta, settings, cloid
        )


def _has_working_orders(state: dict[str, _CoinState]) -> bool:
    return any(coin_state.entry is not None for coin_state in state.values())


def _place_entry(
    intent: Intent,
    now_ms: int,
    state: dict[str, _CoinState],
    rows: Mapping[str, FeatureRow],
    mids: Mapping[str, float | None],
    portfolio: Portfolio,
    risk: RiskEngine,
    result: BacktestResult,
    meta: Mapping[str, AssetMeta],
    settings: Settings,
    cloid: _CloidCounter,
) -> None:
    """Size an intent, gate it, and rest an order if it survives both."""
    coin_state = state.setdefault(intent.coin, _CoinState())
    if coin_state.entry is not None or intent.coin in portfolio.positions:
        # Already working or already on. The live order policy reprices rather than
        # stacking a second entry on the same cascade.
        return

    row = rows[intent.coin]
    asset = meta[intent.coin]
    decision = size_intent(
        intent,
        equity=portfolio.equity(mids),
        stats=LiquidityStats(
            traded_usd_window=row.traded_usd_vol_window,
            depth_usd=row.depth(intent.side),
        ),
        meta=asset,
        settings=settings,
        coin_exposure_usd=portfolio.coin_exposure(intent.coin, mids.get(intent.coin)),
        total_exposure_usd=portfolio.gross_exposure(mids),
    )
    if decision.reason is not None:
        result.sizing_blocks[decision.reason] = result.sizing_blocks.get(decision.reason, 0) + 1
        return
    if decision.depth_cap_applied:
        result.depth_capped_entries += 1

    request = EntryRequest(
        coin=intent.coin,
        side=intent.side,
        notional_usd=decision.notional_usd,
        now_ms=now_ms,
    )
    verdict = risk.check_entry(
        request,
        AccountView(
            equity=portfolio.equity(mids),
            open_sides=portfolio.open_sides(),
            coin_last_data_ms={
                coin: cs.last_data_ms for coin, cs in state.items() if cs.last_data_ms is not None
            },
            global_last_data_ms=max(
                (cs.last_data_ms for cs in state.values() if cs.last_data_ms is not None),
                default=None,
            ),
        ),
    )
    if not verdict.allowed:
        assert verdict.reason is not None
        result.risk_blocks[verdict.reason] = result.risk_blocks.get(verdict.reason, 0) + 1
        return

    risk.on_entry(request)
    coin_state.intent = intent
    coin_state.reprices = 0
    coin_state.entry = _entry_order(
        intent, float(decision.size), coin_state, now_ms, settings, cloid
    )
    result.entries_placed += 1


def _entry_order(
    intent: Intent,
    size: float,
    coin_state: _CoinState,
    now_ms: int,
    settings: Settings,
    cloid: _CloidCounter,
) -> SimOrder:
    """Build the entry order, with the queue we assume is ahead of us.

    Whether it crosses is `execution.entry_tif`, plus the high-urgency exception in
    `intent.allow_taker_entry_high_urgency`.

    **This choice decides whether the run has any trades at all, and the default is the
    strict one.** Under `Alo` with `exhaustion.mode: fixed_delay`, the entry is placed 120s
    after the cascade started, by which time the reversion the strategy is trying to catch
    is already underway; a passive bid sits behind a rising market, misses through all
    `execution.max_reprices`, and is abandoned. That is what the policy in
    `docs/STRATEGY_AND_RISK.md` section 3 actually implies, and the report counts every
    abandonment so it is visible rather than mysterious.

    Phase 3 measured something different. Q1's net-stress figure prices the first print
    after the extreme plus one second and subtracts a full effective spread **per side**,
    which is a crossing entry. Reproducing those numbers therefore needs
    `execution.entry_tif: Ioc` in the run config. The two are both defensible and they are
    not the same strategy, so the key is honoured here rather than hardcoded either way.
    """
    taker = settings.execution.entry_tif == "Ioc" or (
        intent.urgency == "high" and settings.intent.allow_taker_entry_high_urgency
    )
    levels = coin_state.book.levels(intent.side)
    # Everything displayed at our price is assumed to be ahead of us. With no book we
    # assume an empty queue, which is optimistic, and the no-book fills are counted so a
    # reader knows how much of the run rested on that assumption.
    queue = next((sz for px, sz in levels if px == intent.entry_px), 0.0)
    return SimOrder(
        cloid=cloid.next(),
        coin=intent.coin,
        side=intent.side,
        kind=OrderKind.IOC if taker else OrderKind.ALO,
        px=intent.entry_px,
        size=size,
        visible_ms=now_ms + settings.backtest.latency_ms,
        queue_ahead=queue,
        tag="entry",
    )


def _cross_taker_entries(
    now_ms: int,
    state: dict[str, _CoinState],
    portfolio: Portfolio,
    settings: Settings,
) -> None:
    """Execute a high urgency entry against the book as soon as it becomes visible.

    `Ioc` means fill what is there and cancel the rest, so unlike an `Alo` it never rests
    waiting for a print to come to it. Whatever the book cannot fill is cancelled rather
    than left working: an Ioc that quietly stayed on the book would be a different order
    type than the one the strategy asked for, and would fill later at a price the urgency
    flag exists to avoid waiting for.
    """
    for coin in sorted(state):
        coin_state = state[coin]
        entry = coin_state.entry
        if entry is None or entry.kind is not OrderKind.IOC or now_ms < entry.visible_ms:
            continue
        fill = fill_taker(entry, coin_state.book, now_ms=now_ms, settings=settings)
        if fill is not None and coin_state.intent is not None:
            portfolio.apply_entry(fill, coin_state.intent)
        else:
            # Nothing to fill against, which on this lake means no book and no tape yet.
            portfolio.abandoned_entries += 1
            coin_state.intent = None
        coin_state.entry = None


def _expire_entries(
    now_ms: int,
    state: dict[str, _CoinState],
    portfolio: Portfolio,
    result: BacktestResult,
    settings: Settings,
) -> None:
    """Reprice a resting entry past its TTL, and abandon it past `max_reprices`.

    The same policy the live order manager will run. A backtest that let entries rest
    forever would report a maker fill rate nobody is going to achieve.
    """
    ttl_ms = int(settings.execution.entry_ttl_s * 1000)
    for coin_state in state.values():
        entry = coin_state.entry
        # Only a resting Alo expires. An Ioc is crossed or cancelled the moment it becomes
        # visible, by _cross_taker_entries, and never reaches here.
        if entry is None or entry.kind is not OrderKind.ALO:
            continue
        if now_ms < entry.visible_ms + ttl_ms:
            continue
        if coin_state.reprices >= settings.execution.max_reprices:
            coin_state.entry = None
            coin_state.intent = None
            portfolio.abandoned_entries += 1
            continue
        coin_state.reprices += 1
        touch = _touch(coin_state.book, entry.side)
        if touch is None:
            continue
        levels = coin_state.book.levels(entry.side)
        entry.px = touch
        entry.queue_ahead = next((sz for px, sz in levels if px == touch), 0.0)
        entry.traded_at_px = 0.0
        entry.visible_ms = now_ms + settings.backtest.latency_ms


def _check_exits(
    now_ms: int,
    state: dict[str, _CoinState],
    mids: Mapping[str, float | None],
    portfolio: Portfolio,
    result: BacktestResult,
    settings: Settings,
    cloid: _CloidCounter,
) -> None:
    """Target, stop and time stop, in that order of preference.

    The stop is checked against mid and, when it has gapped, fills at the gapped price.
    The target rests as a maker order, because that is what the live policy does with a
    profit target and it is where the maker rebate comes from.
    """
    for coin in sorted(portfolio.positions):
        position = portfolio.positions[coin]
        coin_state = state.get(coin)
        if coin_state is None:
            continue
        mid = mids.get(coin)
        if mid is None:
            continue
        intent = position.intent

        if stop_triggered(side=position.side, stop_px=intent.stop_px, mid=mid):
            _take_exit(
                coin,
                coin_state,
                asked_px=intent.stop_px,
                mid=mid,
                reason="stop",
                now_ms=now_ms,
                portfolio=portfolio,
                settings=settings,
                cloid=cloid,
            )
            continue
        age_s = (now_ms - position.opened_ms) / 1000.0
        if age_s >= intent.time_stop_s:
            _take_exit(
                coin,
                coin_state,
                asked_px=mid,
                mid=mid,
                reason="time_stop",
                now_ms=now_ms,
                portfolio=portfolio,
                settings=settings,
                cloid=cloid,
            )
            continue
        if coin_state.exit_order is None:
            coin_state.exit_order = SimOrder(
                cloid=cloid.next(),
                coin=coin,
                # A long position exits by selling, which rests on the offer.
                side="short" if position.is_long else "long",
                kind=OrderKind.ALO,
                px=intent.target_px,
                size=position.size,
                visible_ms=now_ms + settings.backtest.latency_ms,
                reduce_only=True,
                tag="target",
            )


def _take_exit(
    coin: str,
    coin_state: _CoinState,
    *,
    asked_px: float,
    mid: float,
    reason: str,
    now_ms: int,
    portfolio: Portfolio,
    settings: Settings,
    cloid: _CloidCounter,
) -> None:
    """Cross out of a position now, at whatever the market is actually offering."""
    position = portfolio.positions.get(coin)
    if position is None:
        return
    order = SimOrder(
        cloid=cloid.next(),
        coin=coin,
        side="short" if position.is_long else "long",
        kind=OrderKind.IOC,
        px=asked_px,
        size=position.size,
        # Visible immediately: the stop and the time stop are decisions the engine takes at
        # this tick against a price it has already observed, and the crossing order is the
        # reaction. The entry carries the latency because that is where the edge is;
        # delaying the exit too would double-count it.
        visible_ms=now_ms,
        reduce_only=True,
        tag=reason,
    )
    if reason == "stop":
        order = gapped_stop_order(order, mid=mid)
    fill = fill_taker(order, coin_state.book, now_ms=now_ms, settings=settings)
    if fill is None:
        return
    coin_state.exit_order = None
    portfolio.apply_exit(fill, reason)


def _book_exit(
    fill: Fill,
    order: SimOrder,
    coin_state: _CoinState,
    portfolio: Portfolio,
    result: BacktestResult,
) -> None:
    portfolio.apply_exit(fill, order.tag)
    if order.remaining <= 0:
        coin_state.exit_order = None


def _close_out(
    end_ms: int,
    state: dict[str, _CoinState],
    portfolio: Portfolio,
    result: BacktestResult,
    settings: Settings,
) -> None:
    """Flatten whatever is still open when the window ends.

    Marked out at the last known price rather than left open. A run that ended holding
    positions would report an equity curve whose last point was a guess, and the trade
    count would not match the number of round trips.
    """
    for coin in sorted(portfolio.positions):
        coin_state = state.get(coin)
        if coin_state is None:
            continue
        mid = coin_state.book.mid()
        if mid is None:
            continue
        position = portfolio.positions[coin]
        order = SimOrder(
            cloid="close-out",
            coin=coin,
            side="short" if position.is_long else "long",
            kind=OrderKind.IOC,
            px=mid,
            size=position.size,
            visible_ms=end_ms,
            reduce_only=True,
            tag="window_end",
        )
        fill = fill_taker(order, coin_state.book, now_ms=end_ms, settings=settings)
        if fill is not None:
            portfolio.apply_exit(fill, "window_end")


# --- helpers ------------------------------------------------------------------


class _CloidCounter:
    """Monotonic client order ids. Deterministic, so a rerun produces the same log.

    CLAUDE.md requires every order to carry a cloid and be tracked, and a simulated order
    is no exception: a fill in `trades.parquet` has to be traceable to the order that
    produced it.
    """

    def __init__(self) -> None:
        self._n = 0

    def next(self) -> str:
        self._n += 1
        return f"bt-{self._n:08d}"


def _touch(book: BookState, side: str) -> float | None:
    """The price a `side` entry would rest at: the bid to buy, the offer to sell."""
    levels = book.bids if side == "long" else book.asks
    if levels:
        return levels[0][0]
    return book.last_px


def _resolve_meta(
    provided: Mapping[str, AssetMeta], coins: Sequence[str], settings: Settings
) -> dict[str, AssetMeta]:
    """Fill in metadata for any coin the caller did not describe.

    The defaults are the choices that constrain the run most: the finest `szDecimals`
    Hyperliquid uses, and its most conservative leverage tier. A backtest that invented a
    generous asset spec would report sizes the venue would have refused.

    `listing_days` defaults to just over `universe.min_listing_days`, because a historical
    window has no listing dates and refusing every coin for being undateable would produce
    a run with no trades and no explanation.
    """
    core = set(settings.universe.core_coins)
    resolved = dict(provided)
    for coin in coins:
        if coin in resolved:
            continue
        resolved[coin] = AssetMeta(
            coin=coin,
            sz_decimals=DEFAULT_SZ_DECIMALS,
            max_leverage=DEFAULT_MAX_LEVERAGE,
            tier="core" if coin in core else "major",
            listing_days=float(settings.universe.min_listing_days + 1),
        )
    return resolved


def _load_funding(portfolio: Portfolio, asset_ctx: pl.DataFrame | None) -> None:
    """Preload the funding book so a position closing early in the run can price itself."""
    if asset_ctx is None or asset_ctx.is_empty():
        return
    for row in asset_ctx.iter_rows(named=True):
        stamp = row["rx_ms"] if row.get("rx_ms") else row["ts_ms"]
        portfolio.funding.add_sample(str(row["coin"]), int(stamp), row.get("funding"))


def _or_none(frame: pl.DataFrame) -> pl.DataFrame | None:
    return None if frame.is_empty() else frame


def _within(
    frame: pl.DataFrame | None, spans: Mapping[str, Sequence[tuple[int, int]]]
) -> pl.DataFrame:
    """The rows of an in-memory table falling inside a coin's replay windows.

    The same predicate `load_day` pushes into the scan, so a `RunInputs` built by a test
    and a `LakeSource` reading the lake keep exactly the same rows.
    """
    if frame is None or frame.is_empty() or not spans:
        return pl.DataFrame()
    return frame.filter(span_filter(spans))
