# Prompts 05e: the projected liquidation map

Owner notes: two prompts in one file, each pasted into its own fresh chat. They continue the
letter sequence from `prompts/05d_external_ideas.md`, which holds A through E, because this is
the same lineage: A recorded Hyperliquid's *real* public positions, D (Q9) grades them. F and G
here add the *projected* map, the one you can compute from candles alone and therefore
backtest over the year we already hold.

**Where the idea comes from.** A public TradingView indicator, "Tomo's Flat Fibs", draws lines
at fixed dollar offsets below the last pivot high and above the last pivot low: 600, 1000,
1400, 1900, 2700, 3400, 4000, 4800. Those numbers are hardcoded for Bitcoin at roughly $40,000,
where the indicator was written in April 2022. Converted to percentages and read through the
liquidation formula `L = 1 / (d + m)` at a maintenance margin `m` of 0.5%, they are this:

| offset | distance | implied leverage |
|---|---|---|
| 600 | 1.50% | 50x |
| 1000 | 2.50% | 33x |
| 1400 | 3.50% | 25x |
| 1900 | 4.75% | 19x |
| 2700 | 6.75% | 14x |
| 3400 | 8.50% | 11x |
| 4000 | 10.0% | 9.5x |
| 4800 | 12.0% | 8x |

That is the exchange leverage menu. The indicator is not drawing Fibonacci levels, it is drawing
the liquidation ladder of everyone who entered at the swing extreme, and it is doing it in
dollars, which is why it only works on one coin at one price era. Stored as percentages it
works on any coin at any price. **That is the whole feature, and Q10 is whether it measures
anything.**

`features/levels.py` already supplies the anchors: fractal swing highs and lows, each stamped
with the millisecond it became knowable, plus a touch count so "untouched" is a field we
already compute. Nothing in the ladder needs new data.

| Prompt | Builds | Branch | Chat | Runs | Gate |
|---|---|---|---|---|---|
| F | Address coverage: measure the real info-weight budget, raise the poll set, add a weight metric | `07d-liqmap-coverage` | NEW | **first, and immediately** | none |
| G | Q10, the preregistered research question on the projected ladder | `07e-q10-ladder` | NEW | concurrently with F | none; runs on the year already in the lake |

**F resets Q9's clock and that is deliberate.** Prompt D in `05d_external_ideas.md` is gated on
21 complete days of `hl_positions`, counted from 2026-09-08. F changes how many addresses each
poll covers, which changes the instrument mid-experiment, so the 21 days must be counted from
F's deploy instead. Losing the days already recorded to roughly double the coverage for the
rest is the right trade, and F is instructed to move the date in both places. Do not paste D
until F has been deployed and the new clock has run.

Each prompt's first line is its routing line. Each chat sends its final report to the control
chat `hl-robotrader-67` with `SendMessage`; if that session is gone, ask the owner for the
current name rather than guessing from `ListAgents`. The control chat merges. Paste only the
part below each `---`.

---

## Prompt F: pay the address budget to where price actually is

ROUTING: NEW chat. Start now, concurrently with G. Branch `07d-liqmap-coverage` off `main`.
When done, send the final report to the control chat `hl-robotrader-67` with `SendMessage`.

/mattpocock-skills:tdd

You are working in the liqfade repo. Read `CLAUDE.md` in full, then `docs/HYPERLIQUID.md`
(the rate limits section and the `clearinghouseState` section), `docs/DATA_SOURCES.md`,
`docs/research/external_bots_scan.md` section 1, and these files:
`liqfade/recorder/streams/hl_positions.py`, `liqfade/recorder/addresses.py`,
`liqfade/recorder/streams/coinalyze.py` (the other REST poller, the shape to match),
`liqfade/config/models.py` (`HlPositions`), and the `positions:` block in
`config/default.yaml`.

This session widens the liquidation map's coverage. Recorder work only. Nothing in
`features/`, `strategy/`, `sizing/`, `risk/` or `execution/` is touched.

Print a short plan and wait for an OK before writing anything.

### The measurement that motivates this

Taken from the VPS on 2026-09-09 over the most recent hourly partition of `hl_positions`,
deduplicated to the latest row per address and coin, joined to `asset_ctx` marks:

| | |
|---|---|
| addresses harvested into `hl_addresses.json` | 5,000 |
| addresses actually polled per cycle | 200 |
| open positions seen | 873 across 18 coins |
| notional tracked | $1,561M |
| positions with no `liquidation_px` at all | 271 of 873 |
| notional within 2% of mark | $54M |
| notional within 5% of mark | $143M |
| notional within 20% of mark | $413M |
| share of notional in cross margin | $1,529M of $1,561M |

