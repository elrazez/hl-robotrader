# Prompt 03: Phase 3 research, the go/no-go gate

Owner notes: this phase decides whether the project continues. Its deliverable is eight
written yes/no/inconclusive decisions with sample sizes (the spec's six, plus the
supply/demand zone question and the path/MAE question), not code that trades. Needs the
Phase 2 data on disk. Question 4 also wants a few weeks of recorder data; if the
recorder has not run that long, Q4 is answered "inconclusive" and that is fine. Expect
to run the scripts yourself and read the reports; the agent builds them.

Read "Decisions from the Phase 3 grill" at the end of the Decisions section before
anything else. It supersedes conflicting rules stated earlier in this file.

---

You are working in the liqfade repo. Read `CLAUDE.md` in full, then
`docs/STRATEGY_AND_RISK.md` sections 2 and 7 (the feature definitions and the six
research questions) and `docs/ROADMAP.md` Phase 3.

This session is **Phase 3: features and research**. Build `liqfade/features/` as pure
tested functions, an episode detector, and one runnable script per research question
that writes a markdown report. Do NOT build strategy/, sizing/, risk/, backtest/ or any
live code. Do NOT tune parameters to make results look good; the job is measurement.

## Ground rules that override everything

- **No lookahead, mechanically enforced.** Every feature takes arrays sorted by
  timestamp and an explicit decision time or grid; it may only use rows with
  `effective_ts < t` where `effective_ts = rx_ms if rx_ms > 0 else ts_ms`. Every
  feature gets a property test that mutates rows after `t` and asserts the output at
  `t` is bit-identical.
- **Report everything net of costs.** Every table shows three columns: gross, net-9bps
  (0.09% round trip, taker both ways) and net-stress (fees plus 1s entry delay plus an
  estimated spread per side, defined in the grill section). Every DECISION reads
  net-stress. A table that only shows gross is a bug.
- **Honest outcomes.** Each question ends with `DECISION: yes | no | inconclusive`,
  the episode count, the cluster count, and the effect size with a cluster-bootstrap
  confidence interval. If the data cannot answer the question, say inconclusive. Never
  soften a no.

## Decisions already made

- Features are sync functions on numpy arrays / polars frames in `liqfade/features/`,
  organized as: `grid.py` (build a 1s mid/price grid and a 250ms decision grid from
  trades/bbo; for historical S3-only windows where bbo is absent, mid = last trade
  price, and the docstring says so), `intensity.py` (liq_long/short_usd_w for w in
  config windows, venue-weighted aggregation, liq_imbalance_w), `zscore.py` (robust z
  of log1p on the 24h trailing distribution, median/MAD, computed on the 1 minute
  grid per docs), `proxy.py` (hl_forced_proxy per the spec: aggressive volume ratio
  gated by oi_delta < 0 and mark-mid divergence percentile), `flow.py` (ret_w, rv_1m,
  rv_5m, cvd_w and slope, disp_w vs the 5 minute pre-window VWAP anchor), `book.py`
  (spread_bps, depth within 10bps per side, book_imbalance; returns NaN columns when
  no l2 data exists for the window and callers must handle NaN), `context.py`
  (oi_delta_w, funding, premium, btc_ret_5m, btc_liq_z_60s, session hour bucket),
  `levels.py` (supply/demand zones, below).
  Every docstring states window, units, inputs, and NaN behavior.
