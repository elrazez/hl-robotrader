# Phase 3 preregistration

Committed as the first commit of Phase 3, before any script had read the real lake. It is
transcribed from `prompts/03_research.md`, sections "Decisions already made" and
"Decisions from the Phase 3 grill (2026-09-04)". `SUMMARY.md` cites this file's commit
hash so anyone reading a DECISION can check it against what was promised beforehand.

Any later change is a dated amendment in `docs/DECISIONS.md` stating what had already been
seen when the change was made. Nothing in this file may be edited after the first run.

## What this phase is

Eight written yes/no/inconclusive decisions with sample sizes. Not code that trades. The
job is measurement; parameters are not tuned to make results look good. No `strategy/`,
`sizing/`, `risk/`, `backtest/` or live code is built in this phase.

## Ground rules

**No lookahead, mechanically enforced.** Every feature takes arrays sorted by timestamp and
an explicit decision time or grid; it may only use rows with `effective_ts < t` where
`effective_ts = rx_ms if rx_ms > 0 else ts_ms`. Every feature has a property test that
mutates rows after `t` and asserts the output at `t` is bit-identical.

**Report everything net of costs.** Every table shows three columns: gross, net-9bps and
net-stress. Every DECISION reads net-stress. A table that only shows gross is a bug.

**Honest outcomes.** Each question ends with `DECISION: yes | no | inconclusive`, the
episode count, the cluster count, and the effect size with a cluster-bootstrap confidence
interval. If the data cannot answer the question, it says inconclusive. A no is never
softened.

## Step 0, the data precondition

`asset_ctx` holds the year from Hyperliquid's own archive: open interest, mark, oracle,
funding and premium per coin at the cadence recorded in `docs/DATA_SOURCES.md`
(`source = hl_archive`, `rx_ms = 0`), validated against `fundingHistory` and the twelve
Tardis days. So `context.py` is real for the year at that cadence, Q6's funding filter is
evaluable, and all three of the proxy's gates are calibrated on the year rather than on
twelve days. If `asset_ctx` is missing or short, the phase stops and says so. No
trades-only workaround is built. `scripts/research/oi_from_fills.py` gives block-level OI
changes from `fills_full` if a gate needs finer than the archive's cadence.

Hyperliquid's `open_interest` is both sides of the book, longs plus shorts, so it is twice
what Binance and Bybit publish. It is halved before any cross-venue comparison and never
inside a same-venue feature: `oi_delta_w` and the proxy's OI gate read one venue's own
series, where the factor cancels and halving would only make the number wrong in a
different way.

The recorder has not yet run on the VPS (as of 2026-09-04 the lake has no recorder rows: no
`bbo`, no live `asset_ctx`). `book.py` is NaN for the year: there is no historical `bbo`
and `l2` is hourly snapshots on four coins. The live bot reads the same `asset_ctx` table
from `activeAssetCtx` at every block, so features have one code path.

## Features

Sync functions on numpy arrays and polars frames in `liqfade/features/`:

- `grid.py`: a 1s mid/price grid and a 250ms decision grid from trades/bbo. For historical
  S3-only windows where bbo is absent, `mid = last trade price`, and the docstring says so.
- `intensity.py`: `liq_long/short_usd_w` for w in config windows, venue-weighted
  aggregation, `liq_imbalance_w`.
- `zscore.py`: robust z of `log1p` on the 24h trailing distribution, median/MAD, computed
  on the 1 minute grid.
- `proxy.py`: `hl_forced_proxy` per the spec, aggressive volume ratio gated by
  `oi_delta < 0` and mark-mid divergence percentile.
- `flow.py`: `ret_w`, `rv_1m`, `rv_5m`, `cvd_w` and slope, `disp_w` vs the 5 minute
  pre-window VWAP anchor.
- `book.py`: `spread_bps`, depth within 10bps per side, `book_imbalance`. Returns NaN
  columns when no l2 data exists for the window, and callers must handle NaN.
- `context.py`: `oi_delta_w`, funding, premium, `btc_ret_5m`, `btc_liq_z_60s`, session hour
  bucket.
- `levels.py`: supply/demand zones, below.

Every docstring states window, units, inputs and NaN behavior.

### levels.py, the zone feature set

Built from 1m candles (the `candles_1m` table, derived from `trades` by
`liqfade derive candles`; the API holds no 1m history), per coin, usable at any decision
time with only completed bars. A bar at time t is usable from its close, never mid-bar; the
no-lookahead property test applies here too.

