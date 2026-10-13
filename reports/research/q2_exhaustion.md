# Q2: exhaustion timing against a fixed delay

Generated 2026-09-05 18:37 UTC from the Parquet lake.

Every table below carries three columns: gross, net-9bps (0.09% round trip) and
net-stress (that fee, entry one second after the extreme, and one effective
spread per side). The DECISION reads net-stress and nothing else. Funding is
ignored: at a 1 to 60 minute hold it is under a tenth of a basis point.

Window: 2025-09-01 to 2026-08-30. Qualifying episodes: **2722**. The exhaustion rule fired on **2655** of them. Every row exits 15 minutes after its own entry, so the comparison is of entry timing and nothing else.

The book-imbalance arm of the flow-flip condition is unavailable: the historical year has no bbo, so only the CVD slope arm is evaluated. That makes the rule strictly harder to satisfy here than it will be live.

## Net-stress PnL by entry rule, per month

| entry rule | episodes | net-stress bps |
|---|---|---|
| exhaustion | 210 | -27.5 |
| fixed 30s | 214 | -49.3 |
| fixed 60s | 214 | -28.2 |
| fixed 120s | 214 | -23.3 |
| exhaustion | 426 | -237.3 |
| fixed 30s | 438 | -288.4 |
| fixed 60s | 438 | -256.9 |
| fixed 120s | 438 | -233.9 |
| exhaustion | 366 | -22.5 |
| fixed 30s | 375 | -39.5 |
| fixed 60s | 375 | -23.8 |
| fixed 120s | 375 | +1.2 |
| exhaustion | 203 | -22.0 |
| fixed 30s | 211 | -37.1 |
| fixed 60s | 211 | -25.8 |
| fixed 120s | 211 | -10.3 |
| exhaustion | 276 | -29.0 |
| fixed 30s | 283 | -37.6 |
| fixed 60s | 283 | -26.8 |
| fixed 120s | 283 | -17.9 |
| exhaustion | 174 | -37.1 |
| fixed 30s | 175 | -57.7 |
| fixed 60s | 175 | -39.7 |
| fixed 120s | 175 | -25.0 |
| exhaustion | 145 | -11.8 |
| fixed 30s | 147 | -30.3 |
| fixed 60s | 147 | -14.3 |
| fixed 120s | 147 | -3.0 |
| exhaustion | 94 | -21.0 |
| fixed 30s | 96 | -26.2 |
| fixed 60s | 96 | -20.2 |
| fixed 120s | 96 | -11.4 |
| exhaustion | 178 | -20.6 |
| fixed 30s | 183 | -33.2 |
| fixed 60s | 183 | -23.0 |
| fixed 120s | 183 | -10.7 |
| exhaustion | 297 | -39.0 |
| fixed 30s | 302 | -55.8 |
| fixed 60s | 302 | -35.7 |
| fixed 120s | 302 | -10.2 |
| exhaustion | 85 | -28.9 |
| fixed 30s | 87 | -39.9 |
| fixed 60s | 87 | -27.9 |
| fixed 120s | 87 | -14.0 |
| exhaustion | 200 | -26.4 |
| fixed 30s | 209 | -102.4 |
| fixed 60s | 209 | -41.7 |
| fixed 120s | 209 | +33.8 |

## DECISION: no

A flat delay beats the rule: fixed 120s carries the most net-stress PnL. The rule fired on 2655 of 2722 episodes. Phase 4 should enter on the delay rather than on the rule, or the rule needs rewriting before it earns its complexity. 2722 episodes, 1758 clusters, 12 calendar months.
