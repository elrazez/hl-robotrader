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
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import numpy as np
import polars as pl

from liqfade.features.context import with_ctx
from liqfade.features.episodes import group_episodes, qualify_episodes
from liqfade.features.flow import cvd, realized_vol
from liqfade.features.grid import build_grid, effective_ts
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


def load_table(
    data_dir: str | Path, table: str, start: str, end: str, *, venue: str | None = None
) -> pl.DataFrame:
    """Read one table's partitions over a date range into polars.

    Missing days are not an error: a lake that never recorded a day has no directory for
    it, and a question that needs a day it does not have says inconclusive rather than
    crashing. Returns an empty frame with no columns when the table is absent entirely,
    which callers check with `.is_empty()`. Every file of a table shares one schema, which
    `data/schemas.py` enforces on write, so the whole list reads in one call.
    """
    root = Path(data_dir) / "parquet" / table
    files = [
        str(path)
        for day in dates(start, end)
        for path in sorted((root / f"date={day}").glob("*.parquet"))
        if venue is None or path.name.startswith(f"{venue}_") or path.name.startswith("all_")
    ]
    if not files:
        return pl.DataFrame()
    frame = pl.read_parquet(files)
    if venue is not None and "venue" in frame.columns:
        frame = frame.filter(pl.col("venue") == venue)
    return frame


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
# Chunked by coin and calendar month, because a year of one coin's 1s grid is 31 million
# rows and twenty coins of it is not going to fit in memory. Each chunk is loaded with a
# 24h warm-up in front, so the z-score's trailing distribution is full before the first
# episode of the month is judged, and a 65 minute pad behind, so the +60 minute horizon of
# an episode on the last day of the month has tape to read.
#
# ponytail: a year of one coin costs about 275s through the whole feature set, against the
# 60s budget in prompts/03_research.md, so a year of a twenty coin universe is a few hours
# rather than twenty minutes. Nothing loops over rows; the cost is that a year at 1s is 31.5
# million rows and every rolling op with `.over("coin")` re-partitions and re-materialises
# them. The upgrade path, if the owner ever needs the run to be interactive, is to fold the
# four flow functions into one pass over one sort instead of four, which measurement puts at
# roughly a 3x win. Not taken now: it would merge four tested seams into one to speed up a
# job that runs once.
# ---------------------------------------------------------------------------------------

