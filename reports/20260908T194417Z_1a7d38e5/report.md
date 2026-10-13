# Backtest 20260908T194417Z_1a7d38e5

**Trigger: `exact`** - the lake's exact Hyperliquid liquidations, from the S3 pull. **These are not visible in real time.** Every number below is a ceiling on what the strategy could have made with a trigger nobody can see live, not a forecast.

- config hash: `1a7d38e5c41e576bf344937d3f9ffd43d8afb963d4e4f3fdb19ac1b7359d6ec2`
- window: 2025-10-09 00:00 to 2025-10-12 00:00 UTC
- coins: ASTER, AVAX, BNB, BTC, DOGE, ENA, ETH, FARTCOIN, HYPE, LINK, NEAR, PUMP, SOL, SUI, TAO, TON, TRUMP, UNI, WLD, XPL, XRP, ZEC
- exhaustion mode: `rule`
- latency: 1000 ms
- starting equity: 10,000.00 USD
- replayed: 52.2% of the tape in 31 replay windows, skipping 123,877s

Every number below is net of fees and funding. There is no gross headline; `gross_pnl` appears once, beside the costs it paid.

## Headline

| metric | value |
|---|---|
| trades | 18 |
| net PnL | -20.84 USD |
| mean net | 1222.7 bps |
| bootstrap 95% CI | [-814.0, 3853.4] bps over 1000 draws |
| hit rate | 50.0% |
| profit factor | 0.93 |
| Sharpe per trade | 0.22 |
| Sortino per trade | 0.69 |
| Sharpe daily, annualised | -13.71 |
| max drawdown | 276.71 USD (2.8%) |
| deflated Sharpe | 0.000 over 5 configurations tried |

## Costs

| item | USD |
|---|---|
| gross PnL | -6.67 |
| fees | -14.16 |
| funding | -0.00 |
| **net PnL** | **-20.84** |

Funding was applied on 0 hour boundaries and unavailable on 0. An unavailable hour costs zero and is counted rather than guessed.

## Concentration

The best 5 trades carry **0.0%** of net PnL. Phase 3's stop rule 3 is that a headline which dies when the best five leave the sample is those five, not an edge.

## Holdout

Split by trade count, not by days, so both sides carry comparable evidence regardless of where the calendar fell.

| split | trades | net PnL | mean bps | hit rate | no-book fills |
|---|---|---|---|---|---|
| train | 14 | -269.73 | -710.8 | 35.7% | 0.0% |
| holdout | 4 | 248.90 | 7989.7 | 100.0% | 0.0% |

The share of fills priced without a book snapshot is reported per split above. A gap over 10 points between splits means the two were priced by different models and are not directly comparable: see `docs/DATA_SOURCES.md` on the 2026-06-21 l2 cadence step.

## Fill quality

| metric | value |
|---|---|
| fills priced without a book | 52.8% |
| mean entry slippage | 46.4 bps |
| mean stop slippage | 31.7 bps |
| stop-out rate | 33.3% |
| mean hold | 367 s |
| max hold | 900 s |
| exposure | 1.8% of the window |
| turnover | 18,485 USD |

## Where the intents went

| stage | count |
|---|---|
| intents emitted | 56 |
| entries placed | 18 |
| entries abandoned unfilled | 0 |
| completed trades | 18 |

**Blocked by sizing**

_none_

**Blocked by risk**

| reason | count |
|---|---|
| cluster_exposure | 25 |
| coin_cooldown | 3 |
| max_positions | 8 |
| max_positions_per_direction | 1 |
| stale_coin_data | 1 |

**Blocked by filters**

| reason | count |
|---|---|
| edge_too_small | 65 |
| spread_too_wide | 1 |
| target_behind | 145 |

**Blocked by rules**

_none_

## Rules

A rule with zero evaluations was switched off for this run. Required rules block on their own; of the optional ones, 0 had to agree.

**Required**

| rule | evaluated | passed | intents killed |
|---|---|---|---|
| min_cascade_notional | 267 | 267 | 0 |
| zone_proximity | 0 | 0 | 0 |

**Confluence**

| rule | evaluated | passed | pass rate |
|---|---|---|---|
| level_reclaim | 267 | 121 | 45.3% |
| oi_flush | 267 | 61 | 22.8% |
| vwap_stretch | 267 | 248 | 92.9% |

## Splits

**By coin**

| bucket | net PnL |
|---|---|
| AVAX | -50.69 |
| BNB | -62.25 |
| ETH | -60.02 |
| HYPE | -3.81 |
| LINK | -50.10 |
| NEAR | 33.68 |
| PUMP | 0.92 |
| SOL | -51.73 |
| TRUMP | 157.40 |
| XPL | 59.51 |
| XRP | 6.25 |

**By UTC hour of entry**

| bucket | net PnL |
|---|---|
| 14 | -8.03 |
| 15 | -21.08 |
| 19 | 4.22 |
| 2 | -2.65 |
| 20 | -21.83 |
| 21 | 37.08 |
| 8 | -8.55 |

**By cascade size**

| bucket | net PnL |
|---|---|
| 1m-5m | 143.06 |
| 5m-20m | -136.83 |
| >20m | -27.07 |

**By urgency**

| bucket | net PnL |
|---|---|
| normal | -20.84 |

**Exits, by reason**

| bucket | trades |
|---|---|
| stop | 6 |
| target | 7 |
| time_stop | 5 |

## Stability

Walk-forward over 30 day windows. v0 fits nothing, so this reports whether the result is steady rather than validating a fit: metrics that swing window to window mean the pooled number is an average over regimes rather than a description of any of them.

| window | from | trades | net PnL | mean bps |
|---|---|---|---|---|
| 0 | 2025-10-09 00:00 | 18 | -20.84 | 1222.7 |
