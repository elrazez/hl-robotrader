# Phase 3 summary: the go/no-go

**Status: run complete, 2026-09-05**, over 2025-09-01 to 2026-08-30 against the lake at
`/Volumes/<drive>/liqfade-data`. Read the two amendments in `docs/DECISIONS.md` dated
2026-09-05 before any number below: the universe was not filtered on the first run and that
result is void, and Q8's "beats hold-to-horizon" clause was a drafting error replaced by
stop rule 2.

Preregistration: [`PREREGISTRATION.md`](PREREGISTRATION.md), committed as
**`901f0eebc722f7dc8942dc180fbd4889f90f7de9`** (`901f0ee`), before any script had read the
real lake. Every bar in the table below is the one written in that commit.

## Step 0: does `asset_ctx` cover the year? Yes.

Confirmed by the owner on 2026-09-05 from

    uv run python scripts/research/data_quality.py --footers-only --data-dir <lake>

- **days present / expected: 363 of 365.** Both absences are the ones
  `docs/DATA_SOURCES.md` already predicted rather than anything new.
- **cadence: one row per coin per minute**, 1440 per coin per day, across 229 to 232 perps.
- **source: `hl_archive`, `rx_ms = 0`**, from `s3://hyperliquid-archive/asset_ctxs/`.

So `features/context.py` is real for the year at that cadence, Q6's funding filter is
evaluable, and all three of the proxy's gates are calibrated on the year rather than on the
twelve Tardis days. The phase proceeds.

Two caveats a reader of the numbers below should carry:

- `asset_ctxs/20260708.csv.lz4` does not exist in Hyperliquid's bucket. It is their gap,
  and it was left empty rather than filled from another source, which would have put a row
  in the lake that `source` could not honestly label.
- **2026-08-27 and 2026-08-30 are short rather than absent**: 155,440 and 119,248 rows
  against a full day's 334,080. They count as present in the 363 and are not a hole in the
  day count, but they are a hole in those two days. Anything reading the spread or the
  proxy gates on those dates is reading a partial day.

## The eight questions

| # | question | DECISION | key number, net-stress | episodes / clusters / months | report |
|---|---|---|---|---|---|
| 1 | reversion after an exact cascade | **yes** | +84.6 bps at +15m, 95% CI [+25.1, +180.0] | 2722 / 1758 / 12 | [q1_reversion.md](q1_reversion.md) |
| 2 | exhaustion timing against a fixed delay | **no** | a flat 120s delay carries more PnL than the rule | 2722 / 1758 / 12 | [q2_exhaustion.md](q2_exhaustion.md) |
| 3 | forced flow proxy calibration | **no** | precision 0.01 at recall 0.68; 429,227 triggers against 2,617 episodes | 2617 / n/a / 12 | [../proxy_calibration.md](../proxy_calibration.md) |
| 4 | cross-venue cascades HL did not see | **inconclusive** | 144 orphans, 102 clusters, but only 12 days of cross-venue data | 144 / 102 / 12 days | [q4_crossvenue.md](q4_crossvenue.md) |
| 5 | partial liquidation rhythm | **no** | 1.65x mass at the 30s cooldown, against a 2x bar | 2722 / 1758 / 12 | [q5_rhythm.md](q5_rhythm.md) |
| 6 | filters that remove the losing tail | **no** | best concentration 1.25x (BTC cascading), against a 2x bar | 2722 / 1758 / 12 | [q6_filters.md](q6_filters.md) |
| 7 | supply and demand zone confluence | **no** | in-zone is *worse*: +36.1 against +146.3 out of zone at 15 bps | 1523 in zone / 1052 / 12 | [q7_zones.md](q7_zones.md) |
| 8 | path, MAE and the stop/target grid | **yes with a stop** | `k_stop=1`, `k_target=0.8` keeps +80.6 of +84.6 at 15% stop-outs | 2722 / 1758 / 12 | [q8_path.md](q8_path.md) |

Q1 and Q8 are the gates and both pass. Everything between them says the strategy is simpler
than the spec assumed: no exhaustion rule, no rhythm, no filters, no zones.