- `levels.py`, the zone feature set. Built from 1m candles (the `candles_1m` table,
  derived from `trades` by `liqfade derive candles`; the API holds no 1m history), per
  coin, and usable at
  any decision time with only completed bars (a bar at time t is usable from its
  close, never mid-bar; the no-lookahead property test applies here too):
  - swing highs and lows: fractal points where a bar's high (low) exceeds the N bars
    on each side, N from config (`features.levels.fractal_n`, default 12).
  - untested levels: a swing level is "fresh" until price trades through it; track
    freshness and touch count.
  - range edges: rolling highest high and lowest low over `features.levels.range_d`
    days (default 5), plus prior UTC day and prior week high/low.
  - derived features at decision time: `dist_to_support_bps`, `dist_to_resistance_bps`
    (nearest level on each side, NaN when none within
    `features.levels.max_dist_bps`, default 200), the same distances normalized by
    rv_5m, the nearest level's freshness and touch count, and
    `extreme_in_zone` (bool: is the current cascade extreme within
    `features.levels.zone_width_bps`, default 15, of a level).
  Config keys go into models.py and default.yaml with unit and rationale comments
  like everything else. Keep the detector dumb and transparent: fractals and rolling
  extremes only. No ML, no volume profile in this phase; those are Phase 7
  candidates if Q7 says zones matter at all.
- Episode (cascade) detection for research lives in `liqfade/features/episodes.py`,
  NOT in strategy/. Definition: per coin per side, take exact liquidation events
  (from S3 data), join consecutive events with gaps under 60s into an episode, keep
  episodes with total notional >= the coin's trailing 30 day p95 of episode notional
  OR liq_z_60s >= 3.0 at any point, AND clearing both absolute floors in the grill
  section (notional >= $250k, anchor-to-extreme move >= 30 bps). Record: coin, side,
  start_ts, end_ts, anchor_px (mid at start), extreme_px, extreme_ts, total_notional,
  n_events, peak_liq_15s, notional_at_25pct_participation.
  Output one Parquet file `reports/research/episodes.parquet`. This detector is the
  ground truth the strategy's online detector is later measured against.
- Coin tiers for bucketing: tier by 30 day median daily notional volume: core =
  universe.core_coins, major = top 20 excluding core, minor = rest.
- Each research question is a script `scripts/research/q1_reversion.py` ...
  `q8_path.py`, runnable as `uv run python scripts/research/qN_*.py --start ...
  --end ...`, reading only the Parquet lake, writing
  `reports/research/qN_<slug>.md` plus PNGs beside it. Scripts are the canonical
  deliverable; notebooks are optional scratch and never required.
- Shared plumbing (arg parsing, lake loading, cluster-bootstrap CI helper, report
  writer) goes in `scripts/research/_lib.py`, imported only by the scripts, never by
  the package.
  matplotlib comes from the `research` dependency group; scripts may assume it.
- The eight questions, made concrete:
  - **Q1 reversion**: for each episode, mid at end_ts and at +1, +5, +15, +30, +60
    minutes; reversion fraction = (mid_t - extreme_px) / (anchor_px - extreme_px)
    signed so positive = reversion. Distributions by tier and by notional quartile,
    gross and net. DECISION bar: see the grill section (mean net-stress PnL >= 15 bps
    at the pre-registered +15 min horizon, cluster-bootstrap 95% CI lower bound above
    zero, on a qualifying sample). Also carries the capacity column and the notional
    histogram.
  - **Q2 exhaustion timing**: entry at exhaustion (the spec's conditions, evaluated on
    the research grid) vs fixed delays of 30/60/120s after episode start. Compare net
    reversion captured. DECISION yes if exhaustion beats every fixed delay.
  - **Q3 proxy calibration**: compute hl_forced_proxy on the grid, mark proxy
    triggers, match against exact episodes within a 120s window. Precision, recall,
    F1, per tier, at proxy thresholds 2/3/4/6. For the historical year the proxy is
    trades-only (no OI gate, no mark-mid gate); see the grill section for the
    gate-lift check and the trigger branch this decides. Writes
    `reports/proxy_calibration.md` (this exact path, other docs reference it).
    DECISION yes if any threshold reaches precision >= 0.6 AND recall >= 0.4 on
    core+major tiers.
  - **Q4 cross-venue**: Bybit episode without a matching HL episode within 120s ->
    does HL still revert per Q1's measure? First cut runs on the 12 Tardis days that
    already hold Binance/Bybit/OKX liquidations alongside exact HL ones; rerun on
    2026-09-13 when the recorder crosses 14 days. Whether Q4 blocks Phase 4 is decided
    by Q3's trigger branch in the grill section.
  - **Q5 partial liquidation rhythm**: every qualifying episode (the $250k floor
    already clears the 100k partial-liquidation threshold the spec named):
    histogram of inter-event gaps; is there a mass near 30s (the documented chunk
    cooldown)? DECISION yes if the 25-35s bin exceeds 2x the neighboring bins.
  - **Q6 filters**: for each candidate filter from the spec (spread cap, BTC
    cascading, funding extreme, listing age, session bucket), split Q1's episode
    outcomes by filter pass/fail. The loss tail is Q8's stopped-out episodes, not the
    worst decile of terminal reversion. DECISION per filter: keep if it removes at
    least 2x more loss-tail episodes than it removes sample overall.
  - **Q7 zone confluence** (owner addition, `scripts/research/q7_zones.py`): split
    Q1's episodes by whether the cascade extreme landed in a pre-existing
    supply/demand zone (`extreme_in_zone` from `levels.py`, using only levels that
    existed before the cascade started). Compare net reversion, hit rate and the
    loss tail for in-zone vs out-of-zone episodes, by tier, at zone widths 10, 15
    and 25 bps. Also test the reverse use: does distance-to-zone at cascade start
    predict how far the cascade extends (a stop-placement input)? DECISION yes if
    in-zone episodes beat out-of-zone net-stress reversion with the cluster-bootstrap
    95% CIs separated, on at least 100 in-zone episodes. A yes wires the zone rule
    into Phase 4 as an optional confluence gate; a no leaves it built but off.
  - **Q8 path and MAE** (`scripts/research/q8_path.py`), the second gate: maximum
    adverse excursion from entry, and the stop/target grid. Full definition and
    DECISION bar in the grill section. Runs immediately after Q1.
