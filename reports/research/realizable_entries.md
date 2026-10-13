# Realizable entries: where the Phase 3 edge goes when the entry is live

Generated 2026-09-08 19:05 UTC from the Parquet lake.

Episodes: the 2,722 qualifying cascades of `q1_episodes.parquet`. Every table below prices a taker entry at the last trade one second after the decision and a maker exit (`net maker`, 6 bps plus one effective spread), and beside it the preregistration's stress cost (9 bps plus two spreads). `equity/yr` sums each trade's return at 0.5% risk per trade with the 25% coin cap, over the year. `after extreme` is the share of entries that landed after the cascade's true low or high, which nothing live can know; `s since extreme` is the median entry time relative to it.

## Does the engine start a cascade at all

The start condition is `abs(ret_60s) / rv_300s >= cascade.ret_over_rv_min`, and `rv_300s` holds the cascade's own moves, so a fast flush scores about 1 and only a slow slide clears 2.

| ret_over_rv_min | episodes | start a cascade | share |
|---|---|---|---|
| 1.0 | 2722 | 1996 | 73% |
| 2.0 | 2722 | 599 | 22% |

## Entry rules, threshold 1.0, target from entry 0.8, stop 1.0 rv, 15 min

| rule | n | net maker bps | net stress bps | hit | stop | after extreme | s since extreme | equity/yr |
|---|---|---|---|---|---|---|---|---|
| fixed_120 | 780 | -69.2 | -102.4 | 57% | 28% | 74% | +43 | -30.7% |
| fixed_180 | 810 | -67.3 | -99.3 | 57% | 25% | 86% | +105 | -23.2% |
| fixed_30 | 576 | -78.5 | -121.6 | 55% | 37% | 37% | -11 | -65.0% |
| fixed_300 | 827 | -32.8 | -63.7 | 55% | 21% | 96% | +231 | -22.5% |
| fixed_450 | 820 | +69.3 | +38.5 | 53% | 23% | 98% | +382 | -17.4% |
| fixed_60 | 695 | -94.1 | -130.7 | 51% | 37% | 46% | -5 | -50.7% |
| fixed_600 | 836 | +5.0 | -25.2 | 54% | 23% | 100% | +537 | -24.9% |
| fixed_900 | 829 | -36.3 | -66.6 | 48% | 24% | 100% | +839 | -32.6% |
| liqend_10 | 925 | +132.6 | +103.0 | 73% | 14% | 95% | +12 | +38.2% |
| liqend_20 | 851 | +102.8 | +71.6 | 69% | 15% | 97% | +22 | +16.4% |
| liqend_30 | 780 | +91.0 | +57.9 | 66% | 16% | 98% | +31 | +1.7% |
| liqend_45 | 723 | +65.6 | +30.9 | 62% | 17% | 99% | +47 | -11.1% |
| liqend_5 | 1027 | +131.5 | +103.6 | 77% | 13% | 93% | +7 | +61.8% |
| liqend_60 | 737 | +47.0 | +12.5 | 60% | 19% | 100% | +62 | -16.1% |
| quiet_120 | 742 | +8.1 | -26.3 | 58% | 17% | 99% | +120 | -20.8% |
| quiet_15 | 551 | -89.3 | -132.4 | 59% | 30% | 54% | +15 | -29.8% |
| quiet_180 | 736 | -1.0 | -34.8 | 56% | 18% | 100% | +180 | -20.1% |
| quiet_30 | 631 | -86.9 | -125.9 | 58% | 26% | 69% | +30 | -26.6% |
| quiet_300 | 651 | -25.0 | -62.1 | 54% | 18% | 100% | +300 | -22.5% |
| quiet_60 | 734 | -79.9 | -114.2 | 59% | 22% | 90% | +60 | -26.9% |
| reclaim_0.2 | 525 | -72.4 | -118.2 | 56% | 35% | 47% | -5 | -64.6% |
| reclaim_0.3 | 512 | -70.7 | -117.6 | 60% | 29% | 60% | +4 | -52.0% |
| reclaim_0.5 | 448 | -83.3 | -135.9 | 64% | 18% | 79% | +17 | -27.2% |

## Before and after the true extreme

The realizable rules lose on the entries that land before the extreme and roughly break even on the rest; the rule cannot tell the two apart from price alone.