- swing highs and lows: fractal points where a bar's high (low) exceeds the N bars on each
  side, N from config (`features.levels.fractal_n`, default 12).
- untested levels: a swing level is fresh until price trades through it; freshness and
  touch count are tracked.
- range edges: rolling highest high and lowest low over `features.levels.range_d` days
  (default 5), plus prior UTC day and prior week high/low.
- derived features at decision time: `dist_to_support_bps`, `dist_to_resistance_bps`
  (nearest level on each side, NaN when none within `features.levels.max_dist_bps`, default
  200), the same distances normalized by `rv_5m`, the nearest level's freshness and touch
  count, and `extreme_in_zone` (bool: is the current cascade extreme within
  `features.levels.zone_width_bps`, default 15, of a level).

The detector stays dumb and transparent: fractals and rolling extremes only. No ML, no
volume profile in this phase; those are Phase 7 candidates if Q7 says zones matter at all.

## Episode detection

Lives in `liqfade/features/episodes.py`, not in `strategy/`. Per coin per side: take exact
liquidation events (from S3 data), join consecutive events with gaps under 60s into an
episode, keep episodes with total notional at or above the coin's trailing 30 day p95 of
episode notional OR `liq_z_60s >= 3.0` at any point, AND clearing both absolute floors:

- total notional >= $250,000
- `abs(anchor_px - extreme_px) / anchor_px >= 30 bps`

The floors are roughly 2x net-stress cost; below them the trade cannot pay even at 100%
reversion. The relative p95 and z rules do the per-coin tiering; the floors only kill
dead-tape noise, since a relative rule emits a fixed rate of episodes no matter how quiet
the market is. Both go in config with unit and rationale comments. **The floors are not
tunable by this phase.**

Recorded per episode: coin, side, start_ts, end_ts, anchor_px (mid at start), extreme_px,
extreme_ts, total_notional, n_events, peak_liq_15s, notional_at_25pct_participation.

Output: one Parquet file `reports/research/episodes.parquet`. `episodes.py` writes both the
filtered and unfiltered counts. This detector is the ground truth the strategy's online
detector is later measured against.

Coin tiers for bucketing, by 30 day median daily notional volume: core = `universe.core_coins`,
major = top 20 excluding core, minor = rest.

## Cost model

Every table reports three columns: gross, net-9bps, net-stress. Every DECISION reads
net-stress and nothing else.

- net-9bps = 0.09% round trip (0.045% taker per side).
- net-stress = 9 bps fees, entry at the first print after `extreme_ts + 1s`, and one
  estimated effective spread subtracted per side.