- Final deliverable `reports/research/SUMMARY.md`: one table, eight rows, DECISION and
  key number each, plus the capacity-weighted edge in dollars per episode and
  annualised, the trigger branch Q3 selected, and the PREREGISTRATION.md commit hash,
  plus a paragraph saying whether Phase 4 should proceed and with which parameter
  values. If the answer is stop, say stop.
- Parameter writeback: where results support different defaults than
  `config/default.yaml` (thresholds, windows, k_target, filters), update the YAML
  values AND their rationale comments, list every change in `docs/DECISIONS.md` with
  the evidence, and do not change anything the data did not speak to. Tightened by the
  grill section below.

### Decisions from the Phase 3 grill (2026-09-04)

These supersede any conflicting rule above. Where a bullet above states a DECISION bar,
the bar here wins.

**Step 0, before anything else: backfill funding.** The Info API's `fundingHistory`
returns hourly funding per coin across the whole lake window, free, one call per coin.
Write it into `asset_ctx`-compatible rows (`source = hl_api`, `rx_ms = 0`) so
`context.py`'s `funding` is real for the year and Q6's funding filter is evaluable.
This is data preparation and may precede the preregistration commit.

`asset_ctx` is otherwise a recorder-only table starting 2026-08-30, so for the
historical year `open_interest`, `mark_px`, `oracle_px` and `premium` are absent, and
Coinalyze cannot fill them (its intraday history keeps about 2,000 points, useless at
scalp resolution). `book.py` is NaN for the year as well: there is no historical `bbo`
and `l2` is hourly snapshots on four coins. The live bot is unaffected, since
`activeAssetCtx` carries OI, mark and premium at every block. What is missing is a year
of history to calibrate two of the proxy's three gates, not the gates themselves.

**Cost model. Every table reports three columns: gross, net-9bps, net-stress. Every
DECISION reads net-stress and nothing else.**

- net-9bps = 0.09% round trip (0.045% taker per side).
- net-stress = 9 bps fees, entry at the first print after `extreme_ts + 1s`, and one
  estimated effective spread subtracted per side.