Two facts follow. Coverage is the risk the scan doc named first and we are polling 4% of the
addresses we have. And ranking purely by cumulative traded notional spends most of the budget
on positions that are nowhere near their liquidation price, which are exactly the positions
that cannot become cascade fuel this hour.

### Non-negotiables restated for this session

- The rate limit is **per IP, 1200 weight per minute**, and the VPS runs the recorder, the paper
  loop and the testnet loop behind one address. From `docs/HYPERLIQUID.md`: `l2Book`, `allMids`,
  `clearinghouseState` and `orderStatus` weigh 2, `userRole` weighs 60, everything else weighs
  20. **Work out the real shared budget yourself and print it**, because two things this prompt
  will not assert are whether the testnet loop counts against the same bucket as mainnet, given
  it talks to a different host, and which of the reconciler's three calls the paper loop
  actually makes. Enumerate the processes, name each mainnet `/info` call and its interval,
  total it, and leave room for a future mainnet process.
- Round trip to `api.hyperliquid.xyz` from the VPS measured at 52 to 63 ms on 2026-09-09, so a
  sequential sweep of 600 addresses takes about 33s and the clock is not the binding
  constraint. The weight budget is.
- The poller does not gather. Keep it that way: bursting a minute of budget into one instant is
  how a shared IP gets throttled and takes the trading loop down with it.
- Config reload does not reach this poller. A restart applies a change here, and the deploy
  step must say so.
- Do not restart the paper or testnet loops to deploy this. The recorder is its own unit.

### Decisions already made (follow these; do not re-ask)

1. **Two tiers, one poller.** Every cycle polls a *priority* set and a slice of a *sweep*.
   - Priority: addresses whose most recent snapshot held any position with a non-null
     `liquidation_px` within `priority_pct` of that coin's last mark, ranked by
     `position_value_usd`. These are the ones whose liquidation price can be reached soon.
   - Sweep: a rotating cursor over the whole address book, so every harvested address is
     re-checked on a fixed period and can be promoted into the priority set. The sweep is what
     discovers positions the priority rule cannot know about yet.
   - Both counts are config. The sum is what the weight budget buys.
2. **Sizing is measured, not asserted.** Add a `weight_per_min` gauge to the poller's
   `StreamStats` and log it each cycle. Then set the config from the measurement, not from this
   prompt's arithmetic. The one anchor: 400 addresses is 800 weight of the 1200, up from the
   current 200 addresses at 400. Whether 400 is the right number depends on the budget you
   measure above, and 300 with headroom beats 500 that throttles the trading loop.
3. **Priority state lives in memory and is rebuilt from the last poll.** No new table, no new
   file. On restart the poller has no priority set and fills the first cycles from the sweep,
   which is correct and self-healing.
4. **`priority_pct` default 25.** Wide, because a position 20% away in a quiet hour is 5% away
   after a real move, and the sweep period is the safety net for everything beyond it.
5. **The schema does not change.** `HL_POSITIONS` stays exactly as it is. This prompt changes
   which addresses get asked, not what is written down.
6. **No new dependency.** Sorting a list is not a library.

### The seams (agreed here; do not re-ask)

Write the failing test first in every case.

1. `hl_positions.select_addresses(book, last_snapshot, marks, cfg) -> list[str]`, pure, no I/O,
   no clock. Given an address book, the previous cycle's rows, a mapping of coin to mark, and
   the config, it returns the addresses to poll this cycle in order. Tests: an address with a
   near-price liquidation level outranks a larger address with none; the sweep cursor advances
   and wraps; an address already in the priority set is not polled twice in one cycle; an empty
   last snapshot returns a pure sweep; a null `liquidation_px` never earns priority.
2. `StreamStats` gains `weight_per_min`, and `test_recorder_health` (or its nearest equivalent)
   asserts the gauge equals two times the addresses actually polled in the last minute.
3. Config: `HlPositions` gains `priority_addresses`, `sweep_addresses` and `priority_pct`,
   each with a unit and a rationale comment in `config/default.yaml`. `max_addresses` stays as
   the hard ceiling on the sum and the model validates that the two new counts do not exceed
   it. `tests/test_config.py` covers the refusal.

### Slices, in order

1. The selection function and its tests, pure, no poller changes.
2. Wire it into `HlPositionsPoller`, keeping the sequential ask.
3. The weight gauge and the health assertion.
4. Config, models, defaults, refusal test.
5. `make test` and `make lint` clean.