| rule | after extreme | n | net maker bps | net stress bps | hit | stop | after extreme | s since extreme | equity/yr |
|---|---|---|---|---|---|---|---|---|---|
| fixed_120 | False | 204 | -272.4 | -360.8 | 29% | 63% | 0% | -51 | -27.1% |
| fixed_120 | True | 576 | +2.8 | -10.9 | 67% | 16% | 100% | +62 | -3.6% |
| fixed_300 | False | 30 | -625.3 | -1148.9 | 40% | 53% | 0% | -164 | -5.0% |
| fixed_300 | True | 797 | -10.5 | -22.8 | 56% | 20% | 100% | +233 | -17.5% |
| fixed_60 | False | 373 | -183.2 | -238.9 | 35% | 59% | 0% | -37 | -53.9% |
| fixed_60 | True | 322 | +9.2 | -5.2 | 71% | 12% | 100% | +27 | +3.1% |
| quiet_120 | False | 10 | -847.1 | -1103.3 | 50% | 50% | 0% | -157 | -3.4% |
| quiet_120 | True | 732 | +19.8 | -11.6 | 58% | 16% | 100% | +120 | -17.4% |
| quiet_15 | False | 256 | -237.3 | -311.8 | 37% | 55% | 0% | -44 | -42.4% |
| quiet_15 | True | 295 | +39.1 | +23.4 | 78% | 9% | 100% | +15 | +12.7% |
| quiet_30 | False | 194 | -337.1 | -428.5 | 34% | 56% | 0% | -40 | -30.5% |
| quiet_30 | True | 437 | +24.1 | +8.4 | 69% | 13% | 100% | +30 | +3.9% |
| reclaim_0.3 | False | 207 | -219.9 | -314.4 | 36% | 57% | 0% | -52 | -62.2% |
| reclaim_0.3 | True | 305 | +30.6 | +16.0 | 77% | 9% | 100% | +11 | +10.3% |

## Entries N seconds after the last liquidation, the upper bound for the exhaustion rule

Hindsight for the last print, realizable for everything else. The exhaustion rule's `liq_decay_frac` and `no_new_extreme_s` are the live approximation.

| rule | n | net maker bps | net stress bps | hit | stop | after extreme | s since extreme | equity/yr |
|---|---|---|---|---|---|---|---|---|
| liqend_10 | 925 | +132.6 | +103.0 | 73% | 14% | 95% | +12 | +38.2% |
| liqend_20 | 851 | +102.8 | +71.6 | 69% | 15% | 97% | +22 | +16.4% |
| liqend_30 | 780 | +91.0 | +57.9 | 66% | 16% | 98% | +31 | +1.7% |
| liqend_45 | 723 | +65.6 | +30.9 | 62% | 17% | 99% | +47 | -11.1% |
| liqend_5 | 1027 | +131.5 | +103.6 | 77% | 13% | 93% | +7 | +61.8% |
| liqend_60 | 737 | +47.0 | +12.5 | 60% | 19% | 100% | +62 | -16.1% |

| rule | tier | n | net maker bps | net stress bps | hit | stop | after extreme | s since extreme | equity/yr |
|---|---|---|---|---|---|---|---|---|---|
| liqend_10 | core | 513 | +19.5 | +11.5 | 70% | 17% | 96% | +12 | +13.2% |
| liqend_10 | major | 374 | +265.9 | +221.7 | 77% | 9% | 94% | +12 | +22.7% |
| liqend_10 | minor | 38 | +347.4 | +170.1 | 68% | 13% | 89% | +10 | +2.4% |
| liqend_20 | core | 465 | +10.4 | +2.2 | 65% | 18% | 97% | +22 | +2.7% |
| liqend_20 | major | 348 | +206.1 | +160.0 | 75% | 10% | 97% | +22 | +12.8% |
| liqend_20 | minor | 38 | +286.9 | +111.1 | 71% | 13% | 95% | +20 | +0.9% |

| rule | btc cascading | n | net maker bps | net stress bps | hit | stop | after extreme | s since extreme | equity/yr |
|---|---|---|---|---|---|---|---|---|---|
| liqend_10 | False | 407 | +15.8 | +3.2 | 75% | 10% | 92% | +11 | +13.1% |
| liqend_10 | True | 518 | +224.3 | +181.5 | 71% | 17% | 97% | +12 | +25.1% |
| liqend_20 | False | 374 | +5.0 | -7.8 | 73% | 10% | 95% | +21 | +5.9% |
| liqend_20 | True | 477 | +179.4 | +133.9 | 66% | 18% | 98% | +22 | +10.5% |

## Exits for the liqend_10 entry

