# Q3: forced flow proxy calibration against exact S3 liquidations

Generated 2026-09-05 19:50 UTC from the Parquet lake.

Every table below carries three columns: gross, net-9bps (0.09% round trip) and
net-stress (that fee, entry one second after the extreme, and one effective
spread per side). The DECISION reads net-stress and nothing else. Funding is
ignored: at a 1 to 60 minute hold it is under a tenth of a basis point.

Window: 2025-09-01 to 2026-08-30. Qualifying episodes: **2722**. A trigger is a run of ticks at or over the threshold, collapsed to its first tick, and matches an episode on the same coin and side within 120s. The proxy window is 60s.

## Three-gate proxy, the headline

Volume ratio, gated by falling open interest and mark-versus-mid over its trailing p95, both read from the archive-backed `asset_ctx`. A null gate blocks.

| threshold | tier | precision | recall | F1 | matched/triggers | episodes |
|---|---|---|---|---|---|---|
| 2 | core+major | 0.00 | 0.68 | 0.01 | 1806/429227 | 2617 |
| 2 | core | 0.01 | 0.68 | 0.02 | 1062/86108 | 1556 |
| 2 | major | 0.00 | 0.70 | 0.00 | 744/343119 | 1061 |
| 2 | minor | 0.00 | 0.69 | 0.00 | 71/262318 | 105 |
| 3 | core+major | 0.00 | 0.68 | 0.01 | 1799/383548 | 2617 |
| 3 | core | 0.01 | 0.67 | 0.03 | 1052/74055 | 1556 |
| 3 | major | 0.00 | 0.70 | 0.00 | 747/309493 | 1061 |
| 3 | minor | 0.00 | 0.69 | 0.00 | 71/239708 | 105 |
| 4 | core+major | 0.01 | 0.68 | 0.01 | 1791/350159 | 2617 |
| 4 | core | 0.02 | 0.67 | 0.03 | 1046/65476 | 1556 |
| 4 | major | 0.00 | 0.70 | 0.01 | 745/284683 | 1061 |
| 4 | minor | 0.00 | 0.69 | 0.00 | 71/224206 | 105 |
| 6 | core+major | 0.01 | 0.67 | 0.01 | 1758/303490 | 2617 |
| 6 | core | 0.02 | 0.65 | 0.04 | 1023/53188 | 1556 |
| 6 | major | 0.00 | 0.69 | 0.01 | 735/250302 | 1061 |
| 6 | minor | 0.00 | 0.70 | 0.00 | 72/203232 | 105 |

## Trades-only variant, the gate lift

The same volume ratio with both gates off, on the same window. The difference is what the gates buy. This table carries no DECISION of its own.

| threshold | tier | precision | recall | F1 | matched/triggers | episodes |
|---|---|---|---|---|---|---|
| 2 | core+major | 0.00 | 0.30 | 0.00 | 777/2232125 | 2617 |
| 2 | core | 0.00 | 0.30 | 0.00 | 460/424425 | 1556 |
| 2 | major | 0.00 | 0.30 | 0.00 | 317/1807700 | 1061 |
| 2 | minor | 0.00 | 0.30 | 0.00 | 32/1730328 | 105 |
| 3 | core+major | 0.00 | 0.41 | 0.00 | 1076/2211152 | 2617 |
| 3 | core | 0.00 | 0.42 | 0.00 | 655/405641 | 1556 |
| 3 | major | 0.00 | 0.40 | 0.00 | 421/1805511 | 1061 |
| 3 | minor | 0.00 | 0.40 | 0.00 | 42/1671869 | 105 |
| 4 | core+major | 0.00 | 0.48 | 0.00 | 1260/2119895 | 2617 |
| 4 | core | 0.00 | 0.49 | 0.00 | 764/372759 | 1556 |
| 4 | major | 0.00 | 0.47 | 0.00 | 496/1747136 | 1061 |
| 4 | minor | 0.00 | 0.47 | 0.00 | 49/1571196 | 105 |
| 6 | core+major | 0.00 | 0.58 | 0.00 | 1513/1928475 | 2617 |
| 6 | core | 0.00 | 0.59 | 0.01 | 919/310167 | 1556 |
| 6 | major | 0.00 | 0.56 | 0.00 | 594/1618308 | 1061 |
| 6 | minor | 0.00 | 0.59 | 0.00 | 62/1435276 | 105 |

## The trigger branch for Phase 4

No threshold clears precision 0.6 and recall 0.4 on core+major (best precision 0.01 at recall 0.67). **The v0 trigger is cross-venue**, on Bybit, the only complete feed, and Q4 becomes a Phase 4 entry requirement at the reduced bar of 15 clusters and 60 recorder days. Precision is also under 0.5, so if Q4 cannot be answered for months the project parks until the recorder has the data.

## DECISION: no

No threshold clears precision 0.6 and recall 0.4 on core+major (best precision 0.01 at recall 0.67). **The v0 trigger is cross-venue**, on Bybit, the only complete feed, and Q4 becomes a Phase 4 entry requirement at the reduced bar of 15 clusters and 60 recorder days. Precision is also under 0.5, so if Q4 cannot be answered for months the project parks until the recorder has the data.

Rerun both tables at 60 recorder days, where the gates read live block-level values instead of the archive's one-a-minute sampling.