- Effective spread is estimated from trades, per episode, at episode time: the mean
  absolute price change between consecutive prints whose aggressor side flips, over the
  60s after the extreme. It exists for every coin and every episode and has no
  lookahead. The 12 Tardis `tardis_book_snapshot_5` days and the hourly `l2` snapshots
  on the core coins are a sanity check on the estimator, never its source. The
  recorder's live `bbo` replaces it once a few weeks exist.
- Funding is ignored at these holds. Every report says so.
- `mid = last trade price` for the historical year is accepted, on the condition that
  the spread haircut above is applied.

**Q1's bar, replacing the one stated above.** Mean net-stress PnL per episode >= 15 bps
at a horizon pre-registered as **+15 minutes**, with the cluster-bootstrap 95% CI lower
bound above 0. **+5 minutes** is the only permitted secondary horizon and needs a 99%
CI because it is a second look. The other three horizons are descriptive and cannot
produce a yes on their own. Median is reported beside the mean as a concentration
check, never as the criterion. A result between 15 and 24 bps is written "marginal, Q2
must supply the rest", not yes. 24 bps is the strategy spec's own `min_edge_multiple`
floor and nothing below it will ever trade.

**Sample size, and what an observation is.** A **cluster** is every episode on the same
side whose start time falls within a 5-minute window, across all coins. A qualifying
sample is **>= 200 episodes AND >= 40 clusters AND >= 8 distinct calendar months
containing a cluster**. All confidence intervals come from a **cluster bootstrap**
(resample whole clusters); the per-episode bootstrap is banned from every report,
because a market-wide sweep liquidates hundreds of coins inside one minute and they are
one observation, not hundreds. Every table carries a mandatory
**top-5-clusters-removed** rerun of its own headline number, not just the summary
table.

**Episode floors, added to the detector.** On top of the relative rules already
specified, an episode must have **total notional >= $250,000** and
**`abs(anchor_px - extreme_px) / anchor_px >= 30 bps`** (roughly 2x net-stress cost;
below it the trade cannot pay even at 100% reversion). The relative p95 and z rules do
the per-coin tiering; these floors only kill dead-tape noise, since a relative rule
emits a fixed rate of episodes no matter how quiet the market is. Both go in config
with unit and rationale comments. `episodes.py` writes both the filtered and unfiltered
counts, and the Q1 report carries a histogram of episodes by notional bucket so
capacity is visible before any reversion number is read. The floors are **not** tunable
by this phase.

**Q8, the new question and the second gate: `scripts/research/q8_path.py`.** Nothing in
Q1 to Q7 measures the path, so nothing measures the loss: terminal reversion cannot
tell you whether a stop is hit before a target, and a strategy with +20 bps mean
reversion and a 60% stop-out rate loses money. Built from **trades, not 1m bars**, or
MAE is meaningless. For every episode, the distribution of **maximum adverse excursion
from an entry at the first print after `extreme_ts + 1s`**, in bps and in units of
`rv_5m`; then the joint outcome over a grid of `k_stop` in {1.0, 1.5, 2.5, infinity} x
`k_target` in {0.3, 0.5, 0.8} with a 15-minute time stop, net-stress, reported as a
hit/stop/timeout breakdown per cell. **DECISION yes only if some cell beats
hold-to-horizon, has a stop-out rate under 50%, and its neighbours in the grid are also
positive.** An isolated positive cell is the multiple-comparison problem wearing a hat.
Q8 runs immediately after Q1 because it is the question most likely to kill the
project.

**Capacity, folded into Q1 rather than made a question.** Each episode carries
`notional_at_25pct_participation`, computed from the trades in the 60s after the
extreme. `SUMMARY.md` reports the capacity-weighted edge in **dollars per episode** and
annualised dollars at the sample's own episode rate.

**Robustness, pre-registered, all three mandatory, judged on point estimates.** With
roughly 40 clusters a per-slice CI means nothing; only the pooled number carries one.

1. **Time:** split the lake into 4 quarters. The headline must be positive in **>= 3 of
   4**, and the pooled CI must survive dropping the single best quarter.
2. **Coin:** the headline must be positive **excluding BTC and ETH**, and again
   **excluding HYPE**. A result that needs one coin is that coin's idiosyncrasy.
