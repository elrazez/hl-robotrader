"""Cash, positions, equity, and the order lifecycle the live loop will run.

The portfolio is the backtest's account. It applies fills, marks open positions to mid on
every tick, and closes a position out when the target, the stop or the time stop is reached.
Equity from here is what `sizing/` and `risk/` read, so a backtest sizes against the account
it actually has rather than against a constant.

**Entries expire and reprice like the live order policy.** A resting entry lives
`execution.entry_ttl_s` and is then repriced, up to `execution.max_reprices` times, after
which the intent is abandoned. That is not an optimisation, it is the point: the backtest
has to exercise the same order lifecycle Phase 5 will run, or the maker fill rate it reports
is about a policy nobody is going to use. An abandoned entry is recorded as such, so the
report can show how many intents never became trades and why.

Realized PnL is booked at the close, funding at the close, fees at every fill. Nothing here
reports a gross number: `Trade.net_pnl` is the only PnL field and it is after fees and
funding, per CLAUDE.md rule 2.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from liqfade.backtest.costs import FundingBook, fee_for
from liqfade.backtest.fills import Fill
from liqfade.config.models import Settings
from liqfade.strategy.types import Intent, Side

__all__ = ["Portfolio", "Position", "Trade"]


@dataclass(slots=True)
class Position:
    """One open position, and the intent that opened it.

    Args:
        coin: the symbol.
        side: `long` or `short`.
        size: coin units held, always positive.
        entry_px: volume weighted average entry.
        opened_ms: the first fill's timestamp.
        intent: the intent that produced it, carrying the target, stop and time stop.
        fees_usd: fees paid so far on this position, entry side included.
        entry_slippage_bps: slippage on the entry fills, size weighted.
        no_book_entry: whether any entry fill used the degraded no-snapshot model.
    """

    coin: str
    side: Side
    size: float
    entry_px: float
    opened_ms: int
    intent: Intent
    fees_usd: float = 0.0
    entry_slippage_bps: float = 0.0
    no_book_entry: bool = False

    @property
    def is_long(self) -> bool:
        return self.side == "long"

    def notional(self, px: float) -> float:
        return self.size * px

    def unrealized(self, mid: float) -> float:
        """Mark to market at `mid`, USD."""
        direction = 1.0 if self.is_long else -1.0
        return direction * (mid - self.entry_px) * self.size

    def entry_notional(self, size: float) -> float:
        """Notional of `size` units at the entry price, USD. What funding accrues on."""
        return size * self.entry_px


@dataclass(slots=True)
class Trade:
    """One completed round trip. This is a row of `trades.parquet`.

    Every PnL field is net. `gross_pnl` exists beside `net_pnl` only so a report can show
    what the costs took, never as a headline: CLAUDE.md rule 2 forbids reporting gross.
    """

    coin: str
    side: Side
    size: float
    entry_px: float
    exit_px: float
    entry_ms: int
    exit_ms: int
    exit_reason: str
    gross_pnl: float
    fees_usd: float
    funding_usd: float
    net_pnl: float
    entry_slippage_bps: float
    exit_slippage_bps: float
    no_book_entry: bool
    no_book_exit: bool
    urgency: str
    cascade_notional_usd: float
    explain: Mapping[str, float] = field(default_factory=dict)

    @property
    def hold_s(self) -> float:
        return (self.exit_ms - self.entry_ms) / 1000.0

    @property
    def net_bps(self) -> float:
        """Net PnL as basis points of entry notional, which is how research quotes it."""
        notional = self.size * self.entry_px
        return 1e4 * self.net_pnl / notional if notional else 0.0


class Portfolio:
    """Cash, open positions and the completed trade log."""

    def __init__(self, settings: Settings, *, starting_equity: float) -> None:
        """
        Args:
            settings: resolved config.
            starting_equity: the account's opening balance, USD. Comes from the run config
                rather than from anywhere in the package: CLAUDE.md forbids hardcoding
                capital, and the backtest is no exception.
        """
        self.settings = settings
        self.cash = starting_equity
        self.starting_equity = starting_equity
        self.positions: dict[str, Position] = {}
        self.trades: list[Trade] = []
        self.funding = FundingBook()
        self.no_book_fills = 0
        self.total_fills = 0
        self.abandoned_entries = 0

    # --- marks ----------------------------------------------------------------

    def equity(self, mids: Mapping[str, float | None]) -> float:
        """Cash plus every open position marked to mid.

        A coin with no mid is marked at its entry, which is the last price we know it to
        have been worth. Marking it at zero would be a fictional loss and marking it out
        would hide a real position from the risk gates.
        """
        total = self.cash
        for coin, position in self.positions.items():
            mid = mids.get(coin)
            total += position.unrealized(mid if mid is not None else position.entry_px)
        return total

    def gross_exposure(self, mids: Mapping[str, float | None]) -> float:
        """Total notional open, USD, for the total exposure cap."""
        total = 0.0
        for coin, position in self.positions.items():
            mid = mids.get(coin)
            total += position.notional(mid if mid is not None else position.entry_px)
        return total

    def coin_exposure(self, coin: str, mid: float | None) -> float:
        position = self.positions.get(coin)
        if position is None:
            return 0.0
        return position.notional(mid if mid is not None else position.entry_px)

    def open_sides(self) -> dict[str, Side]:
        """Coin to side, for the risk engine's concurrency gates."""
        return {coin: position.side for coin, position in self.positions.items()}

    # --- fills ----------------------------------------------------------------

    def apply_entry(self, fill: Fill, intent: Intent) -> None:
        """Book an entry fill, opening or adding to a position."""
        self._count(fill)
        fee = fee_for(fill.size * fill.px, maker=fill.maker, settings=self.settings)
        self.cash -= fee
        side: Side = "long" if fill.is_buy else "short"
        existing = self.positions.get(fill.coin)
        if existing is None:
            self.positions[fill.coin] = Position(
                coin=fill.coin,
                side=side,
                size=fill.size,
                entry_px=fill.px,
                opened_ms=fill.ts_ms,
                intent=intent,
                fees_usd=fee,
                entry_slippage_bps=fill.slippage_bps,
                no_book_entry=fill.no_book,
            )
            return
        # Adding to a position: the entry price and the slippage are both size weighted, so
        # a second fill at a worse price shows up in both.
        total = existing.size + fill.size
        existing.entry_px = (existing.entry_px * existing.size + fill.px * fill.size) / total
        existing.entry_slippage_bps = (
            existing.entry_slippage_bps * existing.size + fill.slippage_bps * fill.size
        ) / total
        existing.size = total
        existing.fees_usd += fee
        existing.no_book_entry = existing.no_book_entry or fill.no_book

    def apply_exit(self, fill: Fill, reason: str) -> Trade | None:
        """Book an exit fill and, when it closes the position, produce the trade row."""
        position = self.positions.get(fill.coin)
        if position is None:
            return None
        self._count(fill)
        fee = fee_for(fill.size * fill.px, maker=fill.maker, settings=self.settings)
        self.cash -= fee
        position.fees_usd += fee

        closed = min(fill.size, position.size)
        direction = 1.0 if position.is_long else -1.0
        gross = direction * (fill.px - position.entry_px) * closed
        self.cash += gross
        position.size -= closed
        if position.size > 1e-12:
            return None

        funding = self.funding.cost(
            coin=position.coin,
            notional_usd=position.entry_notional(closed),
            is_long=position.is_long,
            open_ms=position.opened_ms,
            close_ms=fill.ts_ms,
            settings=self.settings,
        )
        self.cash -= funding
        del self.positions[fill.coin]
        trade = Trade(
            coin=position.coin,
            side=position.side,
            size=closed,
            entry_px=position.entry_px,
            exit_px=fill.px,
            entry_ms=position.opened_ms,
            exit_ms=fill.ts_ms,
            exit_reason=reason,
            gross_pnl=gross,
            fees_usd=position.fees_usd,
            funding_usd=funding,
            net_pnl=gross - position.fees_usd - funding,
            entry_slippage_bps=position.entry_slippage_bps,
            exit_slippage_bps=fill.slippage_bps,
            no_book_entry=position.no_book_entry,
            no_book_exit=fill.no_book,
            urgency=position.intent.urgency,
            cascade_notional_usd=position.intent.explain.get("cascade_notional_usd", 0.0),
            explain=dict(position.intent.explain),
        )
        self.trades.append(trade)
        return trade

    def _count(self, fill: Fill) -> None:
        self.total_fills += 1
        if fill.no_book:
            self.no_book_fills += 1

    @property
    def no_book_share(self) -> float:
        """Fraction of fills priced without a book snapshot, in `[0, 1]`."""
        return self.no_book_fills / self.total_fills if self.total_fills else 0.0
