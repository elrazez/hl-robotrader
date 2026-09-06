"""A merged, time-ordered event iterator over the Parquet lake.

The features are vectorized on a grid (`backtest/features.py`); the fills are not, because a
fill depends on the exact order of prints against a resting price and a bar cannot say
which came first. This module is the event side of that split: it reads the lake for a set
of coins and a window, and yields typed events in the order they became knowable.

**Ordering is by `effective_ts`, which is `rx_ms` when it is set and `ts_ms` otherwise.**
Recorded rows carry a local receive time, which is the only time a live decision could have
used them; historical pulls write `rx_ms = 0` and are ordered on exchange time, because
that is the best the archive can offer. Mixing the two in one window is fine and the column
says which is which. Ties break on a fixed key so a rerun produces the same order and
therefore the same fills, which is what makes the determinism test possible.

**Missing tables are allowed and are not an error.** The historical year has no `bbo` at
all, `l2` covers four coins only, and `asset_ctx` is missing two days the archive never
published. A lake with a hole in it produces a degraded fill model and a report that says
so, not a traceback: see `backtest/fills.py` for what "degraded" means and `no_book_fills`
for how it is counted.

**`l2` is the largest table in the lake**, not the smallest: a snapshot roughly every
0.55s on each of four coins, twenty levels a side, is 12.6 million rows a day. Reading it
over a range is what killed the first year run, which is why `load_day` exists and
`load_window` does not.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

__all__ = [
    "L2",
    "AssetCtx",
    "Bbo",
    "Event",
    "Liq",
    "Trade",
    "day_files",
    "iter_events",
    "load_day",
    "utc_days",
]

# Columns each table is read with. Projected in the scan so whole row groups are skipped
# rather than read and thrown away, which is the difference between a year of trades being
# minutes and being impossible.
COLUMNS: dict[str, tuple[str, ...]] = {
    "trades": ("coin", "ts_ms", "rx_ms", "px", "sz", "side"),
    "liquidations": ("venue", "coin", "ts_ms", "rx_ms", "side", "px", "sz", "notional_usd"),
    "bbo": ("coin", "ts_ms", "rx_ms", "bid_px", "bid_sz", "ask_px", "ask_sz"),
    "l2": ("coin", "ts_ms", "rx_ms", "level", "bid_px", "bid_sz", "ask_px", "ask_sz"),
    # Every column `features/context.py` reads, plus mid_px. Projecting fewer would push
    # the failure into the feature grid, where the error names a column rather than a
    # table and is much harder to place.
    "asset_ctx": (
        "coin",
        "ts_ms",
        "rx_ms",
        "mark_px",
        "oracle_px",
        "mid_px",
        "funding",
        "premium",
        "open_interest",
    ),
    "candles_1m": ("coin", "ts_ms", "o", "h", "l", "c", "v", "n"),
}


@dataclass(frozen=True, slots=True)
class Trade:
    """One print on the tape. `side` is the aggressor: `buy` lifted the ask."""

    ts_ms: int
    coin: str
    px: float
    sz: float
    side: str


@dataclass(frozen=True, slots=True)
class Bbo:
    """A top-of-book update. Absent for the whole historical year."""

    ts_ms: int
    coin: str
    bid_px: float
    bid_sz: float
    ask_px: float
    ask_sz: float


@dataclass(frozen=True, slots=True)
class L2:
    """One book snapshot, twenty levels a side, already assembled from its rows.

    `bids` and `asks` are `(price, size)` ordered outwards from the touch, which is the
    order a taker walk consumes them in.
    """

    ts_ms: int
    coin: str
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]


@dataclass(frozen=True, slots=True)
class Liq:
    """One liquidation. `side` is the side that was liquidated, not the aggressor."""

    ts_ms: int
    coin: str
    venue: str
    side: str
    px: float
    notional_usd: float


@dataclass(frozen=True, slots=True)
class AssetCtx:
    """A funding, mark and oracle sample. Hourly funding is read from the HH:59 row."""

    ts_ms: int
    coin: str
    mark_px: float | None
    oracle_px: float | None
    mid_px: float | None
    funding: float | None


Event = Trade | Bbo | L2 | Liq | AssetCtx

# Sort order within a timestamp. Fixed so a rerun of the same window produces the same
# sequence and therefore byte-identical fills. Book state is applied before the prints that
# might fill against it, and liquidations last because they only feed features.
_KIND_ORDER: dict[str, int] = {"l2": 0, "bbo": 1, "trade": 2, "asset_ctx": 3, "liq": 4}


def utc_days(start_ms: int, end_ms: int) -> list[str]:
    """Every `YYYY-MM-DD` partition the window touches, inclusive of both ends."""
    first = datetime.fromtimestamp(start_ms / 1000, UTC).date()
    last = datetime.fromtimestamp(max(end_ms - 1, start_ms) / 1000, UTC).date()
    days, day = [], first
    while day <= last:
        days.append(day.isoformat())
        day = day + timedelta(days=1)
    return days


def day_files(data_dir: Path, table: str, day: str) -> list[Path]:
    """The Parquet files of one table on one day, skipping hidden ones.

    macOS leaves a 4 KiB AppleDouble `._x.parquet` beside every file our code writes onto
    an exFAT drive, which is not Parquet at all and which `read_parquet` fails on. That is
    a crash on the owner's external disk and on nobody's development machine, so the filter
    is here rather than left to the caller.
    """
    directory = Path(data_dir) / "parquet" / table / f"date={day}"
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.parquet") if not path.name.startswith("."))


def load_day(
    data_dir: Path,
    table: str,
    day: str,
    *,
    coins: Sequence[str] | None = None,
    venue: str | None = None,
    spans: Mapping[str, Sequence[tuple[int, int]]] | None = None,
) -> pl.DataFrame:
    """Read **one UTC day** of one table, projected and filtered inside the scan.

    One day, never a range. A year of trades is 1.79 billion rows and a year of `l2` is
    over four billion; the first version of this engine handed `scan_parquet` every file in
    the window and the OS killed the run twice. There is deliberately no function here that
    takes a start and an end date, so that mistake cannot be made again by accident.

    Args:
        data_dir: lake root.
        table: one of `COLUMNS`.
        day: `YYYY-MM-DD`, one partition.
        coins: restrict to these coins, or None for every coin in the file.
        venue: restrict to one venue, where the table has the column.
        spans: per coin, the `[from_ms, to_ms)` spans on `effective_ts` to keep. This is
            the whole point of the streaming engine: outside a cascade window the tape is
            never read at all, so a busy day whose cascades cover 3% of the clock reads
            roughly 3% of its rows rather than fifty million and throwing them away. None
            reads the whole day.

    Returns:
        The rows, or an empty frame when the day is absent or nothing survives the filters.
        Absence is not an error: `bbo` is absent for the whole historical year and the fill
        model has a documented answer for it.
    """
    files = day_files(data_dir, table, day)
    if not files:
        return pl.DataFrame()
    frame = pl.scan_parquet(files).select(list(COLUMNS[table]))
    if coins is not None:
        frame = frame.filter(pl.col("coin").is_in(sorted(set(coins))))
    if venue is not None and "venue" in COLUMNS[table]:
        frame = frame.filter(pl.col("venue") == venue)
    if spans is not None:
        if not spans:
            return pl.DataFrame()
        known = _effective_ts()
        frame = frame.filter(
            pl.any_horizontal(
                [
                    (pl.col("coin") == coin)
                    & pl.any_horizontal(
                        [known.is_between(start, end, closed="left") for start, end in coin_spans]
                    )
                    for coin, coin_spans in sorted(spans.items())
                    if coin_spans
                ]
                or [pl.lit(False)]
            )
        )
    return frame.collect()


def iter_events(
    *,
    trades: pl.DataFrame,
    liquidations: pl.DataFrame | None = None,
    bbo: pl.DataFrame | None = None,
    l2: pl.DataFrame | None = None,
    asset_ctx: pl.DataFrame | None = None,
) -> Iterator[Event]:
    """Merge every table into one time-ordered stream of typed events.

    Args:
        trades: canonical `trades` rows. The only table that must be present.
        liquidations: canonical `liquidations`, or None.
        bbo: canonical `bbo`, or None.
        l2: canonical `l2`, twenty rows per snapshot. Assembled into `L2` events here.
        asset_ctx: canonical `asset_ctx`, or None.

    Yields:
        Events in `effective_ts` order. Within one timestamp the order is book, then quote,
        then print, then context, then liquidation, which is fixed so that a resting order
        sees the book state that preceded the print it might fill against, and so that two
        runs of the same window produce the same fills.
    """
    rows: list[tuple[int, int, int, Event]] = []
    rows.extend(_trade_events(trades))
    rows.extend(_bbo_events(bbo))
    rows.extend(_l2_events(l2))
    rows.extend(_ctx_events(asset_ctx))
    rows.extend(_liq_events(liquidations))
    rows.sort(key=lambda item: (item[0], item[1], item[2]))
    for _, _, _, event in rows:
        yield event


# --- internals ----------------------------------------------------------------


def _effective_ts() -> pl.Expr:
    """`rx_ms` when the row was recorded, `ts_ms` when it was backfilled."""
    return pl.when(pl.col("rx_ms") > 0).then(pl.col("rx_ms")).otherwise(pl.col("ts_ms"))


def _known(frame: pl.DataFrame) -> pl.DataFrame:
    """Add the `known` column the merge sorts on. `candles_1m` has no `rx_ms`."""
    if "rx_ms" not in frame.columns:
        return frame.with_columns(pl.col("ts_ms").alias("known"))
    return frame.with_columns(_effective_ts().alias("known"))


def _trade_events(frame: pl.DataFrame | None) -> list[tuple[int, int, int, Event]]:
    """The tape, which is the largest table by far.

    Columns are pulled out as Python lists rather than read through `iter_rows(named=True)`,
    which builds a dict per row: a month of ten coins is 26 million prints and the dicts
    alone cost more than the rest of the merge put together.
    """
    if frame is None or frame.is_empty():
        return []
    order = _KIND_ORDER["trade"]
    marked = _known(frame)
    known = marked.get_column("known").to_list()
    coins = marked.get_column("coin").to_list()
    prices = marked.get_column("px").to_list()
    sizes = marked.get_column("sz").to_list()
    sides = marked.get_column("side").to_list()
    return [
        (
            known[index],
            order,
            index,
            Trade(
                ts_ms=known[index],
                coin=coins[index],
                px=prices[index],
                sz=sizes[index],
                side=sides[index],
            ),
        )
        for index in range(marked.height)
    ]


def _bbo_events(frame: pl.DataFrame | None) -> list[tuple[int, int, int, Event]]:
    if frame is None or frame.is_empty():
        return []
    order = _KIND_ORDER["bbo"]
    return [
        (
            int(row["known"]),
            order,
            index,
            Bbo(
                ts_ms=int(row["known"]),
                coin=str(row["coin"]),
                bid_px=float(row["bid_px"]),
                bid_sz=float(row["bid_sz"]),
                ask_px=float(row["ask_px"]),
                ask_sz=float(row["ask_sz"]),
            ),
        )
        for index, row in enumerate(_known(frame).iter_rows(named=True))
    ]


def _l2_events(frame: pl.DataFrame | None) -> list[tuple[int, int, int, Event]]:
    """Assemble the twenty rows of a snapshot into one event.

    The lake stores one row per level, so a snapshot is a group. Levels are sorted outwards
    from the touch here rather than trusted from the file, because a taker walk consuming
    them in the wrong order would fill at the far side of the book and report it as
    slippage.
    """
    if frame is None or frame.is_empty():
        return []
    order = _KIND_ORDER["l2"]
    events: list[tuple[int, int, int, Event]] = []
    marked = _known(frame).sort("coin", "known", "level")
    for index, ((coin, known), group) in enumerate(
        marked.group_by(["coin", "known"], maintain_order=True)
    ):
        bids = [
            (float(px), float(sz))
            for px, sz in zip(group["bid_px"], group["bid_sz"], strict=True)
            if px is not None and sz is not None
        ]
        asks = [
            (float(px), float(sz))
            for px, sz in zip(group["ask_px"], group["ask_sz"], strict=True)
            if px is not None and sz is not None
        ]
        events.append(
            (
                int(known),
                order,
                index,
                L2(
                    ts_ms=int(known),
                    coin=str(coin),
                    bids=tuple(sorted(bids, key=lambda level: -level[0])),
                    asks=tuple(sorted(asks, key=lambda level: level[0])),
                ),
            )
        )
    return events


def _ctx_events(frame: pl.DataFrame | None) -> list[tuple[int, int, int, Event]]:
    if frame is None or frame.is_empty():
        return []
    order = _KIND_ORDER["asset_ctx"]
    return [
        (
            int(row["known"]),
            order,
            index,
            AssetCtx(
                ts_ms=int(row["known"]),
                coin=str(row["coin"]),
                mark_px=_optional(row.get("mark_px")),
                oracle_px=_optional(row.get("oracle_px")),
                mid_px=_optional(row.get("mid_px")),
                funding=_optional(row.get("funding")),
            ),
        )
        for index, row in enumerate(_known(frame).iter_rows(named=True))
    ]


def _liq_events(frame: pl.DataFrame | None) -> list[tuple[int, int, int, Event]]:
    if frame is None or frame.is_empty():
        return []
    order = _KIND_ORDER["liq"]
    return [
        (
            int(row["known"]),
            order,
            index,
            Liq(
                ts_ms=int(row["known"]),
                coin=str(row["coin"]),
                venue=str(row["venue"]),
                side=str(row["side"]),
                px=float(row["px"]),
                notional_usd=float(row["notional_usd"] or 0.0),
            ),
        )
        for index, row in enumerate(_known(frame).iter_rows(named=True))
    ]


def _optional(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None