3. **Regime:** bucket clusters by whether BTC was itself cascading
   (`btc_liq_z_60s >= 3`). **The isolated-coin bucket must pass on its own.**
   Market-wide sweeps are reported for the risk model, not for the yes, since the
   strategy spec already defaults to skipping them.

Anything that passes pooled and fails a split is written **"conditional yes, gated on
X"**, and the gate becomes Phase 4 config. Never a plain yes.

**Q3, restated.** The headline is a **trades-only proxy** over the full year:
aggressive volume ratio versus its trailing median, with no OI gate and no mark-mid
gate, because neither exists historically. Precision, recall and F1 by tier at
thresholds 2/3/4/6, cluster-aware, against exact S3 liquidations. The bar is unchanged:
precision >= 0.6 AND recall >= 0.4 on core+major. Separately, a **gate-lift check** on
the 12 Tardis `tardis_derivative_ticker` days asks whether adding the two gates raises
precision; reported with an explicit "12 days, not a sample size" caveat and **no
DECISION of its own**, and rerun at 60 recorder days. `reports/proxy_calibration.md`
keeps its path.

**The trigger branch, decided by Q3 and written into its report.** If the trades-only
proxy reaches precision >= 0.6 and recall >= 0.4 on core+major, the v0 live trigger is
HL-native and Q4 stays inconclusive and non-blocking, revisited in Phase 7. If it does
not, **the v0 trigger is cross-venue (Bybit is the only complete feed) and Q4 becomes a
Phase 4 entry requirement** at a reduced bar of **>= 15 clusters and >= 60 recorder
days**, reduced because Q4 is then asking whether the cross-venue trigger fires on
HL-reverting events, not establishing the edge, which Q1 already did on exact data.
`SUMMARY.md` states which trigger Phase 4 is being built on.

**Q4 is not empty today.** Tardis carries Binance, Bybit and OKX liquidations on the
1st of every month of the lake window, and we hold exact HL liquidations for those same
12 days. Run Q4's skeleton on those 12 days as the first cut on the day it is built,
and again on **2026-09-13** when the recorder crosses 14 days. Twelve days cannot reach
any bar, but a cross-venue cascade that never once coincides with an HL episode is
worth knowing before Q3 has run.

**Stop rules. The project stops on any one of these, evaluated on the pre-registered
run only.**

1. **No edge.** Q1 net-stress mean at +15 min is <= 0 bps on a qualifying sample.
2. **Unharvestable edge.** Q1 passes, but no Q8 cell has positive net-stress with a
   stop-out rate under 50% and positive neighbours.
3. **Lottery edge.** The headline dies with the top 5 clusters removed, or fails 2 or
   more of the 3 robustness splits.
4. **No capacity.** Capacity-weighted expected net dollars, at the sample's own episode
   rate and 25% participation, is under **$2,000/year** (roughly 5x infra plus margin).
   A clean 25 bps edge you can put $800 into is not a business.
5. **No trigger.** Q1 and Q8 pass on exact liquidations, but the trades-only proxy is
   under precision 0.5 and Q4 cannot be answered for months. Park the project until the
   recorder has the data; do not build Phase 4 blind.

**Pivot, not stop.** If Q1 fails but continuation is strongly tradable, that is the
momentum variant, out of v0 scope. Write it up as a new thesis needing its own Phase 3
and close this one. Do not flip the sign and carry on.

**Anti-tinkering.** After the pre-registered run, **at most two re-cuts of the episode
definition**, each logged in `docs/DECISIONS.md` with its rationale **before** it is
run. Any yes found on a re-cut is labelled **exploratory** and Phase 4 must reproduce it
on the held-out last 25% before mainnet is mentioned.

**Preregistration.** `reports/research/PREREGISTRATION.md` is written verbatim from this
"Decisions already made" section and committed as the **first commit of the phase**,
before any script touches the real lake. The funding backfill is data preparation and
may precede it. `SUMMARY.md` cites its commit hash. Any later change is a dated
amendment in `docs/DECISIONS.md` stating what had already been seen when the change was
made.

