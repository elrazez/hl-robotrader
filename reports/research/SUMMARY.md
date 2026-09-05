# Phase 3 summary: the go/no-go

**Status: RUN PENDING.** Every cell below is filled in by reading the eight reports after
the owner runs them against the real lake. Nothing here is a result yet. The template is
committed ahead of the run so it is obvious what will be filled in and impossible to
quietly change the shape of the answer afterwards.

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
| 1 | reversion after an exact cascade | RUN PENDING | RUN PENDING | RUN PENDING | [q1_reversion.md](q1_reversion.md) |
| 2 | exhaustion timing against a fixed delay | RUN PENDING | RUN PENDING | RUN PENDING | [q2_exhaustion.md](q2_exhaustion.md) |
| 3 | forced flow proxy calibration | RUN PENDING | RUN PENDING | RUN PENDING | [../proxy_calibration.md](../proxy_calibration.md) |
| 4 | cross-venue cascades HL did not see | RUN PENDING | RUN PENDING | RUN PENDING | [q4_crossvenue.md](q4_crossvenue.md) |
| 5 | partial liquidation rhythm | RUN PENDING | RUN PENDING | RUN PENDING | [q5_rhythm.md](q5_rhythm.md) |
| 6 | filters that remove the losing tail | RUN PENDING | RUN PENDING | RUN PENDING | [q6_filters.md](q6_filters.md) |
| 7 | supply and demand zone confluence | RUN PENDING | RUN PENDING | RUN PENDING | [q7_zones.md](q7_zones.md) |
| 8 | path, MAE and the stop/target grid | RUN PENDING | RUN PENDING | RUN PENDING | [q8_path.md](q8_path.md) |

Q1 and Q8 are the gates. Q1's key number is the mean net-stress PnL at **+15 minutes**, the
only horizon that can produce a yes. Q8's is the winning cell's net-stress against
hold-to-horizon, with its stop-out rate.

## Capacity

| measure | value |
|---|---|
| capacity-weighted edge, dollars per episode at 25% participation | RUN PENDING |
| annualised at the sample's own episode rate | RUN PENDING |
| stop rule 4 threshold | $2,000 a year |

## The trigger Phase 4 is built on

RUN PENDING. Q3 decides it and writes it into `../proxy_calibration.md`:

- **HL-native** if the three-gate proxy reaches precision >= 0.6 and recall >= 0.4 on
  core+major. Q4 then stays inconclusive and non-blocking until Phase 7.
- **cross-venue** (Bybit, the only complete feed) otherwise. Q4 then becomes a Phase 4
  entry requirement at the reduced bar of 15 clusters and 60 recorder days.

## Robustness on the headline

All three are mandatory and judged on point estimates. Anything that passes pooled and
fails a split is written "conditional yes, gated on X", never a plain yes.

| split | result |
|---|---|
| positive in >= 3 of 4 quarters | RUN PENDING |
| pooled CI survives dropping the best quarter | RUN PENDING |
| positive excluding BTC and ETH | RUN PENDING |
| positive excluding HYPE | RUN PENDING |
| isolated-coin bucket passes on its own | RUN PENDING |
| headline survives the top 5 clusters being removed | RUN PENDING |

## Stop rules

The project stops on any one of these, on the pre-registered run only.

| # | rule | triggered? |
|---|---|---|
| 1 | no edge: Q1 net-stress at +15m <= 0 on a qualifying sample | RUN PENDING |
| 2 | unharvestable: no Q8 cell positive with a stop-out rate under 50% and positive neighbours | RUN PENDING |
| 3 | lottery: headline dies with the top 5 clusters removed, or fails 2 of 3 splits | RUN PENDING |
| 4 | no capacity: under $2,000 a year at 25% participation | RUN PENDING |
| 5 | no trigger: proxy under precision 0.5 and Q4 unanswerable for months | RUN PENDING |

## Config writeback

Only parameters the data spoke to directly, only to values the data actually produced,
never a value chosen to make a table pass. Every change is listed in `docs/DECISIONS.md`
with the report path, the sample size and the old value.

| key | current | proposed | evidence |
|---|---|---|---|
| `intent.k_target` | 0.5 | RUN PENDING | Q8's winning cell, only if its neighbours are positive |
| `intent.k_stop` | 1.5 | RUN PENDING | as above |
| `intent.time_stop_s` | 900 | RUN PENDING | as above |
| `cascade.proxy_min` | 4.0 | RUN PENDING | Q3's F1 curve |
| `filters.*` | see default.yaml | RUN PENDING | Q6's 2x concentration rule |

Frozen by the preregistration and **not** writeable by this phase:
`features.episodes.min_notional_usd` ($250k), `features.episodes.min_move_bps` (30),
`filters.min_edge_multiple`, `sizing.risk_frac`, and every gate in
`docs/STRATEGY_AND_RISK.md` section 5. Research measures the edge; it does not set risk
limits.

## Should Phase 4 proceed?

RUN PENDING.

This paragraph is the deliverable. It says continue or stop, and if continue, with which
parameter values and which gates. If the answer is stop, it says stop. If Q1 fails but
continuation is strongly tradeable, that is the momentum variant, out of v0 scope: it gets
written up as a new thesis needing its own Phase 3 and this one closes. The sign does not
get flipped and carried on.

## What was run

RUN PENDING: the date of the run, the lake's date range, and the git hash of the commit the
scripts were run from. Any re-cut of the episode definition after this run is logged in
`docs/DECISIONS.md` with its rationale **before** it is run, at most twice, and any yes
found on a re-cut is labelled exploratory.