- Effective spread comes from `asset_ctx`: `(impact_ask_px - impact_bid_px) / mid_px` from
  the last archive sample at or before the entry time (one per coin per minute, the
  exchange's own impact prices, so it reflects depth, not just the touch). No lookahead: the
  sample precedes the entry. It is cross-checked with the trade-based estimator (mean
  absolute price change between consecutive prints whose aggressor side flips, over the 60s
  after the extreme) and both are reported; if the trade-based number is larger on a cascade
  minute, the larger is used. The 12 Tardis `tardis_book_snapshot_5` days are a sanity check
  on both. The recorder's live `bbo` replaces them once a few weeks exist.
- Funding is ignored at these holds. Every report says so.
- `mid = last trade price` for the historical year is accepted, on the condition that the
  spread haircut above is applied.

## Sample size, and what an observation is

A **cluster** is every episode on the same side whose start time falls within a 5-minute
window, across all coins.

A **qualifying sample** is >= 200 episodes AND >= 40 clusters AND >= 8 distinct calendar
months containing a cluster.

All confidence intervals come from a **cluster bootstrap** (resample whole clusters). The
per-episode bootstrap is banned from every report, because a market-wide sweep liquidates
hundreds of coins inside one minute and they are one observation, not hundreds. Every table
carries a mandatory **top-5-clusters-removed** rerun of its own headline number, not just
the summary table.

## The eight questions

**Q1 reversion** (`scripts/research/q1_reversion.py`). For each episode, mid at end_ts and
at +1, +5, +15, +30, +60 minutes; reversion fraction =
`(mid_t - extreme_px) / (anchor_px - extreme_px)` signed so positive = reversion.
Distributions by tier and by notional quartile, gross and net. Carries the capacity column
and the notional histogram.

DECISION bar: mean net-stress PnL per episode >= 15 bps at a horizon pre-registered as
**+15 minutes**, with the cluster-bootstrap 95% CI lower bound above 0. **+5 minutes** is
the only permitted secondary horizon and needs a 99% CI because it is a second look. The
other three horizons are descriptive and cannot produce a yes on their own. Median is
reported beside the mean as a concentration check, never as the criterion. A result between
15 and 24 bps is written "marginal, Q2 must supply the rest", not yes. 24 bps is the
strategy spec's own `min_edge_multiple` floor and nothing below it will ever trade.

**Q2 exhaustion timing** (`q2_exhaustion.py`). Entry at exhaustion (the spec's conditions,
evaluated on the research grid) vs fixed delays of 30/60/120s after episode start. Compare
net reversion captured. DECISION yes if exhaustion beats every fixed delay.

**Q3 proxy calibration** (`q3_proxy.py`). The headline is the **full three-gate proxy** as
the spec defines it, over the full year: aggressive volume ratio versus its trailing median,
gated by `oi_delta_w < 0` and mark-versus-mid above its trailing p95, both read from the
archive-backed `asset_ctx` at the archive's cadence. Proxy triggers are matched against exact
episodes within a 120s window. Precision, recall and F1 by tier at thresholds 2/3/4/6,
cluster-aware, against exact S3 liquidations. The **trades-only variant** (no gates) is
computed on the same year and reported beside it: the difference is the gate lift, with no
DECISION of its own. Both are rerun at 60 recorder days, where the gates read live
block-level values instead of the archive's sampling. Writes `reports/proxy_calibration.md`
at that exact path; other docs reference it.

DECISION yes if any threshold reaches precision >= 0.6 AND recall >= 0.4 on core+major tiers.

**The trigger branch, decided by Q3 and written into its report.** If the three-gate proxy
reaches precision >= 0.6 and recall >= 0.4 on core+major, the v0 live trigger is HL-native
and Q4 stays inconclusive and non-blocking, revisited in Phase 7. If it does not, the v0
trigger is cross-venue (Bybit is the only complete feed) and Q4 becomes a Phase 4 entry
requirement at a reduced bar of **>= 15 clusters and >= 60 recorder days**, reduced because
Q4 is then asking whether the cross-venue trigger fires on HL-reverting events, not
establishing the edge, which Q1 already did on exact data. `SUMMARY.md` states which trigger
Phase 4 is being built on.

**Q4 cross-venue** (`q4_crossvenue.py`). Bybit episode without a matching HL episode within
120s: does HL still revert per Q1's measure? First cut runs on the 12 Tardis days that
already hold Binance/Bybit/OKX liquidations alongside exact HL ones; rerun 14 days after the
VPS recorder starts (it has not started as of 2026-09-04; the lake holds no recorder rows at
all). Twelve days cannot reach any bar, but a cross-venue cascade that never once coincides
with an HL episode is worth knowing before Q3 has run. Whether Q4 blocks Phase 4 is decided
by Q3's trigger branch.

**Q5 partial liquidation rhythm** (`q5_rhythm.py`). Every qualifying episode (the $250k floor
already clears the 100k partial-liquidation threshold the spec named): histogram of
inter-event gaps; is there a mass near 30s, the documented chunk cooldown? DECISION yes if
the 25-35s bin exceeds 2x the neighboring bins.

**Q6 filters** (`q6_filters.py`). For each candidate filter from the spec (spread cap, BTC
cascading, funding extreme, listing age, session bucket), split Q1's episode outcomes by
filter pass/fail. The loss tail is Q8's stopped-out episodes, not the worst decile of
terminal reversion. DECISION per filter: keep if it removes at least 2x more loss-tail
episodes than it removes sample overall.

**Q7 zone confluence** (`q7_zones.py`). Split Q1's episodes by whether the cascade extreme
landed in a pre-existing supply/demand zone (`extreme_in_zone` from `levels.py`, using only
levels that existed before the cascade started). Compare net reversion, hit rate and the loss
tail for in-zone vs out-of-zone episodes, by tier, at zone widths 10, 15 and 25 bps. Also
test the reverse use: does distance-to-zone at cascade start predict how far the cascade
extends, a stop-placement input?

DECISION yes if in-zone episodes beat out-of-zone net-stress reversion with the
cluster-bootstrap 95% CIs separated, on at least 100 in-zone episodes. A yes wires the zone
rule into Phase 4 as an optional confluence gate; a no leaves it built but off.

**Q8 path and MAE** (`q8_path.py`), the second gate. Nothing in Q1 to Q7 measures the path,
so nothing measures the loss: terminal reversion cannot tell you whether a stop is hit before
a target, and a strategy with +20 bps mean reversion and a 60% stop-out rate loses money.
Built from **trades, not 1m bars**, or MAE is meaningless. For every episode, the distribution
of **maximum adverse excursion from an entry at the first print after `extreme_ts + 1s`**, in
bps and in units of `rv_5m`; then the joint outcome over a grid of `k_stop` in
{1.0, 1.5, 2.5, infinity} x `k_target` in {0.3, 0.5, 0.8} with a 15-minute time stop,
net-stress, reported as a hit/stop/timeout breakdown per cell.

DECISION yes only if some cell beats hold-to-horizon, has a stop-out rate under 50%, and its
neighbours in the grid are also positive. An isolated positive cell is the multiple-comparison
problem wearing a hat. Q8 runs immediately after Q1 because it is the question most likely to
kill the project.

## Capacity

Folded into Q1 rather than made a question. Each episode carries
`notional_at_25pct_participation`, computed from the trades in the 60s after the extreme.
`SUMMARY.md` reports the capacity-weighted edge in dollars per episode and annualised dollars
at the sample's own episode rate.

## Robustness, pre-registered, all three mandatory, judged on point estimates

With roughly 40 clusters a per-slice CI means nothing; only the pooled number carries one.

1. **Time**: split the lake into 4 quarters. The headline must be positive in >= 3 of 4, and
   the pooled CI must survive dropping the single best quarter.
2. **Coin**: the headline must be positive excluding BTC and ETH, and again excluding HYPE. A
   result that needs one coin is that coin's idiosyncrasy.
3. **Regime**: bucket clusters by whether BTC was itself cascading (`btc_liq_z_60s >= 3`). The
   isolated-coin bucket must pass on its own. Market-wide sweeps are reported for the risk
   model, not for the yes, since the strategy spec already defaults to skipping them.

Anything that passes pooled and fails a split is written "conditional yes, gated on X", and
the gate becomes Phase 4 config. Never a plain yes.

## Stop rules

The project stops on any one of these, evaluated on the pre-registered run only.

1. **No edge.** Q1 net-stress mean at +15 min is <= 0 bps on a qualifying sample.
2. **Unharvestable edge.** Q1 passes, but no Q8 cell has positive net-stress with a stop-out
   rate under 50% and positive neighbours.
3. **Lottery edge.** The headline dies with the top 5 clusters removed, or fails 2 or more of
   the 3 robustness splits.
4. **No capacity.** Capacity-weighted expected net dollars, at the sample's own episode rate
   and 25% participation, is under $2,000/year (roughly 5x infra plus margin). A clean 25 bps
   edge you can put $800 into is not a business.
5. **No trigger.** Q1 and Q8 pass on exact liquidations, but the three-gate proxy is under
   precision 0.5 and Q4 cannot be answered for months. Park the project until the recorder has
   the data; do not build Phase 4 blind.

**Pivot, not stop.** If Q1 fails but continuation is strongly tradable, that is the momentum
variant, out of v0 scope. It is written up as a new thesis needing its own Phase 3 and this one
is closed. The sign is not flipped and carried on.

## Anti-tinkering

After the pre-registered run, at most **two re-cuts of the episode definition**, each logged in
`docs/DECISIONS.md` with its rationale **before** it is run. Any yes found on a re-cut is
labelled **exploratory** and Phase 4 must reproduce it on the held-out last 25% before mainnet
is mentioned.

## Config writeback

Only parameters the data spoke to directly, only to values the data actually produced, never a
value chosen to make a table pass.

- `k_target`, `k_stop`, `time_stop_s`: from Q8's winning cell, only if its neighbours are
  positive.
- proxy threshold: from Q3's F1 curve.
- filters: kept or dropped by Q6's 2x rule.
- episode floors ($250k, 30 bps): fixed, not tuned.
- `min_edge_multiple`, `risk_frac` and every gate in `STRATEGY_AND_RISK.md` section 5:
  untouched by Phase 3. Research measures the edge; it does not set risk limits.

Each change carries the report path, the sample size and the old value in `docs/DECISIONS.md`.

## Build order

1. confirm `asset_ctx` covers the year (step 0)
2. this file
3. `episodes.py`
4. `grid.py` + `flow.py` + `intensity.py` + `zscore.py`
5. `_lib.py`, then Q1, then Q8
6. stop and read both reports; only if both pass:
7. `proxy.py` (three gates, plus the trades-only variant) + Q3, including the trigger branch
8. Q4 skeleton on the 12 Tardis days
9. Q5 + Q6
10. `levels.py` + Q7
11. `book.py` and `context.py`, honest NaN contracts on everything except funding
12. `SUMMARY.md` and the config writeback