**Config writeback, tightened.** Only parameters the data spoke to directly, only to
values the data actually produced, never a value chosen to make a table pass.

- `k_target`, `k_stop`, `time_stop_s`: from Q8's winning cell, **only if its neighbours
  are positive**.
- proxy threshold: from Q3's F1 curve.
- filters: kept or dropped by Q6's 2x rule.
- episode floors ($250k, 30 bps): fixed, not tuned.
- `min_edge_multiple`, `risk_frac` and every gate in `STRATEGY_AND_RISK.md` section 5:
  **untouched by Phase 3.** Research measures the edge; it does not set risk limits.

Each change carries the report path, the sample size and the old value in
`docs/DECISIONS.md`.

**Build order, so a no costs three days instead of ten.** `book.py` and `context.py`
(bar funding) produce NaN for every historical episode, and `levels.py` is the phase's
largest build serving its least load-bearing question, so none of them precede the
gate. This replaces the commit order under "How to work".

1. funding backfill (step 0)
2. `reports/research/PREREGISTRATION.md`
3. `episodes.py`
4. `grid.py` + `flow.py` + `intensity.py` + `zscore.py`
5. `_lib.py`, then **Q1**, then **Q8**
6. **Stop and read both reports.** Only if both pass:
7. `proxy.py` (trades-only) + Q3, including the trigger branch decision
8. Q4 skeleton on the 12 Tardis days
9. Q5 + Q6
10. `levels.py` + Q7
11. `book.py` and `context.py`, honest NaN contracts on everything except funding
12. `SUMMARY.md` and the config writeback

`make lint test` green before each commit, as before. The definition of done is
unchanged; only the order is.

## Deliverables

1. `liqfade/features/` complete as above, mypy-clean (add `liqfade.features.*` to the
   strict list in pyproject), every public function with the no-lookahead property
   test plus invariant tests (intensities >= 0, z finite where defined, imbalance in
   [-1, 1], proxy >= 0, rv >= 0).
2. `liqfade/features/episodes.py` plus tests on synthetic liquidation streams
   (constructed episodes with known answers: gap joining, notional threshold, extreme
   tracking).
3. `scripts/research/_lib.py` and the eight question scripts.
4. Fixture-based smoke test: each script runs end-to-end against
   `tests/fixtures/parquet/` (tiny synthetic lake committed in this phase if the
   Phase 1 fixture lacks liquidations: build one through `data/schemas.py` with ~50
   synthetic episodes) and produces a parseable report. This keeps the scripts from
   rotting; it does not validate the science.
5. `reports/research/SUMMARY.md` template with the table pre-built and every cell
   reading "RUN PENDING" so the owner sees exactly what will be filled in.
6. `reports/research/PREREGISTRATION.md`, committed first, before any script touches
   the real lake.
7. The `fundingHistory` backfill (step 0), with its own test.

## How to work

- Print a short plan and wait for OK.
- Commit order: the twelve steps in the grill section's build order. `make lint test`
  green before each.
- Performance: everything vectorized with polars/numpy on the 1s grid. A year of one
  coin's features must compute in under a minute; if a function needs a Python loop
  over rows, redesign it.
- You likely cannot run the real scripts in-session (the lake lives on the owner's
  disk). Run the fixture smoke tests, and end with the exact commands the owner runs
  and in what order, plus how long each should take.
- No em dashes anywhere. No new dependencies.

## Definition of done

`make lint test` green, zero skips, features in the mypy strict list. All eight scripts
run against the fixture lake and produce reports. Owner-run acceptance: the eight real
reports plus SUMMARY.md exist with real DECISIONs; `PREREGISTRATION.md` was committed
before the first run and SUMMARY.md cites its hash; `reports/proxy_calibration.md`
exists; config writebacks match the evidence and the writeback rules. The phase's real
exit condition is a human one: the owner reads SUMMARY.md and decides continue or
stop.