**The number Phase 4 sizes on is not +84.6.** The pooled headline is +26.7 bps in the
isolated-coin bucket, +34.0 with the top five clusters removed, and +32.2 with the effective
spread capped at 30 bps. Three independent cuts agree on roughly 30 bps; the difference
between that and +84.6 is cluster upside, and **a cluster is one bet** — a market-wide sweep
liquidates dozens of coins inside one minute and the strategy would have been in all of them
at once. **The risk layer must cap exposure per cluster**, not only per coin and in total.

## Capacity

| measure | value |
|---|---|
| capacity-weighted edge, dollars per episode at 25% participation | $41,112 |
| annualised at the sample's own episode rate | $111,905,595 |
| stop rule 4 threshold | $2,000 a year |

Stop rule 4 is not triggered, by four orders of magnitude. **Read the annualised figure as a
ceiling on the venue, not as a forecast**: it is 25% of the tape in the 60s after every one
of 2,722 extremes, summed, and assumes capital enough to take all of it. The per-episode
figure is the meaningful one, and it says capacity is not what limits this strategy.

## The trigger Phase 4 is built on

**Cross-venue, on Bybit** — and that makes Q4 a Phase 4 entry requirement, which Q4 cannot
meet for two months.

The three-gate proxy reaches recall 0.68 but precision 0.01 on core+major, at every
threshold from 2 to 6. The gates are working: both are open on only 1.0% of ticks and gating
cuts qualifying ticks 38-fold, from 786,000 to 20,600 on a sample day, and it is that gating
which lifts recall from 0.30 ungated to 0.68 gated. The problem is the trigger rate. The
proxy fires **429,227 times a year** across roughly thirty coins, one every 37 minutes per
coin, against 2,617 episodes to find. **Precision is capped at 0.6% by construction** at that
rate, whatever the proxy is correlated with, so "precision 0.01" says the proxy is not
selective, not that it is uninformative.

That distinction matters for Phase 7 and not for now: the preregistered thresholds were
2/3/4/6 and threshold 6 still produces 303,490 triggers, so the useful threshold, if one
exists, is far above the range this phase agreed to test. Extending it is a re-cut and has
not been done.

## Robustness on the headline

| split | result |
|---|---|
| positive in >= 3 of 4 quarters | **pass**, 5 of 5: +62.2, +141.5, +34.1, +41.1, +92.8 |
| pooled CI survives dropping the best quarter | **pass** |
| positive excluding BTC and ETH | **pass**, +111.4 |
| positive excluding HYPE | **pass**, +92.3 |
| isolated-coin bucket passes on its own | **pass**, +26.7 (market-wide sweeps are +160.4) |
| headline survives the top 5 clusters being removed | **pass**, +34.0 |
| used spread <= 30 bps (reported, not applied) | +32.2 on 2602 of 2722 episodes |

All three preregistered splits pass on point estimates, so this is a plain yes rather than a
conditional one. The isolated-coin figure is the one that matters for sizing.

## Stop rules

| # | rule | triggered? |
|---|---|---|
| 1 | no edge: Q1 net-stress at +15m <= 0 on a qualifying sample | **no**, +84.6 |
| 2 | unharvestable: no Q8 cell positive with stop-out under 50% and positive neighbours | **no**, 12 of 12 cells qualify |
| 3 | lottery: headline dies with the top 5 clusters removed, or fails 2 of 3 splits | **no**, +34.0 and 3 of 3 splits pass |
| 4 | no capacity: under $2,000 a year at 25% participation | **no** |
| 5 | no trigger: proxy under precision 0.5 and Q4 unanswerable for months | **YES, TRIGGERED** |

## Config writeback

Proposed, **not applied**. Every value below comes from a report and none has been written
into `config/default.yaml` yet; risk-adjacent config is not changed without the owner saying
so.

