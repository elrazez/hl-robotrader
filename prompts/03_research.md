# Prompt 03: Phase 3 research, the go/no-go gate

Owner notes: this phase decides whether the project continues. Its deliverable is seven
written yes/no/inconclusive decisions with sample sizes (the spec's six plus the
supply/demand zone question), not code that trades. Needs the
Phase 2 data on disk. Question 4 also wants a few weeks of recorder data; if the
recorder has not run that long, Q4 is answered "inconclusive" and that is fine. Expect
to run the scripts yourself and read the reports; the agent builds them.

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
- **Report everything net of costs.** The round trip cost constant for research is
  0.09% (taker both ways) and results are shown gross AND net. A table that only shows
  gross is a bug.
- **Honest outcomes.** Each question ends with `DECISION: yes | no | inconclusive`,
  the sample size, and the effect size with a bootstrap confidence interval. If the
  data cannot answer the question, say inconclusive. Never soften a no.

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
- `levels.py`, the zone feature set. Built from 1m candles, per coin, and usable at
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
  OR liq_z_60s >= 3.0 at any point. Record: coin, side, start_ts, end_ts, anchor_px
  (mid at start), extreme_px, extreme_ts, total_notional, n_events, peak_liq_15s.
  Output one Parquet file `reports/research/episodes.parquet`. This detector is the
  ground truth the strategy's online detector is later measured against.
- Coin tiers for bucketing: tier by 30 day median daily notional volume: core =
  universe.core_coins, major = top 20 excluding core, minor = rest.
- Each research question is a script `scripts/research/q1_reversion.py` ...
  `q7_zones.py`, runnable as `uv run python scripts/research/qN_*.py --start ...
  --end ...`, reading only the Parquet lake, writing
  `reports/research/qN_<slug>.md` plus PNGs beside it. Scripts are the canonical
  deliverable; notebooks are optional scratch and never required.
- Shared plumbing (arg parsing, lake loading, bootstrap CI helper, report writer) goes
  in `scripts/research/_lib.py`, imported only by the scripts, never by the package.
  matplotlib comes from the `research` dependency group; scripts may assume it.
- The six questions, made concrete:
  - **Q1 reversion**: for each episode, mid at end_ts and at +1, +5, +15, +30, +60
    minutes; reversion fraction = (mid_t - extreme_px) / (anchor_px - extreme_px)
    signed so positive = reversion. Distributions by tier and by notional quartile,
    gross and net of 0.09%. DECISION yes if median net reversion at any horizon is
    positive with the 95% bootstrap CI excluding zero on at least 200 episodes.
  - **Q2 exhaustion timing**: entry at exhaustion (the spec's conditions, evaluated on
    the research grid) vs fixed delays of 30/60/120s after episode start. Compare net
    reversion captured. DECISION yes if exhaustion beats every fixed delay.
  - **Q3 proxy calibration**: compute hl_forced_proxy on the grid, mark proxy
    triggers, match against exact episodes within a 120s window. Precision, recall,
    F1, per tier, at proxy thresholds 2/3/4/6. Writes
    `reports/proxy_calibration.md` (this exact path, other docs reference it).
    DECISION yes if any threshold reaches precision >= 0.6 AND recall >= 0.4 on
    core+major tiers.
  - **Q4 cross-venue**: needs recorded Bybit liquidations. If under 14 days of
    recorder data exist, write the report skeleton with DECISION inconclusive and the
    date when enough data will exist. Else: Bybit episode without a matching HL
    episode within 120s -> does HL still revert per Q1's measure?
  - **Q5 partial liquidation rhythm**: episodes with total notional > 100k USDC:
    histogram of inter-event gaps; is there a mass near 30s (the documented chunk
    cooldown)? DECISION yes if the 25-35s bin exceeds 2x the neighboring bins.
  - **Q6 filters**: for each candidate filter from the spec (spread cap, BTC
    cascading, funding extreme, listing age, session bucket), split Q1's episode
    outcomes by filter pass/fail. DECISION per filter: keep if it removes at least
    2x more loss-tail (worst decile) episodes than it removes sample overall.
  - **Q7 zone confluence** (owner addition, `scripts/research/q7_zones.py`): split
    Q1's episodes by whether the cascade extreme landed in a pre-existing
    supply/demand zone (`extreme_in_zone` from `levels.py`, using only levels that
    existed before the cascade started). Compare net reversion, hit rate and the
    loss tail for in-zone vs out-of-zone episodes, by tier, at zone widths 10, 15
    and 25 bps. Also test the reverse use: does distance-to-zone at cascade start
    predict how far the cascade extends (a stop-placement input)? DECISION yes if
    in-zone episodes beat out-of-zone net reversion with the 95% CI separated on at
    least 100 in-zone episodes. A yes wires the zone rule into Phase 4 as an
    optional confluence gate; a no leaves it built but off.
- Final deliverable `reports/research/SUMMARY.md`: one table, seven rows, DECISION and
  key number each, plus a paragraph saying whether Phase 4 should proceed and with
  which parameter values. If the answer is stop, say stop.
- Parameter writeback: where results support different defaults than
  `config/default.yaml` (thresholds, windows, k_target, filters), update the YAML
  values AND their rationale comments, list every change in `docs/DECISIONS.md` with
  the evidence, and do not change anything the data did not speak to.

## Deliverables

1. `liqfade/features/` complete as above, mypy-clean (add `liqfade.features.*` to the
   strict list in pyproject), every public function with the no-lookahead property
   test plus invariant tests (intensities >= 0, z finite where defined, imbalance in
   [-1, 1], proxy >= 0, rv >= 0).
2. `liqfade/features/episodes.py` plus tests on synthetic liquidation streams
   (constructed episodes with known answers: gap joining, notional threshold, extreme
   tracking).
3. `scripts/research/_lib.py` and the seven question scripts.
4. Fixture-based smoke test: each script runs end-to-end against
   `tests/fixtures/parquet/` (tiny synthetic lake committed in this phase if the
   Phase 1 fixture lacks liquidations: build one through `data/schemas.py` with ~50
   synthetic episodes) and produces a parseable report. This keeps the scripts from
   rotting; it does not validate the science.
5. `reports/research/SUMMARY.md` template with the table pre-built and every cell
   reading "RUN PENDING" so the owner sees exactly what will be filled in.

## How to work

- Print a short plan and wait for OK.
- Commit order: grid+flow+book+context; levels; intensity+zscore+proxy; episodes;
  _lib + q1+q3 (the two that gate everything); q2+q5+q6+q7; q4; summary template +
  config writeback placeholder. `make lint test` green before each.
- Performance: everything vectorized with polars/numpy on the 1s grid. A year of one
  coin's features must compute in under a minute; if a function needs a Python loop
  over rows, redesign it.
- You likely cannot run the real scripts in-session (the lake lives on the owner's
  disk). Run the fixture smoke tests, and end with the exact commands the owner runs
  and in what order, plus how long each should take.
- No em dashes anywhere. No new dependencies.

## Definition of done

`make lint test` green, zero skips, features in the mypy strict list. All seven scripts
run against the fixture lake and produce reports. Owner-run acceptance: the seven real
reports plus SUMMARY.md exist with real DECISIONs; `reports/proxy_calibration.md`
exists; config writebacks match the evidence. The phase's real exit condition is a
human one: the owner reads SUMMARY.md and decides continue or stop.