WARMUP_MS = 25 * 3_600_000  # the 24h z lookback plus an hour of slack for the longest window
HORIZON_PAD_MS = 65 * 60_000  # the +60 minute horizon plus slack
GRID_STEP_MS = 1000
HORIZONS_MIN = (1, 5, 15, 30, 60)
CLUSTER_WINDOW_S = 300
# The window the cascade rule and the preregistration both name: liq_z_60s.
Z_WINDOW_S = 60
# rv_5m: the vol the stop distance is measured in (config intent.k_stop).
RV_WINDOW_S = 300


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

    Reads `asset_ctx.day_ntl_vlm`, which is Hyperliquid's own rolling 24h figure, so the
    median over the window is the coin's typical day. Coins with no `asset_ctx` rows are
    minor: an unranked coin is not a top-20 coin.
    """
    ctx = load_table(data_dir, "asset_ctx", start, end)
    if ctx.is_empty():
        return {}
    ranked = (
        ctx.group_by("coin")
        .agg(pl.col("day_ntl_vlm").median().alias("volume"))
        .sort("volume", descending=True)
    )
    tiers = {}
    majors = [coin for coin in ranked["coin"].to_list() if coin not in core][:20]
    for coin in ranked["coin"].to_list():
        tiers[coin] = "core" if coin in core else ("major" if coin in majors else "minor")
    return tiers


def _chunk_episodes(
    data_dir: str | Path, start: str, end: str, settings: object
) -> pl.DataFrame | None:
    """Detect and qualify every episode starting inside [start, end], with its outcomes."""
    features = settings.features  # type: ignore[attr-defined]
    cfg = features.episodes
    load_start, load_end = padded(start, end)
    trades = load_table(data_dir, "trades", load_start, load_end, venue="hyperliquid")
    liquidations = load_table(data_dir, "liquidations", load_start, load_end, venue="hyperliquid")
    if trades.is_empty() or liquidations.is_empty():
        return None

    grid_start = ms(load_start)
    grid_end = ms(load_end) + 86_400_000
    grid = build_grid(trades, None, start_ms=grid_start, end_ms=grid_end, step_ms=GRID_STEP_MS)
    intensity = liq_intensity(
        liquidations,
        grid,
        windows_s=list(features.windows_s),
        step_ms=GRID_STEP_MS,
        weights={"hyperliquid": 1.0},
    )
    if Z_WINDOW_S not in features.windows_s:
        raise SystemExit(f"features.windows_s must contain {Z_WINDOW_S}, the z window")
    scored = robust_z(
        intensity,
        [f"liq_long_usd_{Z_WINDOW_S}s", f"liq_short_usd_{Z_WINDOW_S}s"],
        lookback_h=features.z_lookback_h,
        sample_s=int(features.z_grid_s),
        robust=features.z_robust,
    )
    episodes = group_episodes(
        liquidations,
        grid,
        join_gap_s=cfg.join_gap_s,
        peak_window_s=cfg.peak_window_s,
        participation=cfg.participation,
        participation_window_s=cfg.participation_window_s,
    )
    episodes = _attach_max_z(episodes, scored)
    episodes = qualify_episodes(
        episodes,
        min_notional_usd=cfg.min_notional_usd,
        min_move_bps=cfg.min_move_bps,
        p95_lookback_d=cfg.p95_lookback_d,
        liq_z_min=cfg.liq_z_min,
    )
    window = (pl.col("start_ts") >= ms(start)) & (pl.col("start_ts") < ms(end) + 86_400_000)
    return attach_outcomes(episodes.filter(window), grid, trades, data_dir, load_start, load_end)


def _attach_max_z(episodes: pl.DataFrame, scored: pl.DataFrame) -> pl.DataFrame:
    """The highest liq z of the liquidated side reached anywhere inside the episode."""
    window_s = Z_WINDOW_S
    per_side = pl.concat(
        [
            scored.select(
                "coin",
                "ts_ms",
                pl.lit(side).alias("side"),
                pl.col(f"liq_{side}_usd_{window_s}s_z").alias("z"),
            )
            for side in ("long", "short")
        ]
    )
    joined = episodes.join(per_side, on=["coin", "side"], how="left").filter(
        (pl.col("ts_ms") >= pl.col("start_ts")) & (pl.col("ts_ms") <= pl.col("end_ts"))
    )
    peak = joined.group_by("coin", "side", "start_ts").agg(pl.col("z").max().alias("max_liq_z"))
    return episodes.join(peak, on=["coin", "side", "start_ts"], how="left")


def attach_outcomes(
    episodes: pl.DataFrame,
    grid: pl.DataFrame,
    trades: pl.DataFrame,
    data_dir: str | Path,
    load_start: str,
    load_end: str,
) -> pl.DataFrame:
    """Entry prices, horizon mids, spread and the filter inputs, read forward from the extreme.

    Public because Q4 measures Bybit-detected episodes on Hyperliquid's own tape, which is
    the same measurement on a different set of episodes, and a second copy of it would be
    a second chance to get the cost model wrong.
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
    )
    entry_at = (pl.col("extreme_ts") + int(ENTRY_DELAY_S * 1000)).alias("entry_ts")
    prints = (
        trades.with_columns(effective_ts())
        .select("coin", pl.col("known").alias("_print_ts"), pl.col("px").alias("entry_px_delayed"))
        .sort("_print_ts")
    )
    # The first actual print at or after the delay, not the next grid mid. The two differ by
    # up to a step, and Q8 measures excursions against this price, so a grid mid would show
    # a trade going against itself before it was ever opened.
    out = (
        out.with_columns(entry_at)
        .sort("entry_ts")
        .join_asof(prints, left_on="entry_ts", right_on="_print_ts", by="coin", strategy="forward")
        .drop("_print_ts")
    )
    out = _mid_at(out, mids, "end_ts", "mid_at_end")
    # rv at the extreme, which reads only the five minutes before it. This is the unit
    # k_stop is expressed in, so Q8 needs it on every episode.
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
        out = _mid_at(out, mids, f"_h{minutes}", f"mid_plus_{minutes}m")
    out = out.drop([f"_h{minutes}" for minutes in HORIZONS_MIN])
    out = _attach_spread(out, trades, data_dir, load_start, load_end)
    out = _attach_filter_inputs(out, trades, data_dir, load_start, load_end)
    return with_clusters(out, window_s=CLUSTER_WINDOW_S)


