"""Shared plumbing for the Phase 3 research scripts. Imported by them, never by the package.

Holds the four things every question needs: the lake reader, the cost model, the cluster
bootstrap and the report writer. Two of those decide every DECISION in the phase and have
their own tests in `tests/test_research_lib.py`:

- **The cost model.** Three columns, always, in this order: gross, net-9bps, net-stress.
  Every DECISION reads net-stress and nothing else. A table that shows only gross is a bug.
- **The cluster bootstrap.** A market-wide sweep liquidates hundreds of coins inside one
  minute; they are one observation, not hundreds. Resampling episodes would report an
  interval about ten times too narrow and turn noise into a yes. The per-episode bootstrap
  is banned from every report in this phase.

Funding is ignored: at a 1 to 60 minute hold the accrual is under a tenth of a basis point
and far inside the spread estimate. Every report says so.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import numpy as np
import polars as pl

from liqfade.features.context import with_ctx
from liqfade.features.episodes import group_episodes, qualify_episodes
from liqfade.features.flow import cvd, realized_vol
from liqfade.features.grid import effective_ts
from liqfade.features.intensity import liq_intensity
from liqfade.features.zscore import robust_z

# 0.045% taker per side, base tier 2026-08, both ways. config/default.yaml backtest.*_fee_frac.
FEES_BPS = 9.0
# Seconds between the extreme and the entry the stress model assumes, matching the 1s
# signal-to-fill latency CLAUDE.md requires in every backtest number.
ENTRY_DELAY_S = 1.0


class Estimate(NamedTuple):
    """A statistic with its cluster-bootstrap interval and both sample sizes."""

    mean: float
    lo: float
    hi: float
    n: int
    n_clusters: int

    def render(self) -> str:
        return (
            f"{self.mean:+.1f} [{self.lo:+.1f}, {self.hi:+.1f}] "
            f"(n={self.n}, clusters={self.n_clusters})"
        )


def parse_args(description: str) -> argparse.Namespace:
    """The argument set every question script shares."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--start", required=True, help="first UTC date, YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="last UTC date inclusive, YYYY-MM-DD")
    parser.add_argument("--data-dir", default="data", help="lake root holding parquet/")
    parser.add_argument(
        "--out-dir", default="reports/research", help="where the report and its figures land"
    )
    return parser.parse_args()


def dates(start: str, end: str) -> list[str]:
    """Every UTC date from start to end inclusive."""
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if last < first:
        raise SystemExit(f"--end {end} is before --start {start}")
    return [
        (first + timedelta(days=offset)).isoformat() for offset in range((last - first).days + 1)
    ]


def ms(day: str) -> int:
    """Midnight UTC of a YYYY-MM-DD date, in milliseconds."""
    return int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp() * 1000)


# Columns each table is read with. Every read prunes: `trades` carries nine columns and a
# single day of it is fifty million rows, so reading `venue`, `source` and `tid` costs a
# gigabyte of resident memory to throw them away. See docs/DECISIONS.md, 2026-09-05.
COLUMNS = {
    "trades": ("coin", "ts_ms", "rx_ms", "px", "sz", "side"),
    "liquidations": ("venue", "coin", "ts_ms", "rx_ms", "side", "px", "sz", "notional_usd"),
    "asset_ctx": (
        "coin",
        "ts_ms",
        "rx_ms",
        "mark_px",
        "oracle_px",
        "mid_px",
        "funding",
        "open_interest",
        "day_ntl_vlm",
        "impact_bid_px",
        "impact_ask_px",
    ),
    "candles_1m": ("coin", "ts_ms", "o", "h", "l", "c", "v", "n"),
}
# Tables small enough to hold a year of in memory. `trades` is not one of them: the lake
# holds 1.79 billion rows of it and a single busy day is fifty million. Anything wanting
# trades goes through `windowed_trades`, one day at a time.
DENSE_TABLES = frozenset({"liquidations", "asset_ctx", "candles_1m"})


def day_files(data_dir: str | Path, table: str, day: str, venue: str | None = None) -> list[Path]:
    """The Parquet files for one table on one UTC day.

    Skips names beginning with a dot. macOS writes an AppleDouble `._all_day.parquet`
    beside every file on an exFAT drive, `Path.glob` returns it, and it is four kilobytes
    of resource fork that no Parquet reader can open. That is a crash on the owner's disk
    and on no developer's.
    """
    directory = Path(data_dir) / "parquet" / table / f"date={day}"
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.glob("*.parquet")
        if not path.name.startswith(".")
        and (venue is None or path.name.startswith((f"{venue}_", "all_")))
    )


def scan_day(
    data_dir: str | Path,
    table: str,
    day: str,
    *,
    venue: str | None = None,
    columns: Sequence[str] | None = None,
) -> pl.LazyFrame | None:
    """A lazy frame over one day of one table, projected to the columns that matter.

    Lazy so that the filter a caller adds is pushed into the Parquet reader and whole row
    groups are skipped rather than read and discarded. Returns None when the day is absent,
    which is not an error: a lake with a hole in it answers "inconclusive", not a traceback.
    """
    files = day_files(data_dir, table, day, venue)
    if not files:
        return None
    wanted = tuple(columns) if columns is not None else COLUMNS.get(table)
    frame = pl.scan_parquet(files)
    if wanted:
        frame = frame.select(list(wanted))
    if venue is not None and "venue" in (wanted or ()):
        frame = frame.filter(pl.col("venue") == venue)
    return frame


def load_table(
    data_dir: str | Path,
    table: str,
    start: str,
    end: str,
    *,
    venue: str | None = None,
    columns: Sequence[str] | None = None,
    coins: Sequence[str] | None = None,
) -> pl.DataFrame:
    """Read a whole table over a date range, one day at a time, pruned to the columns used.

    Only for the tables in `DENSE_TABLES`. A year of liquidations is 5.8 million rows and a
    year of `asset_ctx` is 117 million, of which the eleven columns here are a fraction; a
    year of trades is 1.79 billion and asking for it is what killed the first run. Trades go
    through `windowed_trades` instead, and asking for them here raises rather than swaps the
    machine to death.

    Missing days are skipped rather than raised on. Returns an empty frame when nothing in
    the range exists, which callers check with `.is_empty()`.
    """
    if table not in DENSE_TABLES:
        raise ValueError(
            f"{table!r} is too large to load over a date range; use windowed_trades or "
            f"scan_day one day at a time"
        )
    parts = []
    for day in dates(start, end):
        frame = scan_day(data_dir, table, day, venue=venue, columns=columns)
        if frame is None:
            continue
        if coins is not None:
            frame = frame.filter(pl.col("coin").is_in(list(coins)))
        collected = frame.collect()
        if not collected.is_empty():
            parts.append(collected)
    return pl.concat(parts) if parts else pl.DataFrame()


