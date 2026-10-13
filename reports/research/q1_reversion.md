# Q1: reversion after an exact liquidation cascade

Generated 2026-09-05 16:59 UTC from the Parquet lake.

Every table below carries three columns: gross, net-9bps (0.09% round trip) and
net-stress (that fee, entry one second after the extreme, and one effective
spread per side). The DECISION reads net-stress and nothing else. Funding is
ignored: at a 1 to 60 minute hold it is under a tenth of a basis point.

Window: 2025-09-01 to 2026-08-30. Episodes detected: **80057**, of which **2722** qualify (the trailing p95 or z rule, and both absolute floors). Clusters: **1758**.

## Episodes by notional, before any reversion number is read

| episode notional | episodes | net-stress bps at headline |
|---|---|---|
| $0.0M to $0.5M | 417 | +47.2 |
| $0.5M to $1.0M | 630 | +41.7 |
| $1.0M to $5.0M | 954 | +50.8 |
| $5.0M to $20.0M | 545 | +16.4 |
| $20M+ | 176 | +720.9 |

## Reversion by horizon

| horizon | gross bps | net-9bps | net-stress bps | net-stress median |
|---|---|---|---|---|
| +1m | +102.0 [+74.4, +144.6] (n=2722, clusters=1758) | +93.0 [+65.4, +135.6] (n=2722, clusters=1758) | +33.2 [+22.6, +43.9] (n=2722, clusters=1758) | +15.1 |
| +5m | +134.7 [+73.3, +270.0] (n=2722, clusters=1758) | +125.7 [+64.3, +261.0] (n=2722, clusters=1758) | +64.8 [+26.3, +148.9] (n=2722, clusters=1758) | +21.7 |
| +15m (headline) | +155.0 [+77.2, +282.3] (n=2720, clusters=1757) | +146.0 [+68.2, +273.3] (n=2720, clusters=1757) | +84.6 [+25.1, +180.0] (n=2720, clusters=1757) | +28.7 |
| +30m | +144.1 [+26.6, +314.0] (n=2716, clusters=1755) | +135.1 [+17.6, +305.0] (n=2716, clusters=1755) | +74.1 [-27.1, +207.7] (n=2716, clusters=1755) | +34.5 |
| +60m | +169.5 [+76.1, +321.3] (n=2713, clusters=1753) | +160.5 [+67.1, +312.3] (n=2713, clusters=1753) | +98.7 [+20.5, +218.6] (n=2713, clusters=1753) | +41.9 |

## By coin tier, at the headline horizon

| tier | episodes | clusters | net-stress bps |
|---|---|---|---|
| core | 1556 | 1110 | +27.3 |
| major | 1061 | 797 | +164.9 |
| minor | 105 | 93 | +123.2 |

## Robustness, judged on point estimates

| slice | episodes | clusters | net-stress bps |
|---|---|---|---|
| 2025 Q3 | 214 | 126 | +62.2 |
| 2025 Q4 | 1024 | 617 | +141.5 |
| 2026 Q1 | 605 | 388 | +34.1 |
| 2026 Q2 | 581 | 413 | +41.1 |
| 2026 Q3 | 298 | 214 | +92.8 |
| excluding BTC and ETH | 1940 | 1383 | +111.4 |
| excluding HYPE | 2323 | 1487 | +92.3 |
| isolated coin (BTC quiet) | 1544 | 1232 | +26.7 |
| market-wide sweep | 1178 | 537 | +160.4 |
| top 5 clusters removed | 2682 | 1753 | +34.0 |
| used spread <= 30 bps | 2602 | 1736 | +32.2 |

## Capacity

Capacity-weighted edge: **$41,112 per episode** at 25% participation, **$111,905,595** over the sample's 2722 episodes. The stop rule is $2,000 a year, roughly five times infrastructure plus margin.

## DECISION: yes

Mean net-stress at +15m is +84.6 [+25.1, +180.0] (n=2720, clusters=1757); with the top five clusters removed it is +34.0 bps. Sample: 2722 episodes, 1758 clusters, 12 calendar months.