def _attach_filter_inputs(
    frame: pl.DataFrame, trades: pl.DataFrame, data_dir: str | Path, start: str, end: str
) -> pl.DataFrame:
    """The columns Q6 splits the sample by: funding, session hour and listing age.

    `since_listing_days` is measured from the coin's first print inside the loaded window,
    so a coin listed before the lake starts reads as old as the lake and no older. Q6 says
    so, because the filter it feeds is "listed under 14 days" and the lake is a year.
    """
    ctx = load_table(data_dir, "asset_ctx", start, end)
    if ctx.is_empty():
        frame = frame.with_columns(pl.lit(None, dtype=pl.Float64).alias("funding_at_start"))
    else:
        samples = ctx.select(
            "coin", pl.col("ts_ms").alias("_f_ts"), pl.col("funding").alias("funding_at_start")
        ).sort("_f_ts")
        frame = (
            frame.with_columns((pl.col("start_ts") - 1).alias("_before"))
            .sort("_before")
            .join_asof(samples, left_on="_before", right_on="_f_ts", by="coin", strategy="backward")
            .drop("_before", "_f_ts")
        )
    first_print = trades.group_by("coin").agg(pl.col("ts_ms").min().alias("_first_print"))
    return (
        frame.join(first_print, on="coin", how="left")
        .with_columns(
            ((pl.col("start_ts") - pl.col("_first_print")) / 86_400_000).alias(
                "since_listing_days"
            ),
            pl.col("start_ts").cast(pl.Datetime("ms")).dt.hour().alias("session_hour"),
        )
        .drop("_first_print")
    )


def _mid_at(frame: pl.DataFrame, mids: pl.DataFrame, when: str, alias: str) -> pl.DataFrame:
    """The first grid mid at or after `when`, which is the first price a fill could use."""
    return (
        frame.sort(when)
        .join_asof(
            mids.rename({"ts_ms": f"_{alias}_ts", "mid": alias}),
            left_on=when,
            right_on=f"_{alias}_ts",
            by="coin",
            strategy="forward",
        )
        .drop(f"_{alias}_ts")
    )


def _attach_spread(
    frame: pl.DataFrame, trades: pl.DataFrame, data_dir: str | Path, start: str, end: str
) -> pl.DataFrame:
    """The wider of Hyperliquid's own impact spread and the tape's flip-to-flip move.

    The archive sample used is the last one at or before the entry, so it precedes the
    trade it prices. The tape estimator is the mean absolute price change between
    consecutive prints whose aggressor side flips, over the 60s after the extreme, which is
    the classic effective spread and reads the cascade minute itself rather than the calm
    minute before it.
    """
    ctx = load_table(data_dir, "asset_ctx", start, end)
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
    return effective_spread_bps(frame.join(_tape_spread(trades, frame), on="key", how="left"))


def _tape_spread(trades: pl.DataFrame, episodes: pl.DataFrame) -> pl.DataFrame:
    """Mean absolute move between consecutive prints that flip aggressor side, per episode."""
    flips = (
        trades.sort("coin", "ts_ms")
        .with_columns(
            (pl.col("side") != pl.col("side").shift(1).over("coin")).alias("flipped"),
            (pl.col("px") - pl.col("px").shift(1).over("coin")).abs().alias("move"),
            pl.col("px").alias("at"),
        )
        .filter(pl.col("flipped"))
        .select("coin", "ts_ms", "move", "at")
    )
    windows = episodes.select(
        "key", "coin", "extreme_ts", (pl.col("extreme_ts") + 60_000).alias("_until")
    )
    joined = windows.join(flips, on="coin", how="left").filter(
        (pl.col("ts_ms") > pl.col("extreme_ts")) & (pl.col("ts_ms") <= pl.col("_until"))
    )
    return joined.group_by("key").agg(
        (1e4 * (pl.col("move") / pl.col("at")).mean()).alias("trade_spread_bps")
    )