def merge_windows(windows: list[tuple[int, int]], *, join_ms: int = 0) -> list[tuple[int, int]]:
    """Collapse overlapping `[from, to]` spans so a filter has as few branches as possible.

    A busy day carries dozens of episodes whose padded windows overlap; expressing them as
    one predicate per episode makes a filter polars evaluates dozens of times over fifty
    million rows.
    """
    if not windows:
        return []
    merged = [list(span) for span in sorted(windows)]
    out = [merged[0]]
    for start, end in merged[1:]:
        if start <= out[-1][1] + join_ms:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return [(start, end) for start, end in out]


def windowed_trades(
    data_dir: str | Path, day: str, windows: dict[str, list[tuple[int, int]]]
) -> pl.DataFrame:
    """One day's trades, only for the named coins and only inside the named time spans.

    `windows` maps a coin to its `[from_ms, to_ms]` spans. Both filters are pushed into the
    scan, so a day whose episodes cover a tenth of the tape reads roughly a tenth of the
    rows rather than all fifty million of them and throwing the rest away.

    The whole memory budget of this phase rests on this function: it is the only place the
    lake's largest table is read, and it never reads more than one day of it.
    """
    if not windows:
        return pl.DataFrame()
    frame = scan_day(data_dir, "trades", day, venue="hyperliquid")
    if frame is None:
        return pl.DataFrame()
    branches = [
        (pl.col("coin") == coin)
        & pl.any_horizontal(
            [
                (pl.col("ts_ms") >= start) & (pl.col("ts_ms") <= end)
                for start, end in merge_windows(spans)
            ]
        )
        for coin, spans in sorted(windows.items())
    ]
    return frame.filter(pl.any_horizontal(branches)).collect()