### Deploy and verify

1. Deploy to the VPS and restart the recorder unit only. Confirm from the logs that the first
   cycle completes inside `poll_s` and print the measured cycle duration and `weight_per_min`.
2. After one hour, re-run the measurement table at the top of this prompt against the new
   partition and report the same rows. The number that matters is **notional within 2% and 5%
   of mark**, which is the fuel the map can actually see.
3. If `weight_per_min` plus the live loops exceeds 900, reduce the counts and say so. Never
   trade recorder coverage against the trading loop's ability to reconcile.

### Deliverables

The branch, the tests, a dated `docs/DECISIONS.md` entry giving the before and after coverage
numbers and the weight budget you measured, a line in `docs/HYPERLIQUID.md` recording the
measured round trip and the per-IP budget split across processes, and **the Q9 clock move**:
update the gate wording for prompt D in `prompts/05d_external_ideas.md` and the Phase 7 line in
`docs/ROADMAP.md` to count 21 days from your deploy date rather than 2026-09-08.

---

## Prompt G: Q10, does the projected ladder measure anything

ROUTING: NEW chat. Start now, concurrently with F. Branch `07e-q10-ladder` off `main`. Runs on
the local machine over the synced lake; the VPS is not involved. When done, send the final
report, the DECISION lines verbatim, to the control chat `hl-robotrader-67` with `SendMessage`.

/mattpocock-skills:tdd

You are working in the liqfade repo. Read `CLAUDE.md` in full, then
`reports/research/PREREGISTRATION.md` end to end (ground rules, the DECISION format, the stop
rules, the anti-tinkering section), `reports/research/SUMMARY.md`,
**`reports/research/q7_zones.md` in full because it is the result this question has to beat**,
`reports/research/q2_exhaustion.md`, `reports/research/q8_path.md`,
`docs/research/external_bots_scan.md` section 1, `docs/STRATEGY_AND_RISK.md` section 7, and
these files: `liqfade/features/levels.py`, `scripts/research/_lib.py` (`build_episodes`,
`attach_outcomes`, `cluster_bootstrap`, `write_report`, `decision`, `scan_day`),
`scripts/research/q7_zones.py`, `scripts/research/q2_exhaustion.py`,
`tests/fixtures/research_lake.py`, `tests/test_research_scripts.py`.

This session answers **Q10, preregistered: does a ladder of projected liquidation prices,
anchored on untouched swing extremes, mark where price reacts, where cascades end, and when
forced flow is spent?** Research plus one pure feature module. Nothing enters `strategy/`.

Print a short plan and wait for an OK before writing anything.

### The claim, stated so it can fail

A trader who opened at a recent swing extreme with leverage `L` is liquidated a fixed fraction
away, `d = 1/L - m`. So every untouched swing high projects a ladder of prices below it where
trapped longs are force-sold, and every untouched swing low projects a ladder above it where
trapped shorts are force-bought. Where ladders from several different anchors land on the same
price, that price is where forced flow concentrates.

Read the header of `prompts/05e_projected_liqmap.md` for where this comes from and why the
public indicator that inspired it is hardcoded in dollars.

**Q7 already tested a neighbouring claim and it failed.** Cascades ending within 15 bps of a
raw swing level reverted +36.1 bps net-stress against +146.3 for cascades ending nowhere near
one, over 2,722 episodes. That is the opposite of the expected sign. Q10 is a different
mechanism, the level seeds a projected band rather than acting as the target itself, but it
inherits the burden of proof and the preregistration must say so in those words. This is **not**
a re-cut of the episode definition and does not consume either of the two the preregistration
allows; the episodes are Q7's episodes, unchanged.

### Data precondition

The year already in the lake: `candles_1m` and `liquidations` with `source = hl_s3` for
2025-09-01 to 2026-08-30, plus `asset_ctx` for open interest. No new download, no S3 spend, no
waiting. Confirm the partitions exist and print the day count before writing the script. If
`hl_positions` days are also synced they are used for Q10d only, which is descriptive.

Local machine constraints, same as every research prompt: 8 GB, the lake is on USB, every scan
goes one day at a time through `_lib.scan_day`, peak RSS under 2 GB, `--data-dir` explicit,
`pgrep -f liqfade` before launching, one job at a time.

### The feature module: `liqfade/features/liqmap.py`

Pure functions on polars frames. No I/O, no clock, no live-only branch. Docstrings state units
and edge cases. It must run identically in a backtest replay and, later, in the live loop.

