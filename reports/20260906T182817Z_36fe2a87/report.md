# Backtest 20260906T182817Z_36fe2a87

**Trigger: `exact`** - the lake's exact Hyperliquid liquidations, from the S3 pull. **These are not visible in real time.** Every number below is a ceiling on what the strategy could have made with a trigger nobody can see live, not a forecast.

- config hash: `36fe2a87ac712bdbf8b7a1a7ba60872668987cc02c4f8ce68356b7464fe843e9`
- window: 2025-10-09 00:00 to 2025-10-12 00:00 UTC
- coins: AAVE, ADA, ASTER, AVAX, BERA, BNB, BTC, CRV, DOGE, ENA, ETH, FARTCOIN, HYPE, IP, LINEA, LINK, LTC, MNT, MON, NEAR, ONDO, PAXG, PUMP, SOL, STRK, SUI, TAO, TON, TRUMP, UNI, VIRTUAL, WLD, WLFI, XPL, XRP, ZEC, ZEREBRO, ZORA
- exhaustion mode: `rule`
- latency: 1000 ms
- starting equity: 10,000.00 USD
- replayed: 14.4% of the tape in 12 replay windows, skipping 221,896s

Every number below is net of fees and funding. There is no gross headline; `gross_pnl` appears once, beside the costs it paid.

## Headline

| metric | value |
|---|---|
| trades | 8 |
| net PnL | -210.81 USD |
| mean net | -971.7 bps |
| bootstrap 95% CI | [-1763.3, -237.5] bps over 1000 draws |
| hit rate | 37.5% |
| profit factor | 0.07 |
| Sharpe per trade | -0.81 |
| Sortino per trade | -0.52 |
| Sharpe daily, annualised | -12.89 |
| max drawdown | 224.75 USD (2.2%) |
| deflated Sharpe | 0.000 over 4 configurations tried |

## Costs

| item | USD |
|---|---|
| gross PnL | -207.16 |
| fees | -3.65 |
| funding | -0.00 |
| **net PnL** | **-210.81** |

Funding was applied on 0 hour boundaries and unavailable on 0. An unavailable hour costs zero and is counted rather than guessed.

## Concentration

The best 5 trades carry **0.0%** of net PnL. Phase 3's stop rule 3 is that a headline which dies when the best five leave the sample is those five, not an edge.

## Holdout

Split by trade count, not by days, so both sides carry comparable evidence regardless of where the calendar fell.

| split | trades | net PnL | mean bps | hit rate | no-book fills |
|---|---|---|---|---|---|
| train | 6 | -91.56 | -795.7 | 50.0% | 0.0% |
| holdout | 2 | -119.25 | -1499.8 | 0.0% | 0.0% |

The share of fills priced without a book snapshot is reported per split above. A gap over 10 points between splits means the two were priced by different models and are not directly comparable: see `docs/DATA_SOURCES.md` on the 2026-06-21 l2 cadence step.

## Fill quality

| metric | value |
|---|---|
| fills priced without a book | 62.5% |
| mean entry slippage | 12.1 bps |
| mean stop slippage | 46.2 bps |
| stop-out rate | 50.0% |
| mean hold | 267 s |
| max hold | 900 s |
| exposure | 0.6% of the window |
| turnover | 5,442 USD |

## Where the intents went

| stage | count |
|---|---|
| intents emitted | 14 |
| entries placed | 8 |
| entries abandoned unfilled | 0 |
| completed trades | 8 |

**Blocked by sizing**

_none_

**Blocked by risk**

| reason | count |
|---|---|
| cluster_exposure | 1 |
| max_positions | 5 |

**Blocked by filters**

| reason | count |
|---|---|
| edge_too_small | 1 |
| spread_too_wide | 1 |
| target_behind | 18 |

**Blocked by rules**

| reason | count |
|---|---|
| confluence count | 10 |

## Rules

A rule with zero evaluations was switched off for this run. Required rules block on their own; of the optional ones, 2 had to agree.

**Required**

| rule | evaluated | passed | intents killed |
|---|---|---|---|
| min_cascade_notional | 44 | 44 | 0 |

**Confluence**

| rule | evaluated | passed | pass rate |
|---|---|---|---|
| level_reclaim | 44 | 18 | 40.9% |
| oi_flush | 44 | 22 | 50.0% |
| vwap_stretch | 44 | 43 | 97.7% |
| zone_proximity | 44 | 7 | 15.9% |

## Splits

**By coin**

| bucket | net PnL |
|---|---|
| AAVE | -54.48 |
| AVAX | -51.02 |
| BNB | -62.65 |
| ETH | 6.07 |
| SOL | -56.60 |
| WLFI | 2.76 |
| XPL | 5.11 |

**By UTC hour of entry**

| bucket | net PnL |
|---|---|
| 15 | 2.76 |
| 2 | -2.65 |
| 20 | 6.07 |
| 21 | -224.75 |
| 8 | 7.76 |

**By cascade size**

| bucket | net PnL |
|---|---|
| 1m-5m | -62.65 |
| 250k-1m | -54.48 |
| 5m-20m | -43.16 |
| >20m | -50.53 |

**By urgency**

| bucket | net PnL |
|---|---|
| high | -210.81 |

**Exits, by reason**

| bucket | trades |
|---|---|
| stop | 4 |
| target | 3 |
| time_stop | 1 |

## Stability

Walk-forward over 30 day windows. v0 fits nothing, so this reports whether the result is steady rather than validating a fit: metrics that swing window to window mean the pooled number is an average over regimes rather than a description of any of them.

| window | from | trades | net PnL | mean bps |
|---|---|---|---|---|
| 0 | 2025-10-09 00:00 | 8 | -210.81 | -971.7 |