def build_episodes(
    data_dir: str | Path, start: str, end: str, settings: object, *, cache: Path | None = None
) -> pl.DataFrame:
    """Every qualifying episode in the window, with outcomes, tier and cluster attached.

    Written to `cache` as Parquet so the eight scripts do not each rebuild a year, and read
    back from it when it already covers the window. Delete the file to force a rebuild.
    """
    if cache is not None and cache.exists():
        cached = pl.read_parquet(cache)
        if not cached.is_empty():
            return cached
    parts = [
        chunk
        for first, last in month_chunks(start, end)
        if (chunk := _chunk_episodes(data_dir, first, last, settings)) is not None
        and not chunk.is_empty()
    ]
    if not parts:
        return pl.DataFrame()
    episodes = pl.concat(parts, how="diagonal_relaxed").sort("start_ts")
    tiers = coin_tiers(data_dir, start, end, tuple(settings.universe.core_coins))  # type: ignore[attr-defined]
    episodes = episodes.with_columns(
        pl.col("coin").replace_strict(tiers, default="minor").alias("tier")
    )
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        episodes.write_parquet(cache)
    return episodes


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
    hit before a target, so the path has to come from trades rather than from bars. Loaded
    a month at a time for the same reason the episodes are.

    Returns `key`, `ts_ms`, `px`, sorted by key then time. Empty when the lake has no
    trades over the episodes' window.
    """
    empty = pl.DataFrame(schema={"key": pl.String, "ts_ms": pl.Int64, "px": pl.Float64})
    if episodes.is_empty():
        return empty
    windows = episodes.select(
        "key",
        "coin",
        "entry_ts",
        (pl.col("entry_ts") + horizon_s * 1000).alias("_until"),
        pl.col("entry_ts").cast(pl.Datetime("ms")).dt.strftime("%Y-%m").alias("_month"),
    )
    parts = []
    for month in sorted(windows["_month"].unique().to_list()):
        first = date.fromisoformat(f"{month}-01")
        last = (first + timedelta(days=32)).replace(day=1)
        trades = load_table(
            data_dir, "trades", first.isoformat(), last.isoformat(), venue="hyperliquid"
        )
        if trades.is_empty():
            continue
        part = (
            windows.filter(pl.col("_month") == month)
            .join(
                trades.with_columns(effective_ts()).select(
                    "coin", pl.col("known").alias("ts_ms"), "px"
                ),
                on="coin",
            )
            .filter((pl.col("ts_ms") >= pl.col("entry_ts")) & (pl.col("ts_ms") <= pl.col("_until")))
            .select("key", "ts_ms", "px")
        )
        if not part.is_empty():
            parts.append(part)
    return pl.concat(parts).sort("key", "ts_ms") if parts else empty


def feature_grid(data_dir: str | Path, first: str, last: str, settings: object) -> pl.DataFrame:
    """One month of the grid with the tick features an entry rule needs, plus its pads.

    Q2 evaluates the exhaustion rule tick by tick, which needs the liquidation intensity,
    the CVD slope, the mark divergence percentile and the running extreme all on the same
    frame. Built here rather than in the script so the windows come from config in one
    place. Returns an empty frame when the month has no trades.
    """
    features = settings.features  # type: ignore[attr-defined]
    exhaustion = settings.exhaustion  # type: ignore[attr-defined]
    load_start, load_end = padded(first, last)
    trades = load_table(data_dir, "trades", load_start, load_end, venue="hyperliquid")
    if trades.is_empty():
        return pl.DataFrame()
    liquidations = load_table(data_dir, "liquidations", load_start, load_end, venue="hyperliquid")
    ctx = load_table(data_dir, "asset_ctx", load_start, load_end)
    grid = build_grid(
        trades,
        None,
        start_ms=ms(load_start),
        end_ms=ms(load_end) + 86_400_000,
        step_ms=GRID_STEP_MS,
    )
    if not liquidations.is_empty():
        grid = liq_intensity(
            liquidations,
            grid,
            windows_s=list(features.windows_s),
            step_ms=GRID_STEP_MS,
            weights={"hyperliquid": 1.0},
        )
    grid = cvd(
        grid,
        windows_s=[int(features.cvd_slope_window_s)],
        slope_window_s=int(features.cvd_slope_window_s),
        step_ms=GRID_STEP_MS,
    )
    return with_ctx(
        grid,
        ctx,
        windows_s=[int(features.proxy.oi_delta_window_s)],
        step_ms=GRID_STEP_MS,
        quantile=exhaustion.mark_mid_pct_threshold / 100.0,
    )