1. `ladder_bands(levels, *, ladder, mm_frac, max_dist_bps) -> pl.DataFrame`
   Takes rows from `features.levels.swing_levels` and `range_levels` and projects one band per
   anchor per rung. A `resistance` anchor traps longs, so its bands sit **below** at
   `px * (1 - (1/L - mm_frac))` with `side = "long_liq"`, meaning forced selling. A `support`
   anchor traps shorts, so its bands sit **above** at `px * (1 + (1/L - mm_frac))` with
   `side = "short_liq"`. Every band carries its anchor's `known_from` unchanged, plus
   `anchor_px`, `leverage` and `source`. Bands beyond `max_dist_bps` from the anchor are
   dropped.

2. `band_weights(bands, oi, *, histogram, half_life_min) -> pl.DataFrame`
   One notional weight per band. `histogram` maps each rung to a share of notional.
   `oi` supplies the anchor notional: the increase in `asset_ctx.open_interest` over the window
   the anchor formed in, which is the money that opened while price was at the extreme. Weight
   decays by `0.5 ** (age_min / half_life_min)`, because positions opened at a swing do not
   live forever.

3. `map_features(bands, queries, *, band_width_bps, fuel_window_bps) -> pl.DataFrame`
   Shaped like `levels.zone_features`, and read that function first so the two match. One row
   per query, adding `dist_to_band_below_bps`, `dist_to_band_above_bps`, `in_band`,
   `band_leverage` (the nearest band's rung), `bands_stacked` (how many distinct anchors project
   a band within `band_width_bps` of the query price, which is the confluence count and the
   thing the whole idea rests on), `fuel_below_usd` and `fuel_above_usd` (summed weights within
   `fuel_window_bps` each way). Only bands with `known_from <= ts_ms` are considered.

### Decisions already made (follow these; do not re-ask)

1. **The ladder is percentages, never dollars.** Default `features.liqmap.ladder:
   [3, 5, 10, 15, 20, 25, 40, 50]`, the exchange leverage menu.
2. **One flat maintenance margin.** `mm_frac` default 0.005. Hyperliquid's real rule is half
   the initial margin at the asset's max leverage, so the exact value is per coin, but it shifts
   every band by well under a tenth of the gap between rungs. Leave a `ponytail:` comment naming
   the ceiling and the per-coin upgrade path. Do not build a per-coin table.
3. **Uniform weights are the primary result.** Run Q10b and Q10c first with every rung weighted
   equally, and OI-scaled plus histogram-weighted as **one** preregistered secondary. If the
   dumb version works we never need the clever one, and a weighting scheme tuned until a table
   passes is the failure mode the preregistration exists to prevent.
4. **The starting histogram is measured, not invented.** From 194 addresses and $1,561M of
   tracked notional on the VPS on 2026-09-09, share of notional by leverage: under 3x 5.2%,
   3 to 5x 7.2%, 5 to 10x 22.3%, 10 to 20x 32.1%, 20 to 25x 17.5%, 25 to 40x 15.8%, 40x and up
   0%. One day, 194 addresses, one venue: recompute it over whatever `hl_positions` days are
   synced and report both. **This is the only free source of a leverage distribution anywhere.**
   Binance, Bybit and OKX publish open interest and funding and never leverage.
5. **Anchors are untouched extremes only.** Use the existing touch count in
   `levels._with_touches`. An anchor price has been revisited means those positions were already
   resolved. `features.levels.fractal_n` stays at its current default so anchors are identical
   to Q7's.
6. **`features.liqmap.max_dist_bps` is its own key, default 1200.** The existing
   `features.levels.max_dist_bps` of 200 is far too tight for a ladder whose far rungs sit 10%
   out, and changing it would silently move Q7's result.
7. **No changes to `features/levels.py`.** If you need something from it, call it.

### Preregistration, committed first as `reports/research/PREREGISTRATION_Q10.md`

Transcribe the bars below before any script reads real data. Any later change is a dated
amendment in `docs/DECISIONS.md` stating what had already been seen. Every number is net-stress:
0.09% round trip, entry one second after the signal, one effective spread per side, reported in
the same three-column form Q7 uses. Cluster bootstrap and the top-five-cluster removal on every
headline. Under 40 episodes in a cell is `inconclusive`, never a result.

**Q10a, reaction.** For each projected band that price first reaches at time `t` over the year,
measure the move over the next 5, 15 and 30 minutes in the direction that fades the approach.
Control: **shifted rungs**, the same anchors and the same distance scale, each rung moved by
half the gap to the next rung, so the only thing varying is whether the distance is a real
leverage level. DECISION yes when the ladder's 15-minute net-stress mean exceeds the shifted
control's and the cluster-bootstrap interval on the difference excludes zero; no when it
excludes zero the other way; inconclusive otherwise.