| key | current | proposed | evidence |
|---|---|---|---|
| `intent.k_target` | 0.5 | **0.8** | Q8's winning cell, neighbours positive |
| `intent.k_stop` | 1.5 | **1.0** | as above |
| `intent.time_stop_s` | 900 | 900, unchanged | Q8's grid used it and did not test alternatives |
| `cascade.proxy_min` | 4.0 | **no change** | Q3's F1 curve is flat at 0.01 across 2/3/4/6; the data did not choose a value |
| `filters.skip_when_btc_cascading` | true | **false** | Q6: 1.25x concentration, under the 2x bar |
| `filters.funding_extreme_bps_8h` | 30.0 | **off** | Q6: removes 0.6% of the sample and 0% of the loss tail |
| `filters.max_spread_bps` | 3/6/12 by tier | **30 bps flat, pre-trade** | the core tier's p99 of 22.9; see docs/DECISIONS.md |
| `exhaustion.*` | see default.yaml | **unused in v0** | Q2: a flat 120s delay beats the rule |
| `features.levels.*` | see default.yaml | **built, off** | Q7 |

Frozen by the preregistration and not writeable by this phase:
`features.episodes.min_notional_usd` ($250k), `features.episodes.min_move_bps` (30),
`filters.min_edge_multiple`, `sizing.risk_frac`, and every gate in
`docs/STRATEGY_AND_RISK.md` section 5.

## Should Phase 4 proceed?

**No. The project parks, on stop rule 5.**

Q1 and Q8 both pass on exact S3 liquidations, so the edge is real. But the live bot cannot
see S3 liquidations. The HL-native proxy that was supposed to stand in for them reaches
precision 0.01, far under the 0.5 that stop rule 5 names, so the v0 trigger falls to
cross-venue Bybit; and Q4, which then becomes a Phase 4 entry requirement, is inconclusive
and stays that way until the VPS recorder has run for sixty days. **It has not run at all.**
Stop rule 5 is exactly this situation, and its instruction is to park until the recorder has
the data rather than build Phase 4 blind.

This is a park, not a stop. Nothing here says the thesis is wrong; it says the thesis cannot
be traded yet because the trigger cannot be seen. What unblocks it is a running recorder, and
that is a deployment task, not a research one.

What the eight questions say meanwhile: **the edge is real but smaller and simpler than the
spec assumed.** Roughly 30 bps net-stress per episode on the numbers that survive the
robustness cuts, harvested by entering a fixed delay after the cascade starts and exiting at
`k_target = 0.8` with a stop at `k_stop = 1.0`, with no filters, no zone gate and no
exhaustion rule. The complexity in `docs/STRATEGY_AND_RISK.md` sections 2 and 3 is not
earning its place: Q2, Q5, Q6 and Q7 all say no.

What to do next, in order: **start the recorder on the VPS**, because every remaining
question needs it and nothing else moves until it has run; then rerun Q3 and Q4 at sixty
recorder days, where the proxy's gates read live block-level values instead of the archive's
one-a-minute sampling and Q4 has a real sample. If Q4 then shows Bybit-only cascades revert
on Hyperliquid, Phase 4 is built on a cross-venue trigger with the parameters in the
writeback table. If it does not, this thesis has an edge nobody can reach and the momentum
variant deserves its own Phase 3.

## What was run

The eight scripts over 2025-09-01 to 2026-08-30 on 2026-09-05, from commit `70d0092`,
against the lake at `/Volumes/<drive>/liqfade-data`. Wall time and peak resident memory on an 8 GB machine:

| question | wall | peak RSS |
|---|---|---|
| Q1 | 12.5 min | 2.00 GB |
| Q2 | 5.6 min | 2.98 GB |
| Q3 | 73.6 min | 3.66 GB |
| Q4 | 6.4 min | 1.65 GB |
| Q5 | 0.5 min | 3.18 GB |
| Q6 | 3.9 min | 1.84 GB |
| Q7 | 7.1 min | 2.40 GB |
| Q8 | 2.9 min | 2.64 GB |

Q3, Q5 and Q2 exceed the 3 GB working budget; the others are inside it. Q3 checkpoints one
file a day under `q3_days/`, so a rerun resumes rather than restarting.

No re-cut of the episode definition has been used. The universe fix logged on 2026-09-05 is
recorded as a defect, not a re-cut, so both permitted re-cuts remain available.
