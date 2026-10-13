# Q4: cross-venue cascades that Hyperliquid did not see

Generated 2026-09-05 18:50 UTC from the Parquet lake.

Every table below carries three columns: gross, net-9bps (0.09% round trip) and
net-stress (that fee, entry one second after the extreme, and one effective
spread per side). The DECISION reads net-stress and nothing else. Funding is
ignored: at a 1 to 60 minute hold it is under a tenth of a basis point.

Window: 2025-09-01 to 2026-08-30.

## Coincidence between the two venues

| measure | count |
|---|---|
| Bybit episodes clearing both floors | 179 |
| of those, matched by an HL episode within 120s | 35 |
| orphans, Bybit only | 144 |
| coincidence rate | 20% |
| HL qualifying episodes over the same window | 2722 |

Reversion on an orphan is measured on Hyperliquid's own tape with Q1's measure, net-stress, so a yes here would mean a Bybit-only cascade is tradeable on HL.

## DECISION: inconclusive

144 orphaned Bybit cascades in 102 clusters over 12 days of cross-venue data, against the reduced bar of 15 clusters **and** 60 recorder days. The recorder has not run at all, so every cross-venue row here is one of the twelve Tardis first-of-month days. Whether this blocks Phase 4 is Q3's trigger branch to decide, and this report does not decide it. What the sample does say: 20% of Bybit cascades coincide with an HL episode, so the two venues are often independent.
