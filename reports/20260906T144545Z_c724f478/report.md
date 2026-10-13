# Backtest 20260906T144545Z_c724f478

**Trigger: `exact`** - the lake's exact Hyperliquid liquidations, from the S3 pull. **These are not visible in real time.** Every number below is a ceiling on what the strategy could have made with a trigger nobody can see live, not a forecast.

- config hash: `c724f478a779c10b83f8c47326b14454ef2157beecca57c3e08a847410fbab88`
- window: 2025-09-01 00:00 to 2026-08-31 00:00 UTC
- coins: BTC, ETH, HYPE, SOL
- exhaustion mode: `rule`
- latency: 1000 ms (lagged rerun, requested 3000 ms)
- starting equity: 10,000.00 USD
- replayed: 6.2% of the tape in 843 replay windows, skipping 29,492,530s

Every number below is net of fees and funding. There is no gross headline; `gross_pnl` appears once, beside the costs it paid.

## Headline

| metric | value |
|---|---|
| trades | 39 |
| net PnL | -1,083.41 USD |
| mean net | -141.0 bps |
| bootstrap 95% CI | [-255.6, -64.6] bps over 1000 draws |
| hit rate | 7.7% |
| profit factor | 0.01 |
| Sharpe per trade | -0.48 |
| Sortino per trade | -0.42 |
| Sharpe daily, annualised | -10.04 |
| max drawdown | 1,083.41 USD (10.8%) |
| deflated Sharpe | 0.000 over 2 configurations tried |

## Costs

| item | USD |
|---|---|
| gross PnL | -1,026.18 |
| fees | -57.22 |
| funding | -0.00 |
| **net PnL** | **-1,083.41** |

Funding was applied on 0 hour boundaries and unavailable on 0. An unavailable hour costs zero and is counted rather than guessed.

## Concentration

The best 5 trades carry **0.0%** of net PnL. Phase 3's stop rule 3 is that a headline which dies when the best five leave the sample is those five, not an edge.

## Holdout

Split by trade count, not by days, so both sides carry comparable evidence regardless of where the calendar fell.

| split | trades | net PnL | mean bps | hit rate | no-book fills |
|---|---|---|---|---|---|
| train | 29 | -763.02 | -107.2 | 3.4% | 0.0% |
| holdout | 10 | -320.39 | -239.1 | 20.0% | 0.0% |

The share of fills priced without a book snapshot is reported per split above. A gap over 10 points between splits means the two were priced by different models and are not directly comparable: see `docs/DATA_SOURCES.md` on the 2026-06-21 l2 cadence step.

## Fill quality

| metric | value |
|---|---|
| fills priced without a book | 0.0% |
| mean entry slippage | 87.1 bps |
| mean stop slippage | 37.7 bps |
| stop-out rate | 10.3% |
| mean hold | 72 s |
| max hold | 900 s |
| exposure | 0.0% of the window |
| turnover | 88,611 USD |

## Where the intents went

| stage | count |
|---|---|
| intents emitted | 180 |
| entries placed | 39 |
| entries abandoned unfilled | 0 |
| completed trades | 39 |

**Blocked by sizing**

_none_

**Blocked by risk**

| reason | count |
|---|---|
| cluster_exposure | 10 |
| drawdown | 131 |

**Blocked by filters**

| reason | count |
|---|---|
| edge_too_small | 518 |
| spread_too_wide | 1 |

**Blocked by rules**

| reason | count |
|---|---|
| confluence count | 77 |

## Rules

A rule with zero evaluations was switched off for this run. Required rules block on their own; of the optional ones, 2 had to agree.

**Required**

| rule | evaluated | passed | intents killed |
|---|---|---|---|
| min_cascade_notional | 776 | 776 | 0 |

**Confluence**

| rule | evaluated | passed | pass rate |
|---|---|---|---|
| level_reclaim | 776 | 539 | 69.5% |
| oi_flush | 776 | 175 | 22.6% |
| vwap_stretch | 776 | 719 | 92.7% |
| zone_proximity | 776 | 523 | 67.4% |

## Splits

**By coin**

| bucket | net PnL |
|---|---|
| BTC | -271.13 |
| ETH | -493.64 |
| HYPE | -42.43 |
| SOL | -276.21 |

**By UTC hour of entry**

| bucket | net PnL |
|---|---|
| 1 | 4.41 |
| 10 | -8.43 |
| 11 | -114.47 |
| 12 | -30.85 |
| 13 | -10.77 |
| 14 | 0.36 |
| 15 | -89.70 |
| 16 | -8.62 |
| 17 | -62.99 |
| 18 | -41.58 |
| 2 | -12.84 |
| 20 | -190.41 |
| 21 | -173.63 |
| 22 | -31.26 |
| 3 | -23.60 |
| 4 | -25.75 |
| 6 | -41.94 |
| 7 | -174.52 |
| 8 | -12.20 |
| 9 | -34.61 |

**By cascade size**

| bucket | net PnL |
|---|---|
| 1m-5m | -385.04 |
| 250k-1m | -121.21 |
| 5m-20m | -117.16 |
| >20m | -460.00 |

**By urgency**

| bucket | net PnL |
|---|---|
| high | -1,083.41 |

**Exits, by reason**

| bucket | trades |
|---|---|
| stop | 4 |
| target | 33 |
| time_stop | 2 |

## Stability

Walk-forward over 30 day windows. v0 fits nothing, so this reports whether the result is steady rather than validating a fit: metrics that swing window to window mean the pooled number is an average over regimes rather than a description of any of them.

| window | from | trades | net PnL | mean bps |
|---|---|---|---|---|
| 0 | 2025-09-01 00:00 | 13 | -179.83 | -59.4 |
| 1 | 2025-10-01 00:00 | 19 | -621.05 | -131.6 |
| 2 | 2025-10-31 00:00 | 7 | -282.52 | -318.1 |
| 3 | 2025-11-30 00:00 | 0 | 0.00 | 0.0 |
| 4 | 2025-12-30 00:00 | 0 | 0.00 | 0.0 |
| 5 | 2026-01-29 00:00 | 0 | 0.00 | 0.0 |
| 6 | 2026-02-28 00:00 | 0 | 0.00 | 0.0 |
| 7 | 2026-03-30 00:00 | 0 | 0.00 | 0.0 |
| 8 | 2026-04-29 00:00 | 0 | 0.00 | 0.0 |
| 9 | 2026-05-29 00:00 | 0 | 0.00 | 0.0 |
| 10 | 2026-06-28 00:00 | 0 | 0.00 | 0.0 |
| 11 | 2026-07-28 00:00 | 0 | 0.00 | 0.0 |
| 12 | 2026-08-27 00:00 | 0 | 0.00 | 0.0 |
