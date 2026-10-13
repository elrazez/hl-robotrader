# Backtest 20260906T161501Z_55b1127f

**Trigger: `exact`** - the lake's exact Hyperliquid liquidations, from the S3 pull. **These are not visible in real time.** Every number below is a ceiling on what the strategy could have made with a trigger nobody can see live, not a forecast.

- config hash: `55b1127f5169789c3b74ef98dc07c7b488d506e6bbe9f62364ae55a6e610a84b`
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
| trades | 51 |
| net PnL | -601.22 USD |
| mean net | -132.8 bps |
| bootstrap 95% CI | [-245.0, -48.5] bps over 1000 draws |
| hit rate | 49.0% |
| profit factor | 0.20 |
| Sharpe per trade | -0.37 |
| Sortino per trade | -0.25 |
| Sharpe daily, annualised | -7.00 |
| max drawdown | 617.29 USD (6.2%) |
| deflated Sharpe | 0.000 over 3 configurations tried |

## Costs

| item | USD |
|---|---|
| gross PnL | -520.36 |
| fees | -80.84 |
| funding | -0.02 |
| **net PnL** | **-601.22** |

Funding was applied on 1 hour boundaries and unavailable on 0. An unavailable hour costs zero and is counted rather than guessed.

## Concentration

The best 5 trades carry **0.0%** of net PnL. Phase 3's stop rule 3 is that a headline which dies when the best five leave the sample is those five, not an edge.

## Holdout

Split by trade count, not by days, so both sides carry comparable evidence regardless of where the calendar fell.

| split | trades | net PnL | mean bps | hit rate | no-book fills |
|---|---|---|---|---|---|
| train | 38 | -520.37 | -165.0 | 47.4% | 0.0% |
| holdout | 13 | -80.85 | -38.8 | 53.8% | 0.0% |

The share of fills priced without a book snapshot is reported per split above. A gap over 10 points between splits means the two were priced by different models and are not directly comparable: see `docs/DATA_SOURCES.md` on the 2026-06-21 l2 cadence step.

## Fill quality

| metric | value |
|---|---|
| fills priced without a book | 0.0% |
| mean entry slippage | 64.3 bps |
| mean stop slippage | 31.8 bps |
| stop-out rate | 31.4% |
| mean hold | 313 s |
| max hold | 900 s |
| exposure | 0.0% of the window |
| turnover | 108,098 USD |

## Where the intents went

| stage | count |
|---|---|
| intents emitted | 68 |
| entries placed | 51 |
| entries abandoned unfilled | 0 |
| completed trades | 51 |

**Blocked by sizing**

_none_

**Blocked by risk**

| reason | count |
|---|---|
| cluster_exposure | 17 |

**Blocked by filters**

| reason | count |
|---|---|
| edge_too_small | 181 |
| spread_too_wide | 1 |
| target_behind | 449 |

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
| BTC | -203.36 |
| ETH | 10.61 |
| HYPE | -71.80 |
| SOL | -336.67 |

**By UTC hour of entry**

| bucket | net PnL |
|---|---|
| 0 | -97.02 |
| 1 | -18.16 |
| 11 | -3.65 |
| 12 | 2.56 |
| 14 | -19.31 |
| 15 | -86.74 |
| 17 | -8.59 |
| 18 | 12.92 |
| 19 | -63.69 |
| 2 | -4.05 |
| 20 | -44.05 |
| 21 | -98.71 |
| 22 | 28.87 |
| 23 | 6.35 |
| 5 | 9.04 |
| 6 | -15.62 |
| 7 | -173.99 |
| 9 | -27.38 |

**By cascade size**

| bucket | net PnL |
|---|---|
| 1m-5m | -60.94 |
| 250k-1m | -9.35 |
| 5m-20m | -164.14 |
| >20m | -366.79 |

**By urgency**

| bucket | net PnL |
|---|---|
| high | -601.22 |

**Exits, by reason**

| bucket | trades |
|---|---|
| stop | 16 |
| target | 26 |
| time_stop | 9 |

## Stability

Walk-forward over 30 day windows. v0 fits nothing, so this reports whether the result is steady rather than validating a fit: metrics that swing window to window mean the pooled number is an average over regimes rather than a description of any of them.

| window | from | trades | net PnL | mean bps |
|---|---|---|---|---|
| 0 | 2025-09-01 00:00 | 3 | -26.89 | -35.9 |
| 1 | 2025-10-01 00:00 | 5 | -110.45 | -542.9 |
| 2 | 2025-10-31 00:00 | 6 | -255.70 | -499.2 |
| 3 | 2025-11-30 00:00 | 4 | -5.19 | -6.6 |
| 4 | 2025-12-30 00:00 | 4 | -93.69 | -97.3 |
| 5 | 2026-01-29 00:00 | 5 | -23.12 | -2.7 |
| 6 | 2026-02-28 00:00 | 6 | -3.90 | -2.7 |
| 7 | 2026-03-30 00:00 | 4 | -8.79 | -9.3 |
| 8 | 2026-04-29 00:00 | 4 | 6.56 | 8.7 |
| 9 | 2026-05-29 00:00 | 2 | 10.64 | 22.5 |
| 10 | 2026-06-28 00:00 | 1 | 8.39 | 35.4 |
| 11 | 2026-07-28 00:00 | 7 | -99.08 | -84.1 |
| 12 | 2026-08-27 00:00 | 0 | 0.00 | 0.0 |