def with_clusters(episodes: pl.DataFrame, *, window_s: int) -> pl.DataFrame:
    """Label each episode with its cluster: one side, one time window, every coin.

    Coin is deliberately not part of the key. Fifty coins liquidated by the same BTC sweep
    are one draw from the world, and treating them as fifty is the single easiest way to
    manufacture significance in this phase.
    """
    window_ms = window_s * 1000
    return episodes.with_columns(
        (pl.col("side") + "@" + (pl.col("start_ts") // window_ms).cast(pl.String)).alias("cluster")
    )


def cluster_bootstrap(
    values: np.ndarray,
    clusters: np.ndarray,
    *,
    confidence: float = 0.95,
    draws: int = 2000,
    seed: int = 0,
) -> Estimate:
    """Mean of `values` with a percentile interval from resampling whole clusters.

    Args:
        values: one number per episode, usually a net-stress PnL in bps.
        clusters: the cluster label of each episode. Any hashable dtype.
        confidence: 0.95 for a headline, 0.99 for the one permitted secondary horizon.
        draws: bootstrap resamples.
        seed: fixed so a report is reproducible from its own text.

    Returns:
        The point estimate, the interval and both sample sizes. `nan` bounds when there is
        nothing to resample.
    """
    values = np.asarray(values, dtype=float)
    labels, index = np.unique(np.asarray(clusters), return_inverse=True)
    if values.size == 0 or labels.size == 0:
        return Estimate(float("nan"), float("nan"), float("nan"), int(values.size), 0)
    members = [np.flatnonzero(index == position) for position in range(labels.size)]
    rng = np.random.default_rng(seed)
    picks = rng.integers(0, labels.size, size=(draws, labels.size))
    means = np.empty(draws)
    for draw in range(draws):
        taken = np.concatenate([members[position] for position in picks[draw]])
        means[draw] = values[taken].mean()
    tail = (1.0 - confidence) / 2.0
    lo, hi = np.quantile(means, [tail, 1.0 - tail])
    return Estimate(float(values.mean()), float(lo), float(hi), int(values.size), int(labels.size))


def with_cost_columns(trades: pl.DataFrame) -> pl.DataFrame:
    """Add `gross_bps`, `net_9bps` and `net_stress_bps` to a frame of fade trades.

    Args:
        trades: needs `side` (the liquidated side, which the trade is against),
            `entry_px` (the cascade extreme), `entry_px_delayed` (the first print after
            `extreme_ts + 1s`), `exit_px` and `spread_bps` (one effective spread, not a
            half).

    Returns:
        The frame plus the three columns, in basis points of the entry price. Gross and
        net-9bps price the ideal entry at the extreme; net-stress prices the delayed entry
        and subtracts the fee plus one full effective spread **per side**, which is the
        conservative reading the preregistration fixed and roughly twice what crossing a
        quoted touch would cost.
    """
    # Fading a long liquidation means buying into forced selling, so a rising exit is a
    # win; fading a short liquidation is the mirror. Getting this sign wrong would flatter
    # every table in the phase, so it is one expression in one place.
    direction = pl.when(pl.col("side") == "long").then(1.0).otherwise(-1.0)
    gross = 1e4 * direction * (pl.col("exit_px") - pl.col("entry_px")) / pl.col("entry_px")
    delayed = (
        1e4
        * direction
        * (pl.col("exit_px") - pl.col("entry_px_delayed"))
        / pl.col("entry_px_delayed")
    )
    return trades.with_columns(
        gross.alias("gross_bps"),
        (gross - FEES_BPS).alias("net_9bps"),
        (delayed - FEES_BPS - 2 * pl.col("spread_bps")).alias("net_stress_bps"),
    )


def effective_spread_bps(frame: pl.DataFrame) -> pl.DataFrame:
    """Take the wider of the archive and trade-based spread estimates, per row.

    The archive samples Hyperliquid's own impact prices once a minute, which reflects depth
    rather than the touch; the trade-based estimator reads the tape of the cascade minute
    itself. A cascade can be far worse than the minute that preceded it, so the wider of
    the two is the honest one and a null on either side never wins.
    """
    return frame.with_columns(
        pl.max_horizontal("archive_spread_bps", "trade_spread_bps").alias("spread_bps")
    )


def drop_top_clusters(frame: pl.DataFrame, column: str, *, count: int) -> pl.DataFrame:
    """Remove the `count` clusters with the highest mean `column`, whole clusters at a time.

    The mandatory rerun behind every headline number: an edge that dies when the five best
    sweeps leave the sample is those five sweeps, not an edge.
    """
    ranked = (
        frame.group_by("cluster")
        .agg(pl.col(column).mean().alias("_mean"))
        .sort("_mean", descending=True)
        .head(count)["cluster"]
        .to_list()
    )
    return frame.filter(~pl.col("cluster").is_in(ranked))


def write_report(path: Path, title: str, blocks: list[str], *, costs: bool = True) -> Path:
    """Write a question's markdown report and return its path.

    `costs=False` for a question that measures timing rather than money and so has no cost
    columns to carry. Everything else states the three-column rule in its header, because a
    reader should not have to trust that a table they are looking at is net of anything.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    header = [f"# {title}", "", f"Generated {stamp} from the Parquet lake.", ""]
    if costs:
        header += [
            "Every table below carries three columns: gross, net-9bps (0.09% round trip) and",
            "net-stress (that fee, entry one second after the extreme, and one effective",
            "spread per side). The DECISION reads net-stress and nothing else. Funding is",
            "ignored: at a 1 to 60 minute hold it is under a tenth of a basis point.",
            "",
        ]
    path.write_text("\n".join(header + blocks) + "\n")
    return path


def table(headers: list[str], rows: list[list[str]]) -> str:
    """A markdown table, or a line saying the sample was empty."""
    if not rows:
        return "_No rows._"
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def decision(verdict: str, reason: str) -> str:
    """The one line every report ends with. `verdict` is yes, no or inconclusive."""
    if verdict not in {"yes", "no", "inconclusive"}:
        raise ValueError(f"verdict must be yes, no or inconclusive, not {verdict!r}")
    return f"\n## DECISION: {verdict}\n\n{reason}\n"


# ---------------------------------------------------------------------------------------
# The shared pipeline: lake to a frame of episodes with their outcomes attached.
#
# Two stages, because the lake's tables differ in size by three orders of magnitude.
#
# Stage A reads liquidations only, 5.8 million rows for the year, and needs no trades at
# all: gap joining is pure liquidation data, and so is the trailing p95, and so is the
# z-score, whose distribution is a one-minute grid of liquidation notional. It ends with
# the episodes that could still qualify once a price is attached.
#
# Stage B reads trades, and reads them only in a window around each surviving episode, one
# UTC day at a time, aggregating each day to a 1s grid and releasing the raw rows before
# the next day is opened. The grid for a year of episodes is a few million rows; the trades
# behind it are 1.79 billion and would need 23 GB of the owner's 8 GB machine.
#
# The science is unchanged by any of this. `group_episodes` is still called on the full set
# of liquidations, so the unfiltered episode count is the true one; episodes that Stage A
# ruled out simply have no bars under them, which leaves their anchor and extreme null and
# fails them on the move floor, exactly as a dense run would.
# ---------------------------------------------------------------------------------------

GRID_STEP_MS = 1000
HORIZONS_MIN = (1, 5, 15, 30, 60)
CLUSTER_WINDOW_S = 300
# The window the cascade rule and the preregistration both name: liq_z_60s.
Z_WINDOW_S = 60
# rv_5m: the vol the stop distance is measured in (config intent.k_stop).
RV_WINDOW_S = 300
# Seconds of grid in front of an episode. It has to cover the realized-vol window, because
# `realized_vol` counts rows rather than time and a shorter lead-in would let the window
# reach back into whatever grid rows precede it. 360 > 300 with a minute to spare.
LEAD_IN_S = 360
# Seconds of grid behind an episode: the +60 minute horizon plus the capacity window plus
# the join gap the extreme search runs to, and slack.
TRAIL_S = 66 * 60


def month_chunks(start: str, end: str) -> list[tuple[str, str]]:
    """Split a date range into calendar months, clipped to the range."""
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    out = []
    cursor = first
    while cursor <= last:
        if cursor.month == 12:
            following = date(cursor.year + 1, 1, 1)
        else:
            following = date(cursor.year, cursor.month + 1, 1)
        out.append((cursor.isoformat(), min(following - timedelta(days=1), last).isoformat()))
        cursor = following
    return out


def padded(start: str, end: str) -> tuple[str, str]:
    """The date range a chunk must actually load: warm-up in front, horizon behind."""
    before = date.fromisoformat(start) - timedelta(days=2)
    after = date.fromisoformat(end) + timedelta(days=1)
    return before.isoformat(), after.isoformat()


def coin_tiers(data_dir: str | Path, start: str, end: str, core: tuple[str, ...]) -> dict[str, str]:
    """core, major or minor per coin, by median daily notional volume over the window.

    Reads `asset_ctx.day_ntl_vlm`, Hyperliquid's own rolling 24h figure, one day at a time
    and reduced to one row per coin per day before the next day is opened: the raw column is
    117 million rows for the year and the answer is 229 numbers. Coins with no `asset_ctx`
    rows are minor, since an unranked coin is not a top-20 coin.
    """
    daily = []
    for day in dates(start, end):
        frame = scan_day(data_dir, "asset_ctx", day, columns=("coin", "day_ntl_vlm"))
        if frame is None:
            continue
        daily.append(frame.group_by("coin").agg(pl.col("day_ntl_vlm").median()).collect())
    if not daily:
        return {}
    ranked = (
        pl.concat(daily)
        .group_by("coin")
        .agg(pl.col("day_ntl_vlm").median().alias("volume"))
        .sort("volume", descending=True)
    )
    majors = [coin for coin in ranked["coin"].to_list() if coin not in core][:20]
    return {
        coin: "core" if coin in core else ("major" if coin in majors else "minor")
        for coin in ranked["coin"].to_list()
    }


def _liquidation_z(
    liquidations: pl.DataFrame, episodes: pl.DataFrame, settings: object
) -> pl.DataFrame:
    """The highest `liq_z_60s` reached inside each episode, from liquidations alone.

    No trades are involved. The z-score's distribution is the trailing 24h of one-minute
    liquidation notional and the value being scored is the same quantity at the tick, so
    both come from the liquidation table.

    Two grids per coin, because `liq_intensity` measures its window in rows and a
    mixed-resolution frame would make one window mean two things:

    - a one-minute grid over the coin's span, which supplies the trailing distribution;
    - a 1s grid over the episode windows, each carrying 60 seconds of lead-in so the 60s
      rolling window at the episode's first tick is full and reaches back only into that
      episode's own lead-in. Episodes on one coin are more than the join gap apart, so
      those windows never overlap.

    Membership is tagged rather than joined. The previous version joined episodes to scored
    ticks on coin and side and filtered by time afterwards, which is a cross product: a
    month is 22,000 episodes against 15 million scored rows, and it is what killed the
    first full run.
    """
    features = settings.features  # type: ignore[attr-defined]
    blank = episodes.with_columns(pl.lit(None, dtype=pl.Float64).alias("max_liq_z"))
    if liquidations.is_empty() or episodes.is_empty():
        return blank
    minute_ms = Z_WINDOW_S * 1000
    spans = episodes.select("coin", "side", "start_ts", "end_ts")
    # One row per episode per second it covers. This is what says which ticks belong to
    # which episode, and it is the only thing that ever carries the episode key.
    membership = spans.with_columns(
        pl.int_ranges(
            (pl.col("start_ts") // GRID_STEP_MS) * GRID_STEP_MS,
            pl.col("end_ts") + GRID_STEP_MS,
            GRID_STEP_MS,
            dtype=pl.Int64,
        ).alias("ts_ms")
    ).explode("ts_ms")
    peaks = []
    for coin in sorted(episodes["coin"].unique().to_list()):
        coin_liqs = liquidations.filter(pl.col("coin") == coin)
        if coin_liqs.is_empty():
            continue
        first = int(coin_liqs["ts_ms"].min()) // minute_ms * minute_ms
        last = int(coin_liqs["ts_ms"].max()) // minute_ms * minute_ms + minute_ms
        minutes = pl.DataFrame(
            {"coin": coin, "ts_ms": range(first, last + minute_ms, minute_ms)},
            schema={"coin": pl.String, "ts_ms": pl.Int64},
        )
        distribution = liq_intensity(
            coin_liqs,
            minutes,
            windows_s=[Z_WINDOW_S],
            step_ms=minute_ms,
            weights={"hyperliquid": 1.0},
        )
        windows = spans.filter(pl.col("coin") == coin).select(
            "coin",
            (pl.col("start_ts") - minute_ms).alias("from"),
            pl.col("end_ts").alias("to"),
        )
        ticks = (
            windows.with_columns(
                pl.int_ranges(
                    (pl.col("from") // GRID_STEP_MS) * GRID_STEP_MS,
                    pl.col("to") + GRID_STEP_MS,
                    GRID_STEP_MS,
                    dtype=pl.Int64,
                ).alias("ts_ms")
            )
            .explode("ts_ms")
            .select("coin", "ts_ms")
            .unique()
            .sort("ts_ms")
        )
        inside = liq_intensity(
            coin_liqs,
            ticks,
            windows_s=[Z_WINDOW_S],
            step_ms=GRID_STEP_MS,
            weights={"hyperliquid": 1.0},
        )
        # A tick landing exactly on a minute carries the same trailing sum as the minute
        # grid row there, so dropping the duplicate is lossless and keeps one sample per
        # minute in the distribution the rolling median is built from.
        both = pl.concat([distribution, inside]).unique(subset=["coin", "ts_ms"], keep="first")
        columns = [f"liq_{side}_usd_{Z_WINDOW_S}s" for side in ("long", "short")]
        z = robust_z(
            both.sort("ts_ms"),
            columns,
            lookback_h=features.z_lookback_h,
            sample_s=Z_WINDOW_S,
            robust=features.z_robust,
        ).select("coin", "ts_ms", *[f"{column}_z" for column in columns])
        # Reduced to one row per episode before the next coin is started. Keeping each
        # coin's whole minute grid instead is what took a month of this to 2.2 GB: the
        # grids are the year, and the answer is one number per episode.
        per_side = pl.concat(
            [
                z.select(
                    "coin",
                    "ts_ms",
                    pl.lit(side).alias("side"),
                    pl.col(f"liq_{side}_usd_{Z_WINDOW_S}s_z").alias("z"),
                )
                for side in ("long", "short")
            ]
        )
        peaks.append(
            membership.filter(pl.col("coin") == coin)
            .join(per_side, on=["coin", "side", "ts_ms"], how="left")
            .group_by("coin", "side", "start_ts")
            .agg(pl.col("z").max().alias("max_liq_z"))
        )
        del distribution, inside, both, z, per_side
    if not peaks:
        return blank
    peak = pl.concat(peaks)
    return episodes.join(peak, on=["coin", "side", "start_ts"], how="left")


def _grid_windows(episodes: pl.DataFrame) -> dict[str, dict[str, list[tuple[int, int]]]]:
    """Per UTC day, per coin, the `[from, to]` spans of tape an episode needs.

    The lead-in covers the realized-vol window and the VWAP anchor; the trail covers the
    extreme search, the capacity window and the +60 minute horizon. A window that crosses
    midnight appears under both days, so each day's read is self-contained.
    """
    windows: dict[str, dict[str, list[tuple[int, int]]]] = {}
    for row in episodes.iter_rows(named=True):
        first = int(row["start_ts"]) - LEAD_IN_S * 1000
        last = int(row["end_ts"]) + TRAIL_S * 1000
        for day_ms in range(first // 86_400_000 * 86_400_000, last + 86_400_000, 86_400_000):
            day = datetime.fromtimestamp(day_ms / 1000, UTC).strftime("%Y-%m-%d")
            windows.setdefault(day, {}).setdefault(row["coin"], []).append((first, last))
    return windows


def _windowed_second_bars(
    data_dir: str | Path, day: str, windows: dict[str, list[tuple[int, int]]]
) -> pl.DataFrame:
    """Per-second aggregates for one day, filtered to the windows inside the Parquet scan.

    The filter and the aggregation are one lazy query, so a day of fifty million prints is
    never a frame: rows outside the windows are skipped by the reader, and the rows inside
    are reduced to one per coin per second as they stream. This is the single hottest place
    in the phase for memory, and everything else in Stage B is small because of it.
    """
    scan = scan_day(data_dir, "trades", day, venue="hyperliquid")
    if scan is None or not windows:
        return pl.DataFrame()
    branches = [
        (pl.col("coin") == coin)
        & pl.any_horizontal(
            [
                (pl.col("ts_ms") >= start) & (pl.col("ts_ms") <= end)
                for start, end in merge_windows(spans)
            ]
        )
        for coin, spans in sorted(windows.items())
    ]
    known = pl.when(pl.col("rx_ms") > 0).then(pl.col("rx_ms")).otherwise(pl.col("ts_ms"))
    return (
        scan.filter(pl.any_horizontal(branches))
        .with_columns(known.alias("known"))
        .with_columns(
            ((pl.col("known") // GRID_STEP_MS) * GRID_STEP_MS + GRID_STEP_MS).alias("ts_ms")
        )
        .group_by("coin", "ts_ms")
        .agg(
            pl.col("px").sort_by("known").last().alias("last_px"),
            (pl.col("px") * pl.col("sz")).sum().alias("notional_usd"),
            pl.col("sz").sum().alias("volume"),
            (
                pl.when(pl.col("side") == "buy")
                .then(pl.col("px") * pl.col("sz"))
                .otherwise(-pl.col("px") * pl.col("sz"))
            )
            .sum()
            .alias("signed_notional"),
            pl.len().alias("n_trades"),
        )
        .collect(engine="streaming")
    )


def _dense_windows(windows: dict[str, list[tuple[int, int]]]) -> pl.DataFrame:
    """Every 1s grid timestamp inside the windows, as a (coin, ts_ms) frame."""
    rows = [
        {"coin": coin, "from": start, "to": end}
        for coin, spans in windows.items()
        for start, end in merge_windows(spans)
    ]
    if not rows:
        return pl.DataFrame(schema={"coin": pl.String, "ts_ms": pl.Int64})
    frame = pl.DataFrame(rows, schema={"coin": pl.String, "from": pl.Int64, "to": pl.Int64})
    return (
        frame.with_columns(
            pl.int_ranges(
                (pl.col("from") // GRID_STEP_MS) * GRID_STEP_MS,
                pl.col("to") + GRID_STEP_MS,
                GRID_STEP_MS,
                dtype=pl.Int64,
            ).alias("ts_ms")
        )
        .explode("ts_ms")
        .select("coin", "ts_ms")
        .unique()
    )


def episode_grid(data_dir: str | Path, episodes: pl.DataFrame) -> pl.DataFrame:
    """A 1s grid covering only the tape each episode needs, built one UTC day at a time.

    This is the whole of Stage B's memory story. A day of trades is fifty million rows; the
    grid it reduces to, for the handful of coins with an episode that day and only inside
    their windows, is tens of thousands. The raw rows are never materialised: the coin and
    time filters are pushed into the Parquet scan and the aggregation to seconds happens as
    the rows stream past.

    Windows are dense at 1s within themselves and have gaps between them. `realized_vol`
    counts rows rather than seconds, so a window's first `RV_WINDOW_S` rows would read back
    into the previous window; every window carries `LEAD_IN_S` seconds of lead-in in front
    of the episode precisely so that the extreme, which is the only place rv is ever read,
    is past that reach. Nothing may read rv inside the lead-in.
    """
    if episodes.is_empty():
        return pl.DataFrame()
    parts = []
    for day, coins in sorted(_grid_windows(episodes).items()):
        bars = _windowed_second_bars(data_dir, day, coins)
        if bars.is_empty():
            continue
        dense = _dense_windows(coins).filter(
            (pl.col("ts_ms") >= ms(day)) & (pl.col("ts_ms") < ms(day) + 86_400_000)
        )
        kept = (
            dense.join(bars, on=["coin", "ts_ms"], how="left")
            .sort("coin", "ts_ms")
            .with_columns(
                pl.col("last_px").forward_fill().over("coin").alias("mid"),
                pl.col("notional_usd").fill_null(0.0),
                pl.col("volume").fill_null(0.0),
                pl.col("signed_notional").fill_null(0.0),
                pl.col("n_trades").fill_null(0),
            )
            .drop("last_px")
            .filter(pl.col("mid").is_not_null())
        )
        if not kept.is_empty():
            parts.append(kept)
        del bars, dense
    if not parts:
        return pl.DataFrame()
    return pl.concat(parts).unique(subset=["coin", "ts_ms"], keep="first").sort("coin", "ts_ms")


def build_episodes(
    data_dir: str | Path, start: str, end: str, settings: object, *, cache: Path | None = None
) -> pl.DataFrame:
    """Every episode in the window, with outcomes, tier and cluster on the ones that qualify.

    Written to `cache` as Parquet so the eight scripts do not each rebuild a year, and read
    back from it when it exists. Delete the file to force a rebuild.
    """
    if cache is not None and cache.exists():
        cached = pl.read_parquet(cache)
        if not cached.is_empty():
            return cached
    cfg = settings.features.episodes  # type: ignore[attr-defined]
    load_start, load_end = padded(start, end)
    liquidations = load_table(data_dir, "liquidations", load_start, load_end, venue="hyperliquid")
    if liquidations.is_empty():
        return pl.DataFrame()

    # Stage A: every episode there is, from liquidations alone, then the ones a price could
    # still qualify. `group_episodes` tolerates an empty bars frame and leaves the anchor
    # and extreme null, which is exactly the shape this stage wants.
    empty_bars = pl.DataFrame(
        schema={
            "coin": pl.String,
            "ts_ms": pl.Int64,
            "mid": pl.Float64,
            "notional_usd": pl.Float64,
        }
    )
    detector = {
        "join_gap_s": cfg.join_gap_s,
        "peak_window_s": cfg.peak_window_s,
        "participation": cfg.participation,
        "participation_window_s": cfg.participation_window_s,
    }
    window = (pl.col("start_ts") >= ms(start)) & (pl.col("start_ts") < ms(end) + 86_400_000)
    candidates = group_episodes(liquidations, empty_bars, **detector).filter(window)
    if candidates.is_empty():
        return pl.DataFrame()
    candidates = _liquidation_z(liquidations, candidates, settings)
    candidates = qualify_episodes(
        candidates.with_columns(
            # The move floor cannot be judged without a price, so it is left to Stage B; the
            # notional and relative rules are pure liquidation data and cut the list here.
            pl.lit(None, dtype=pl.Float64).alias("anchor_px"),
            pl.lit(None, dtype=pl.Float64).alias("extreme_px"),
        ),
        min_notional_usd=cfg.min_notional_usd,
        min_move_bps=cfg.min_move_bps,
        p95_lookback_d=cfg.p95_lookback_d,
        liq_z_min=cfg.liq_z_min,
    ).filter(
        (pl.col("total_notional") >= cfg.min_notional_usd)
        & (pl.col("passes_p95") | pl.col("passes_z"))
    )

    # Stage B: tape only where Stage A left something worth pricing.
    grid = episode_grid(data_dir, candidates)
    if grid.is_empty():
        return pl.DataFrame()
    episodes = group_episodes(liquidations, grid, **detector).filter(window)
    # The z was computed in Stage A from liquidations alone and is carried across rather
    # than recomputed: the same episode keys, the same numbers, no second pass.
    episodes = episodes.join(
        candidates.select("coin", "side", "start_ts", "max_liq_z"),
        on=["coin", "side", "start_ts"],
        how="left",
    )
    episodes = qualify_episodes(
        episodes,
        min_notional_usd=cfg.min_notional_usd,
        min_move_bps=cfg.min_move_bps,
        p95_lookback_d=cfg.p95_lookback_d,
        liq_z_min=cfg.liq_z_min,
    )
    episodes = attach_outcomes(episodes, grid, data_dir, load_start, load_end)
    tiers = coin_tiers(data_dir, start, end, tuple(settings.universe.core_coins))  # type: ignore[attr-defined]
    episodes = episodes.with_columns(
        pl.col("coin").replace_strict(tiers, default="minor").alias("tier")
    ).sort("start_ts")
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        episodes.write_parquet(cache)
    return episodes


def attach_outcomes(
    episodes: pl.DataFrame,
    grid: pl.DataFrame,
    data_dir: str | Path,
    start: str,
    end: str,
) -> pl.DataFrame:
    """Entry prices, horizon mids, spread and the filter inputs, read forward from the extreme.

    Public because Q4 measures Bybit-detected episodes on Hyperliquid's own tape, which is
    the same measurement on a different set of episodes, and a second copy of it would be a
    second chance to get the cost model wrong.

    Everything here reads the grid rather than the tape. The entry price is the grid's own
    mid at the first step at or after the delay, which for the historical year is the last
    print in that second, since `grid.mid` is the last trade price where no bbo exists.
    """
    if episodes.is_empty():
        return episodes
    if f"rv_{RV_WINDOW_S}s" not in grid.columns:
        grid = realized_vol(grid, windows_s=[RV_WINDOW_S], step_ms=GRID_STEP_MS)
    mids = grid.select("coin", "ts_ms", "mid").sort("ts_ms")
    out = episodes.with_columns(
        pl.col("extreme_px").alias("entry_px"),
        pl.concat_str(["coin", "side", pl.col("start_ts").cast(pl.String)], separator="/").alias(
            "key"
        ),
        (pl.col("extreme_ts") + int(ENTRY_DELAY_S * 1000)).alias("entry_ts"),
    )
    out = _mid_at(out, mids, "entry_ts", "entry_px_delayed", strategy="forward")
    out = _mid_at(out, mids, "end_ts", "mid_at_end", strategy="forward")
    out = (
        out.sort("extreme_ts")
        .join_asof(
            grid.select(
                "coin",
                pl.col("ts_ms").alias("_rv_ts"),
                pl.col(f"rv_{RV_WINDOW_S}s").alias("rv_5m"),
            ).sort("_rv_ts"),
            left_on="extreme_ts",
            right_on="_rv_ts",
            by="coin",
            strategy="backward",
        )
        .drop("_rv_ts")
    )
    for minutes in HORIZONS_MIN:
        out = out.with_columns((pl.col("extreme_ts") + minutes * 60_000).alias(f"_h{minutes}"))
        out = _mid_at(out, mids, f"_h{minutes}", f"mid_plus_{minutes}m", strategy="forward")
    out = out.drop([f"_h{minutes}" for minutes in HORIZONS_MIN])
    out = _attach_spread(out, grid, data_dir, start, end)
    out = _attach_filter_inputs(out, grid, data_dir, start, end)
    return with_clusters(out, window_s=CLUSTER_WINDOW_S)


def _mid_at(
    frame: pl.DataFrame, mids: pl.DataFrame, when: str, alias: str, *, strategy: str
) -> pl.DataFrame:
    """The grid mid at or after `when`, which is the first price a fill could have used."""
    return (
        frame.sort(when)
        .join_asof(
            mids.rename({"ts_ms": f"_{alias}_ts", "mid": alias}),
            left_on=when,
            right_on=f"_{alias}_ts",
            by="coin",
            strategy=strategy,  # type: ignore[arg-type]
        )
        .drop(f"_{alias}_ts")
    )


def _ctx_for(data_dir: str | Path, start: str, end: str, coins: list[str]) -> pl.DataFrame:
    """The asset context for the coins in play, one day at a time, columns pruned.

    The union of what `features/context.py` reads and what the spread and funding joins
    read, which is everything but `day_ntl_vlm`. `asset_ctx` is 117 million rows for the
    year; a coin filter and a day at a time keep that to what the episodes need.
    """
    return load_table(
        data_dir,
        "asset_ctx",
        start,
        end,
        columns=(
            "coin",
            "ts_ms",
            "rx_ms",
            "mark_px",
            "oracle_px",
            "mid_px",
            "funding",
            "premium",
            "open_interest",
            "impact_bid_px",
            "impact_ask_px",
        ),
        coins=coins,
    )


def _attach_spread(
    frame: pl.DataFrame, grid: pl.DataFrame, data_dir: str | Path, start: str, end: str
) -> pl.DataFrame:
    """The wider of Hyperliquid's own impact spread and the tape's own move, per episode.

    The archive sample used is the last one at or before the entry, so it precedes the trade
    it prices. The tape estimator reads the grid's 1s mid changes over the 60s after the
    extreme rather than the raw prints: the raw prints are the table this phase cannot hold,
    and a mean absolute 1s move over a cascade minute is the same order of magnitude and
    errs wide, which is the direction the cost model is supposed to err in.
    """
    coins = frame["coin"].unique().to_list()
    ctx = _ctx_for(data_dir, start, end, coins)
    if ctx.is_empty():
        frame = frame.with_columns(pl.lit(None, dtype=pl.Float64).alias("archive_spread_bps"))
    else:
        impact = (
            ctx.filter(pl.col("mid_px") > 0)
            .select(
                "coin",
                pl.col("ts_ms").alias("_ctx_ts"),
                (
                    1e4 * (pl.col("impact_ask_px") - pl.col("impact_bid_px")) / pl.col("mid_px")
                ).alias("archive_spread_bps"),
            )
            .sort("_ctx_ts")
        )
        frame = (
            frame.sort("entry_ts")
            .join_asof(
                impact, left_on="entry_ts", right_on="_ctx_ts", by="coin", strategy="backward"
            )
            .drop("_ctx_ts")
        )
    return effective_spread_bps(frame.join(_tape_spread(grid, frame), on="key", how="left"))


def _tape_spread(grid: pl.DataFrame, episodes: pl.DataFrame) -> pl.DataFrame:
    """Mean absolute 1s mid move over the 60s after the extreme, in bps, per episode.

    Membership is tagged, not joined on. Joining episodes to the grid on `coin` and
    filtering by time afterwards is a cross product: a month is 22,000 episodes against a
    grid of millions, and that join is what killed the run after the z-score one was fixed.
    Here each episode expands to the sixty second-stamps it cares about and the join is an
    equality join on `(coin, ts_ms)`.
    """
    windows = episodes.select("key", "coin", "extreme_ts").drop_nulls("extreme_ts")
    if windows.is_empty() or grid.is_empty():
        return pl.DataFrame(schema={"key": pl.String, "trade_spread_bps": pl.Float64})
    membership = windows.with_columns(
        pl.int_ranges(
            pl.col("extreme_ts") + GRID_STEP_MS,
            pl.col("extreme_ts") + 60_000 + GRID_STEP_MS,
            GRID_STEP_MS,
            dtype=pl.Int64,
        ).alias("ts_ms")
    ).explode("ts_ms")
    moves = (
        grid.sort("coin", "ts_ms")
        .with_columns(
            (pl.col("mid") - pl.col("mid").shift(1).over("coin")).abs().alias("move"),
            pl.col("mid").alias("at"),
        )
        .filter(pl.col("move") > 0)
        .select("coin", "ts_ms", "move", "at")
    )
    return (
        membership.join(moves, on=["coin", "ts_ms"], how="inner")
        .group_by("key")
        .agg((1e4 * (pl.col("move") / pl.col("at")).mean()).alias("trade_spread_bps"))
    )


def _attach_filter_inputs(
    frame: pl.DataFrame, grid: pl.DataFrame, data_dir: str | Path, start: str, end: str
) -> pl.DataFrame:
    """The columns Q6 splits the sample by: funding, session hour and listing age.

    `since_listing_days` is measured from the coin's first `asset_ctx` sample in the window,
    so a coin listed before the lake starts reads as old as the lake and no older. Q6 says
    so, because the filter it feeds is "listed under 14 days" and the lake is a year.
    """
    coins = frame["coin"].unique().to_list()
    ctx = _ctx_for(data_dir, start, end, coins)
    if ctx.is_empty():
        frame = frame.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("funding_at_start"),
            pl.lit(None, dtype=pl.Float64).alias("since_listing_days"),
        )
    else:
        samples = ctx.select(
            "coin",
            pl.col("ts_ms").alias("_f_ts"),
            pl.col("funding").alias("funding_at_start"),
        ).sort("_f_ts")
        frame = (
            frame.with_columns((pl.col("start_ts") - 1).alias("_before"))
            .sort("_before")
            .join_asof(samples, left_on="_before", right_on="_f_ts", by="coin", strategy="backward")
            .drop("_before", "_f_ts")
        )
        first_seen = ctx.group_by("coin").agg(pl.col("ts_ms").min().alias("_first"))
        frame = (
            frame.join(first_seen, on="coin", how="left")
            .with_columns(
                ((pl.col("start_ts") - pl.col("_first")) / 86_400_000).alias("since_listing_days")
            )
            .drop("_first")
        )
    _ = grid
    return frame.with_columns(
        pl.col("start_ts").cast(pl.Datetime("ms")).dt.hour().alias("session_hour")
    )


def qualifies(episodes: pl.DataFrame) -> tuple[bool, str]:
    """The preregistered sample bar: 200 episodes, 40 clusters, 8 calendar months."""
    if episodes.is_empty():
        return False, "no episodes at all"
    months = episodes.select(
        pl.col("start_ts").cast(pl.Datetime("ms")).dt.strftime("%Y-%m").alias("month")
    )["month"].n_unique()
    clusters = episodes["cluster"].n_unique()
    plural = "month" if months == 1 else "months"
    facts = f"{episodes.height} episodes, {clusters} clusters, {months} calendar {plural}"
    if episodes.height >= 200 and clusters >= 40 and months >= 8:
        return True, facts
    return False, f"{facts}; the bar is 200 episodes, 40 clusters and 8 months"


def with_btc_regime(episodes: pl.DataFrame, *, window_s: int = 300) -> pl.DataFrame:
    """Mark each episode as happening during a BTC cascade or not.

    The preregistration buckets by `btc_liq_z_60s >= 3`. A qualifying BTC episode is
    exactly that condition already evaluated and stored, so this reads the episode table
    rather than recomputing a feature over the year: an episode is market-wide when a
    qualifying BTC episode on the same side started within `window_s` of it. The isolated
    bucket is the one that has to pass on its own, because the strategy spec already
    defaults to skipping market-wide sweeps.
    """
    if episodes.is_empty():
        return episodes.with_columns(pl.lit(False).alias("btc_cascading"))
    btc = episodes.filter((pl.col("coin") == "BTC") & pl.col("qualified")).select(
        "side", pl.col("start_ts").alias("_btc_ts")
    )
    if btc.is_empty():
        return episodes.with_columns(pl.lit(False).alias("btc_cascading"))
    window_ms = window_s * 1000
    near = (
        episodes.select("key", "side", "start_ts")
        .join(btc, on="side")
        .filter((pl.col("_btc_ts") - pl.col("start_ts")).abs() <= window_ms)
        .select("key")
        .unique()
        .with_columns(pl.lit(True).alias("btc_cascading"))
    )
    return episodes.join(near, on="key", how="left").with_columns(
        pl.col("btc_cascading").fill_null(False)
    )


def episode_tape(data_dir: str | Path, episodes: pl.DataFrame, *, horizon_s: int) -> pl.DataFrame:
    """Every print from each episode's entry to `horizon_s` later, keyed by episode.

    What Q8 needs and no other question does: terminal prices cannot say whether a stop was
    hit before a target, so the path has to come from trades rather than from bars. Read one
    UTC day at a time and only inside the entry windows, so the raw rows of one day are the
    most that is ever resident.

    Returns `key`, `ts_ms`, `px`, sorted by key then time. Empty when the lake has no trades
    over the episodes' windows.
    """
    empty = pl.DataFrame(schema={"key": pl.String, "ts_ms": pl.Int64, "px": pl.Float64})
    if episodes.is_empty():
        return empty
    spans = episodes.select(
        "key", "coin", "entry_ts", (pl.col("entry_ts") + horizon_s * 1000).alias("until")
    ).drop_nulls("entry_ts")
    if spans.is_empty():
        return empty
    per_day: dict[str, dict[str, list[tuple[int, int]]]] = {}
    for row in spans.iter_rows(named=True):
        first, last = int(row["entry_ts"]), int(row["until"])
        for day_ms in range(first // 86_400_000 * 86_400_000, last + 86_400_000, 86_400_000):
            day = datetime.fromtimestamp(day_ms / 1000, UTC).strftime("%Y-%m-%d")
            per_day.setdefault(day, {}).setdefault(row["coin"], []).append((first, last))
    parts = []
    for day, coins in sorted(per_day.items()):
        trades = windowed_trades(data_dir, day, coins)
        if trades.is_empty():
            continue
        part = (
            spans.join(
                trades.with_columns(effective_ts()).select(
                    "coin", pl.col("known").alias("ts_ms"), "px"
                ),
                on="coin",
            )
            .filter((pl.col("ts_ms") >= pl.col("entry_ts")) & (pl.col("ts_ms") <= pl.col("until")))
            .select("key", "ts_ms", "px")
        )
        if not part.is_empty():
            parts.append(part)
        del trades
    if not parts:
        return empty
    return pl.concat(parts).unique(subset=["key", "ts_ms"], keep="first").sort("key", "ts_ms")


def feature_grid(
    data_dir: str | Path, episodes: pl.DataFrame, settings: object, grid: pl.DataFrame
) -> pl.DataFrame:
    """The episode grid with the tick features an entry rule needs.

    Q2 evaluates the exhaustion rule tick by tick, which needs the liquidation intensity,
    the CVD slope and the mark divergence percentile on the same frame. It reads the grid
    Stage B already built rather than the tape, because the tape is the table that does not
    fit and every one of these features is an aggregate the grid already carries.
    """
    features = settings.features  # type: ignore[attr-defined]
    exhaustion = settings.exhaustion  # type: ignore[attr-defined]
    if grid.is_empty() or episodes.is_empty():
        return pl.DataFrame()
    coins = episodes["coin"].unique().to_list()
    days = (
        episodes.select(
            pl.col("start_ts").cast(pl.Datetime("ms")).dt.strftime("%Y-%m-%d").alias("day")
        )["day"]
        .unique()
        .sort()
        .to_list()
    )
    first, last = days[0], days[-1]
    load_start, load_end = padded(first, last)
    liquidations = load_table(
        data_dir, "liquidations", load_start, load_end, venue="hyperliquid", coins=coins
    )
    out = grid
    if not liquidations.is_empty():
        out = liq_intensity(
            liquidations,
            out,
            windows_s=list(features.windows_s),
            step_ms=GRID_STEP_MS,
            weights={"hyperliquid": 1.0},
        )
    out = cvd(
        out,
        windows_s=[int(features.cvd_slope_window_s)],
        slope_window_s=int(features.cvd_slope_window_s),
        step_ms=GRID_STEP_MS,
    )
    ctx = _ctx_for(data_dir, load_start, load_end, coins)
    return with_ctx(
        out,
        ctx,
        windows_s=[int(features.proxy.oi_delta_window_s)],
        step_ms=GRID_STEP_MS,
        quantile=exhaustion.mark_mid_pct_threshold / 100.0,
    )


def daily_grid(data_dir: str | Path, day: str, coins: Sequence[str]) -> pl.DataFrame:
    """One UTC day's 1s grid for the named coins, aggregated without ever holding the tape.

    `build_grid` is the definition of a bar and this has to agree with it exactly; the
    difference is only that the aggregation happens inside the Parquet scan, so a day of
    fifty million prints never becomes fifty million rows in memory. `tests/test_research_lib.py`
    asserts the two produce identical frames.

    A question that only needs tape around known episodes should use `episode_grid`, which
    reads far less. This exists for Q3, which has to sweep the whole year to count the
    proxy's false positives and cannot restrict itself to where the answer already is.
    """
    scan = scan_day(data_dir, "trades", day, venue="hyperliquid")
    if scan is None or not coins:
        return pl.DataFrame()
    known = pl.when(pl.col("rx_ms") > 0).then(pl.col("rx_ms")).otherwise(pl.col("ts_ms"))
    per_second = (
        scan.filter(pl.col("coin").is_in(list(coins)))
        .with_columns(known.alias("known"))
        .with_columns(
            ((pl.col("known") // GRID_STEP_MS) * GRID_STEP_MS + GRID_STEP_MS).alias("ts_ms")
        )
        .group_by("coin", "ts_ms")
        .agg(
            pl.col("px").sort_by("known").last().alias("last_px"),
            (pl.col("px") * pl.col("sz")).sum().alias("notional_usd"),
            pl.col("sz").sum().alias("volume"),
            (
                pl.when(pl.col("side") == "buy")
                .then(pl.col("px") * pl.col("sz"))
                .otherwise(-pl.col("px") * pl.col("sz"))
            )
            .sum()
            .alias("signed_notional"),
            pl.len().alias("n_trades"),
        )
        .collect(engine="streaming")
    )
    if per_second.is_empty():
        return pl.DataFrame()
    start = ms(day)
    dense = pl.DataFrame({"coin": sorted(set(coins))}, schema={"coin": pl.String}).join(
        pl.DataFrame(
            {"ts_ms": range(start, start + 86_400_000, GRID_STEP_MS)}, schema={"ts_ms": pl.Int64}
        ),
        how="cross",
    )
    return (
        dense.join(per_second, on=["coin", "ts_ms"], how="left")
        .sort("coin", "ts_ms")
        .with_columns(
            pl.col("last_px").forward_fill().over("coin").alias("mid"),
            pl.col("notional_usd").fill_null(0.0),
            pl.col("volume").fill_null(0.0),
            pl.col("signed_notional").fill_null(0.0),
            pl.col("n_trades").fill_null(0),
        )
        .drop("last_px")
    )