**Q10b, cascade confluence.** Q7's question with projected bands in place of raw levels. Same
qualifying episodes from `build_episodes`, same outcomes from `attach_outcomes`, same widths of
10, 15 and 25 bps, asked as of cascade start so a band the cascade itself created is invisible.
Report the in-band and out-of-band intervals side by side, and separately by `bands_stacked` of
one, two, and three or more, because confluence is the actual claim. DECISION yes when some
width has in-band beating out-of-band with a bootstrap interval on the difference excluding zero
and the effect surviving the top-five-cluster removal; no when no width does; inconclusive on
sample. **Print Q7's corresponding row beside every row of this table.**

**Q10c, fuel and exhaustion.** At each episode's start, split episodes into terciles by
`fuel_ahead_usd`, the summed weight of bands the cascade would have to run through in its own
direction within `fuel_window_bps`. Preregistered direction: the low-fuel tercile reverts
better, because the forced flow ahead is already spent. Outcomes at 5, 15 and 30 minutes plus
the stop-out rate in Q8's winning `(k_stop, k_target)` cell, and the comparison that matters is
against Q2's `fixed_delay` baseline, not against zero. DECISION mirrors Q9b in
`prompts/05d_external_ideas.md` exactly, so the projected map and the real map are graded on
the same question with the same bars.

**Q10d, does the projection match reality** (descriptive, no DECISION). Over whatever
`hl_positions` days are synced, for each projected band report the real notional whose
`liquidation_px` falls within `band_width_bps` of it, the share of bands with any real notional,
and the rank correlation between projected weight and real notional. Report separately for
isolated and cross margin, because a cross-margin liquidation price moves with the rest of that
account's book and 98% of the tracked notional is cross. This is the sanity check on the whole
approach and it needs no waiting.

**Stop rules.** Q10a no and Q10b no: the ladder is dead, `features/liqmap.py` stays in the repo
switched off exactly as the zone code did, and the result is recorded beside Q7 so nobody
re-derives it. Q10b yes: the next prompt is one confluence rule in `strategy/rules.py`,
disabled by default, plus a Q8-style stop and target grid keyed on band distance. Q10c yes
alone: an exhaustion feature only, and it merges with whatever Q9b concludes.

### The seams (agreed; do not re-ask)

Failing test first, every time.

1. `ladder_bands` on a hand-built frame: a resistance anchor at 100 with `ladder: [10]` and
   `mm_frac: 0.005` produces one band at 90.5 with `side = "long_liq"`; a support anchor at 100
   produces 109.5 with `side = "short_liq"`; `known_from` is copied, never recomputed; a rung
   whose band falls outside `max_dist_bps` is dropped.
2. `map_features` no-lookahead property test, the same shape `levels` uses: mutate every row
   with `ts_ms >= t` and assert the output at `t` is byte-identical. A band whose
   `known_from` is after the query is invisible.
3. `bands_stacked` counts distinct **anchors**, not bands, so one anchor cannot stack with
   itself through two adjacent rungs.
4. `band_weights` decay: a band twice `half_life_min` old carries a quarter of the weight; an
   anchor whose window shows no open-interest increase carries zero, not null.
5. `tests/test_research_scripts.py`: `scripts/research/q10_ladder.py` runs end to end over
   `research_lake` and every DECISION reads `inconclusive`, because four synthetic hours cannot
   clear a 40-episode floor.

### Slices, in order

1. `PREREGISTRATION_Q10.md`, committed before the script touches real data.
2. `features/liqmap.py` with its tests, offline, under 30s, `make test` and `make lint` clean.
3. `scripts/research/q10_ladder.py` with the run line in its docstring, one day at a time.
4. The fixture extension and the end-to-end test.
5. The run, then the report.

### Deliverables

`liqfade/features/liqmap.py` and its tests; `config/default.yaml` gaining a
`features.liqmap` block, every key with its unit and rationale, and the matching pydantic model;
`scripts/research/q10_ladder.py`; `reports/research/q10_ladder.md` with the three-column net
tables, Q7's rows printed beside Q10b's, the four DECISION lines, episode and cluster counts and
bootstrap intervals; a Q10 row in `reports/research/SUMMARY.md`; a dated `docs/DECISIONS.md`
entry; and a new `docs/research/projected_liqmap.md` recording the decode of the public
indicator, the leverage arithmetic, the measured histogram and the verdicts, so the next person
does not re-derive any of it.
