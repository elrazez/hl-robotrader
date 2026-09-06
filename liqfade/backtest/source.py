"""Where a replay gets its data: one UTC day at a time, never a range.

The engine used to take `RunInputs`, a set of frames covering the whole run, and
`load_inputs` filled them by handing `scan_parquet` every file in the window. On the top
four coins a year is 1.79 billion trade rows and over four billion `l2` rows, so the OS
killed the run twice on an 8 GB machine before it printed anything. This module is the
replacement: a **day source**, which the engine walks one day at a time and which never
holds more than one day of one coin.

Two of them, and they are the same shape rather than an interface with one implementation:

- `LakeSource` reads the Parquet lake. It builds each day's feature grid from that day plus
  the previous day in front of it, caches the result, and reads raw tape only inside the
  cascade windows the grid names.
- `RunInputs` (in `engine.py`) wraps frames a test built by hand. It answers the same
  questions off what it already holds, so `tests/test_backtest_golden.py` still builds a
  lake in memory and the engine cannot tell the difference.

**The grid cache.** `<data_dir>/cache/features/<feature_hash>/date=YYYY-MM-DD/grid.parquet`,
where `feature_hash` covers exactly the config a feature column depends on. A second run
that changes a stop, a target, a risk limit or a sizing fraction reads every day's grid
back and builds nothing, which is what makes a parameter sweep over a year affordable.
`--no-cache` rebuilds. See `docs/DATA_SOURCES.md` for what it costs on disk.

**The run's first day is never cached and never read from cache.** Every other day is
built with the previous day in front of it, so its rolling windows and its 24h z-score
lookback are full and its grid is a function of the date alone. The first day has no
previous day inside the run window and is therefore cold-started, exactly as the
non-streaming engine was. Caching that under the same key would hand a later run a day
whose z-scores came from a lookback that was not there.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import pyarrow.parquet as pq

from liqfade.backtest.data import Event, iter_events, load_day, utc_days
from liqfade.backtest.features import build_feature_grid, row_columns
from liqfade.config.models import Settings
from liqfade.log import get_logger
from liqfade.strategy.types import AssetMeta

log = get_logger(__name__)

__all__ = ["DAY_MS", "Day", "LakeSource", "feature_hash", "midnight_ms"]

CACHE_DIR = Path("cache") / "features"
CACHE_FILE = "grid.parquet"
COINS_KEY = b"liqfade_coins"

# The config a feature column actually depends on. Everything else may change without
# invalidating a cached grid, which is the point: a sweep over stops, targets, sizing
# fractions and risk limits reuses one year of features.
#
# Add to this list when the grid learns to read a new key. A key that belongs here and is
# missing makes the cache return a grid built under different settings, which is silent
# and wrong, so `tests/test_backtest_streaming.py` pins both directions.
HASHED_KEYS: tuple[str, ...] = (
    "features",  # every window, the z-score parameters, depth_bps
    "cascade",  # the trigger, and the thresholds `candidate_expr` reads
    "venues",  # liq_weight per venue, and which are enabled
    "backtest.feature_grid_ms",  # the grid step every window is counted in
    "sizing.vol_window_s",  # names and fills traded_usd_{w}s
    "exhaustion.mark_mid_pct_threshold",  # the percentile mark_mid_threshold is taken at
    "exhaustion.max_age_s",  # how far past a candidate active_mask reaches
    "strategy.rules.zone_proximity",  # whether support_px and resistance_px are joined
)


@dataclass(frozen=True, slots=True)
class Day:
    """One UTC day of a run, clipped to the run's window.

    `date` is the partition, `start_ms` and `end_ms` the half-open span of it this run
    covers. They differ from midnight to midnight only on a run's first and last day, and
    the CLI always aligns to whole days.
    """

    date: str
    start_ms: int
    end_ms: int

    @property
    def midnight_ms(self) -> int:
        return midnight_ms(self.date)

    @property
    def previous(self) -> str:
        return (date.fromisoformat(self.date) - timedelta(days=1)).isoformat()


def feature_hash(settings: Settings) -> str:
    """A short digest of every config key a feature column depends on."""
    payload = {key: _dotted(settings.model_dump(mode="json"), key) for key in HASHED_KEYS}
    encoded = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


class LakeSource:
    """The Parquet lake, read one day of one coin at a time.

    Args:
        data_dir: lake root, holding `parquet/` and (unless `cache` is false) `cache/`.
        settings: resolved config. Decides the feature hash and the window bounds.
        coins: the universe to replay.
        start_ms: run start, inclusive.
        end_ms: run end, exclusive.
        starting_equity: what the portfolio opens with.
        seed: passed through for determinism; nothing draws from it yet.
        cache: read and write the per-day grid cache. False rebuilds every day.
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        settings: Settings,
        coins: Sequence[str],
        start_ms: int,
        end_ms: int,
        starting_equity: float = 10_000.0,
        seed: int = 0,
        cache: bool = True,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.settings = settings
        self.coins = tuple(sorted(set(coins)))
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.starting_equity = starting_equity
        self.seed = seed
        # Nothing fills this yet: a historical window has no venue metadata, and
        # `_resolve_meta` invents the most constraining defaults. It is here because the
        # engine reads it off the source and the lake will have real values in Phase 5.
        self.meta: Mapping[str, AssetMeta] = {}
        self.levels: pl.DataFrame | None = None
        self._cache = cache
        self.grids_built = 0
        self.grids_cached = 0

    # --- days -----------------------------------------------------------------

    def days(self) -> list[Day]:
        """Every UTC day the run touches, clipped to its window, in order."""
        out = []
        for partition in utc_days(self.start_ms, self.end_ms):
            midnight = midnight_ms(partition)
            out.append(
                Day(
                    date=partition,
                    start_ms=max(midnight, self.start_ms),
                    end_ms=min(midnight + DAY_MS, self.end_ms),
                )
            )
        return out

    def grids(
        self, day: Day, settings: Settings, *, first: bool
    ) -> Iterator[tuple[str, pl.DataFrame]]:
        """This day's feature grid, one coin at a time.

        Per coin rather than per day because a day of 42 coins on a 1s grid is over three
        million rows and a gigabyte of float columns, and every feature transform is
        already `.over("coin")`, so splitting the work changes no number. The engine
        reduces each coin's frame to the rows a cascade could involve and lets the rest go,
        which is why a 42 coin year fits in memory at all.

        `first` says this is the run's first day, which is built without the previous day
        in front of it and is neither read from nor written to the cache.

        `settings` comes from the engine rather than from this source's constructor, and
        everything below reads it rather than `self.settings`. One source is replayed twice
        by `--lagged-ms`, and if the second settings ever differ in a key the grid reads,
        the second run has to build its own grid under its own hash rather than silently
        inherit the first one's.
        """
        cache_root = self.data_dir / CACHE_DIR / feature_hash(settings)
        cached = None if first or not self._cache else self._cached(cache_root, day)
        writer = (
            _CacheWriter(_cache_path(cache_root, day), self.coins)
            if self._cache and not first and cached is None
            else None
        )
        try:
            for coin in self.coins:
                if cached is not None:
                    grid = cached.filter(pl.col("coin") == coin).collect()
                    self.grids_cached += 1
                else:
                    grid = self._build(day, coin, settings, first=first)
                    self.grids_built += 1
                    if writer is not None:
                        writer.add(grid)
                if not grid.is_empty():
                    yield coin, grid
        finally:
            if writer is not None:
                writer.close()

    def events(self, day: Day, spans: Mapping[str, Sequence[tuple[int, int]]]) -> Iterator[Event]:
        """Every event inside `spans` on this day, merged into one time-ordered stream."""
        if not spans:
            return iter(())
        # Trades are Hyperliquid only, matching what the grid was built on; liquidations
        # come from every venue and `cascade.trigger` picks among them inside the grid.
        read = {
            table: load_day(self.data_dir, table, day.date, spans=spans, venue=venue)
            for table, venue in (
                ("trades", "hyperliquid"),
                ("liquidations", None),
                ("bbo", None),
                ("l2", None),
                ("asset_ctx", None),
            )
        }
        return iter_events(
            trades=read["trades"],
            liquidations=read["liquidations"],
            bbo=_or_none(read["bbo"]),
            l2=_or_none(read["l2"]),
            asset_ctx=_or_none(read["asset_ctx"]),
        )

    def funding(self, day: Day) -> pl.DataFrame:
        """This day's `asset_ctx`, for the funding book and nothing else.

        Read for the whole day rather than inside the windows, because funding accrues by
        the hour over the whole run and a position that opens at 12:30 pays the rate
        sampled at 12:59 whether or not 12:59 falls inside a window. It is a small table:
        one sample a minute per coin.
        """
        rows = load_day(self.data_dir, "asset_ctx", day.date, coins=self.coins)
        if rows.is_empty():
            return rows
        return rows.filter(_known().is_between(self.start_ms, self.end_ms, closed="left"))

    # --- internals ------------------------------------------------------------

    def _build(self, day: Day, coin: str, settings: Settings, *, first: bool) -> pl.DataFrame:
        """One coin's grid for one day, built with the previous day in front of it.

        The carry is what makes a day's grid a function of the date rather than of where
        the run happened to start: `rv_300s`, the CVD slope and `traded_usd_300s` all reach
        back minutes, and `robust_z` reaches back a full 24 hours. Only the scored day is
        returned. On the run's first day there is no previous day inside the window, and
        the grid is cold-started exactly as the non-streaming engine was.
        """
        days = [day.date] if first else [day.previous, day.date]
        grid_start = midnight_ms(days[0])
        trades = self._read(days, "trades", coin, venue="hyperliquid")
        if trades.is_empty():
            return pl.DataFrame()
        grid = build_feature_grid(
            settings=settings,
            trades=trades,
            liquidations=self._read(days, "liquidations", coin),
            start_ms=grid_start,
            end_ms=day.midnight_ms + DAY_MS,
            # The book tables carry only their last snapshot from the previous day. Both
            # reach the grid through a backward as-of join and nothing else, so the latest
            # snapshot before midnight is the only row of that day any grid row can ever
            # select; the rest cannot change an answer. It is worth the special case
            # because `l2` is the largest table in the lake, 3.1 million rows per coin per
            # day, and reading two days of it per day of the run was most of the memory.
            bbo=_or_none(self._read(days, "bbo", coin, tail_carry=True)),
            l2=_or_none(self._read(days, "l2", coin, tail_carry=True)),
            asset_ctx=_or_none(self._read(days, "asset_ctx", coin)),
            levels=self.levels,
        )
        if grid.is_empty():
            return grid
        # The strategy's whole read surface and nothing else, so a cached day is as small
        # as it can be while still being enough to replay from.
        wanted = ("coin", "ts_ms", *row_columns(settings))
        keep = [name for name in wanted if name in grid.columns]
        return grid.filter(pl.col("ts_ms") >= day.midnight_ms).select(keep)

    def _read(
        self,
        days: Sequence[str],
        table: str,
        coin: str,
        *,
        tail_carry: bool = False,
        venue: str | None = None,
    ) -> pl.DataFrame:
        """One coin's rows across `days`, the last of which is the day being built.

        `tail_carry` keeps only the last snapshot of every day but the last, for the tables
        that reach the grid through a backward as-of join alone.
        """
        parts = []
        for index, partition in enumerate(days):
            frame = load_day(
                self.data_dir,
                table,
                partition,
                coins=[coin],
                venue=venue,
                tail_only=tail_carry and index < len(days) - 1,
            )
            if not frame.is_empty():
                parts.append(frame)
        return pl.concat(parts) if parts else pl.DataFrame()

    def _cached(self, cache_root: Path, day: Day) -> pl.LazyFrame | None:
        """The day's cached grid, or None when it is absent or covers too few coins."""
        path = _cache_path(cache_root, day)
        if not path.is_file():
            return None
        try:
            covered = _cached_coins(path)
        except Exception as exc:  # a truncated or unreadable cache is a rebuild, not a crash
            log.warning("feature_cache_unreadable", path=str(path), error=str(exc))
            return None
        # The coins the cache was *asked* about, not the ones that ended up with rows: a
        # coin that never printed that day has no grid, and its absence is the right
        # answer rather than a miss. A cache written by a narrower run has nothing to say
        # about the coins it never covered, and a partial answer would silently drop them.
        if covered is None or not set(self.coins) <= covered:
            return None
        return pl.scan_parquet(path)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return (
            f"LakeSource({self.data_dir}, {len(self.coins)} coins, {self.start_ms}..{self.end_ms})"
        )