| target from | k_target | k_stop | time stop s | n | net maker bps | net stress bps | hit | stop | after extreme | s since extreme | equity/yr |
|---|---|---|---|---|---|---|---|---|---|---|---|
| entry | 0.5 | 1.0 | 900 | 675 | +138.4 | +100.6 | 80% | 10% | 95% | +12 | +29.0% |
| entry | 0.5 | 1.0 | 1800 | 675 | +132.1 | +94.3 | 81% | 12% | 95% | +12 | +28.0% |
| entry | 0.5 | 1.0 | 3600 | 675 | +133.6 | +95.8 | 82% | 13% | 95% | +12 | +29.3% |
| entry | 0.5 | 1.5 | 900 | 675 | +136.4 | +98.6 | 80% | 7% | 95% | +12 | +21.6% |
| entry | 0.5 | 1.5 | 1800 | 675 | +126.8 | +89.0 | 82% | 10% | 95% | +12 | +19.6% |
| entry | 0.5 | 1.5 | 3600 | 675 | +129.0 | +91.2 | 84% | 11% | 95% | +12 | +21.2% |
| entry | 0.8 | 1.0 | 900 | 925 | +132.6 | +103.0 | 73% | 14% | 95% | +12 | +38.2% |
| entry | 0.8 | 1.0 | 1800 | 925 | +154.9 | +125.3 | 73% | 19% | 95% | +12 | +36.0% |
| entry | 0.8 | 1.0 | 3600 | 925 | +157.0 | +127.4 | 74% | 20% | 95% | +12 | +38.2% |
| entry | 0.8 | 1.5 | 900 | 925 | +130.9 | +101.3 | 74% | 9% | 95% | +12 | +29.3% |
| entry | 0.8 | 1.5 | 1800 | 925 | +150.0 | +120.4 | 75% | 14% | 95% | +12 | +24.1% |
| entry | 0.8 | 1.5 | 3600 | 925 | +152.9 | +123.3 | 76% | 17% | 95% | +12 | +26.3% |
| extreme | 0.5 | 1.0 | 900 | 390 | +189.3 | +138.8 | 83% | 10% | 92% | +11 | +20.4% |
| extreme | 0.5 | 1.0 | 1800 | 390 | +189.5 | +138.9 | 84% | 12% | 92% | +11 | +20.3% |
| extreme | 0.5 | 1.0 | 3600 | 390 | +189.8 | +139.3 | 85% | 13% | 92% | +11 | +20.6% |
| extreme | 0.5 | 1.5 | 900 | 390 | +187.3 | +136.7 | 84% | 7% | 92% | +11 | +15.3% |
| extreme | 0.5 | 1.5 | 1800 | 390 | +187.3 | +136.7 | 85% | 9% | 92% | +11 | +15.1% |
| extreme | 0.5 | 1.5 | 3600 | 390 | +187.4 | +136.8 | 87% | 10% | 92% | +11 | +15.2% |
| extreme | 0.8 | 1.0 | 900 | 815 | +148.0 | +116.1 | 75% | 14% | 94% | +12 | +38.1% |
| extreme | 0.8 | 1.0 | 1800 | 815 | +170.7 | +138.8 | 75% | 18% | 94% | +12 | +35.3% |
| extreme | 0.8 | 1.0 | 3600 | 815 | +172.4 | +140.5 | 76% | 20% | 94% | +12 | +37.6% |
| extreme | 0.8 | 1.5 | 900 | 815 | +146.4 | +114.5 | 76% | 9% | 94% | +12 | +29.4% |
| extreme | 0.8 | 1.5 | 1800 | 815 | +165.8 | +133.9 | 76% | 14% | 94% | +12 | +24.0% |
| extreme | 0.8 | 1.5 | 3600 | 815 | +168.1 | +136.2 | 78% | 16% | 94% | +12 | +25.8% |

## Equity paths

| cell | trades | equity/yr | worst drawdown | one per cluster | its drawdown | months > 0 |
|---|---|---|---|---|---|---|
| liqend_10, all regimes (v2a's target) | 925 | +38.2% | -4.4% | +16.3% | -2.5% | 11/12 |
| liqend_10, isolated only (v2b's target) | 407 | +13.1% | -2.2% | +11.7% | -1.7% | 10/12 |
| liqend_20, all regimes (v2c's target) | 851 | +16.4% | -5.5% | +4.4% | -3.5% | 9/12 |
| liqend_10, k_target 0.5 | 675 | +29.0% | -2.2% | +12.8% | -1.5% | 11/12 |
| fixed_120 at threshold 2.0 (the v0 shape) | 183 | -10.0% | -14.9% | -4.4% | -4.7% | 4/12 |
| fixed_120 at threshold 1.0 | 780 | -30.7% | -36.8% | -12.8% | -13.9% | 3/12 |

## What this is not

- Not a backtest: no book, fills at the last trade, stops fill at their price with no slippage, the cluster cap and cooldowns are not applied except in the one-per-cluster column, and the detector's notional floor is timed by uniform accrual because the grid carries no liquidation stream.
- `liqend_N` knows which liquidation was the last. The exhaustion rule does not, and how often it fires on a mid-cascade lull is the number the v2 backtests measure: a clean after-extreme entry runs a 10 to 15% stop-out share here.
