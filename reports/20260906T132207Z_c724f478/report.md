# Backtest 20260906T132207Z_c724f478

**Trigger: `exact`** - the lake's exact Hyperliquid liquidations, from the S3 pull. **These are not visible in real time.** Every number below is a ceiling on what the strategy could have made with a trigger nobody can see live, not a forecast.

- config hash: `c724f478a779c10b83f8c47326b14454ef2157beecca57c3e08a847410fbab88`
- window: 2025-10-09 00:00 to 2025-10-12 00:00 UTC
- coins: BTC, ETH, HYPE, SOL
- exhaustion mode: `rule`
- latency: 1000 ms
- starting equity: 10,000.00 USD
- replayed: 7.2% of the tape in 8 replay windows, skipping 240,621s

Every number below is net of fees and funding. There is no gross headline; `gross_pnl` appears once, beside the costs it paid.

## Headline

| metric | value |
|---|---|
| trades | 4 |
| net PnL | -38.02 USD |
| mean net | -34.2 bps |
| bootstrap 95% CI | [-83.9, 23.5] bps over 1000 draws |
| hit rate | 25.0% |
| profit factor | 0.04 |
| Sharpe per trade | -0.54 |
| Sortino per trade | -0.50 |
| Sharpe daily, annualised | -22.48 |
| max drawdown | 38.02 USD (0.4%) |
| deflated Sharpe | 0.174 over 1 configurations tried |

## Costs

| item | USD |
|---|---|
| gross PnL | -34.08 |
| fees | -3.93 |
| funding | -0.00 |
| **net PnL** | **-38.02** |

Funding was applied on 0 hour boundaries and unavailable on 0. An unavailable hour costs zero and is counted rather than guessed.

## Concentration

The best 5 trades carry **0.0%** of net PnL. Phase 3's stop rule 3 is that a headline which dies when the best five leave the sample is those five, not an edge.

## Holdout

Split by trade count, not by days, so both sides carry comparable evidence regardless of where the calendar fell.

| split | trades | net PnL | mean bps | hit rate | no-book fills |
|---|---|---|---|---|---|
| train | 3 | -30.46 | -27.6 | 33.3% | 0.0% |
| holdout | 1 | -7.56 | -54.2 | 0.0% | 0.0% |

The share of fills priced without a book snapshot is reported per split above. A gap over 10 points between splits means the two were priced by different models and are not directly comparable: see `docs/DATA_SOURCES.md` on the 2026-06-21 l2 cadence step.

## Fill quality

| metric | value |
|---|---|
| fills priced without a book | 0.0% |
| mean entry slippage | 10.5 bps |
| mean stop slippage | 0.0 bps |
| stop-out rate | 0.0% |
| mean hold | 2 s |
| max hold | 3 s |
| exposure | 0.0% of the window |
| turnover | 6,566 USD |

## Where the intents went

| stage | count |
|---|---|
| intents emitted | 6 |
| entries placed | 4 |
| entries abandoned unfilled | 0 |
| completed trades | 4 |

**Blocked by sizing**

_none_

**Blocked by risk**

| reason | count |
|---|---|
| cluster_exposure | 2 |

**Blocked by filters**

| reason | count |
|---|---|
| edge_too_small | 6 |
| spread_too_wide | 1 |

**Blocked by rules**

| reason | count |
|---|---|
| confluence count | 2 |

## Rules

A rule with zero evaluations was switched off for this run. Required rules block on their own; of the optional ones, 2 had to agree.

**Required**

| rule | evaluated | passed | intents killed |
|---|---|---|---|
| min_cascade_notional | 15 | 15 | 0 |

**Confluence**

| rule | evaluated | passed | pass rate |
|---|---|---|---|
| level_reclaim | 15 | 10 | 66.7% |
| oi_flush | 15 | 7 | 46.7% |
| vwap_stretch | 15 | 15 | 100.0% |
| zone_proximity | 15 | 5 | 33.3% |

## Splits

**By coin**

| bucket | net PnL |
|---|---|
| BTC | -31.84 |
| ETH | -7.59 |
| SOL | 1.41 |

**By UTC hour of entry**

| bucket | net PnL |
|---|---|
| 20 | -24.28 |
| 21 | 1.41 |
| 22 | -7.56 |
| 8 | -7.59 |

**By cascade size**

| bucket | net PnL |
|---|---|
| 5m-20m | -15.14 |
| >20m | -22.87 |

**By urgency**

| bucket | net PnL |
|---|---|
| high | -38.02 |

**Exits, by reason**

| bucket | trades |
|---|---|
| target | 4 |

## Stability

Walk-forward over 30 day windows. v0 fits nothing, so this reports whether the result is steady rather than validating a fit: metrics that swing window to window mean the pooled number is an average over regimes rather than a description of any of them.

| window | from | trades | net PnL | mean bps |
|---|---|---|---|---|
| 0 | 2025-10-09 00:00 | 4 | -38.02 | -34.2 |