class _CacheWriter:
    """Appends one coin's grid at a time to a day's cache file.

    A `ParquetWriter` rather than a `write_parquet` of the concatenated day, because the
    whole point of the per-coin loop is never to hold the day. Written beside the target
    and renamed, so a run that dies mid-day leaves no half a day that a later run would
    trust.
    """

    def __init__(self, path: Path, coins: Sequence[str]) -> None:
        self.path = path
        self.tmp = path.with_suffix(".parquet.tmp")
        self.coins = list(coins)
        self._writer: pq.ParquetWriter | None = None

    def add(self, grid: pl.DataFrame) -> None:
        if grid.is_empty():
            return
        table = grid.to_arrow()
        if self._writer is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # The coin list rides in the schema metadata because it is the one thing the
            # rows cannot say: a coin that had no prints leaves no row, and a reader has to
            # tell "covered and empty" from "never asked about".
            schema = table.schema.with_metadata({COINS_KEY: json.dumps(self.coins).encode()})
            self._writer = pq.ParquetWriter(self.tmp, schema, compression="zstd")
            table = table.cast(schema)
        self._writer.write_table(table)

    def close(self) -> None:
        if self._writer is None:
            return
        self._writer.close()
        self._writer = None
        os.replace(self.tmp, self.path)


def _cached_coins(path: Path) -> set[str] | None:
    """The coins a cache file was built for, from its schema metadata."""
    metadata = pq.read_schema(path).metadata or {}
    raw = metadata.get(COINS_KEY)
    return set(json.loads(raw)) if raw else None


DAY_MS = 86_400_000


def _cache_path(cache_root: Path, day: Day) -> Path:
    return cache_root / f"date={day.date}" / CACHE_FILE


def midnight_ms(day: str) -> int:
    """Midnight UTC of a `YYYY-MM-DD` partition, in integer milliseconds."""
    parsed = date.fromisoformat(day)
    return int(datetime(parsed.year, parsed.month, parsed.day, tzinfo=UTC).timestamp() * 1000)


def _known() -> pl.Expr:
    return pl.when(pl.col("rx_ms") > 0).then(pl.col("rx_ms")).otherwise(pl.col("ts_ms"))


def _or_none(frame: pl.DataFrame) -> pl.DataFrame | None:
    return None if frame.is_empty() else frame


def _dotted(tree: Mapping[str, object], key: str) -> object:
    node: object = tree
    for part in key.split("."):
        node = node[part]  # type: ignore[index]
    return node
