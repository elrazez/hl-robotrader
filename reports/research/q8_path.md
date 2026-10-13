# Q8: path, maximum adverse excursion and the stop/target grid

Generated 2026-09-05 17:02 UTC from the Parquet lake.

Every table below carries three columns: gross, net-9bps (0.09% round trip) and
net-stress (that fee, entry one second after the extreme, and one effective
spread per side). The DECISION reads net-stress and nothing else. Funding is
ignored: at a 1 to 60 minute hold it is under a tenth of a basis point.

Window: 2025-09-01 to 2026-08-30. Qualifying episodes: **2722**, clusters: **1758**. Prints read: **8646309**. Entry is the first print after `extreme_ts + 1s`; the time stop is 15 minutes.

## Maximum adverse excursion from entry

How far the trade goes against you before the time stop, whatever the exit rule. A stop inside the p50 is a stop that fires on half the winners.

| quantile | MAE bps | MAE in rv_5m |
|---|---|---|
| p25 | 1.5 | 0.02 |
| p50 | 20.2 | 0.25 |
| p75 | 74.4 | 1.00 |
| p90 | 177.4 | 2.28 |
| p95 | 288.1 | 3.21 |

## The stop and target grid

Hold to the 15 minute horizon with no stop and no target is **+84.6 bps** net-stress, and is reported as the reference rather than as a bar: a stop cannot beat no-stop in expectation on a reverting sample. A cell has to be positive, stop out less than 50% of the time, and have positive neighbours.

| k_stop | k_target | gross bps | net-9bps | net-stress bps | stop rate | hit/stop/timeout |
|---|---|---|---|---|---|---|
| 1 | 0.3 | +76.7 | +67.7 | +8.4 [-10.2, +38.2] (n=2722, clusters=1758) | 2% | 2665/48/9 |
| 1 | 0.5 | +119.0 | +110.0 | +49.6 [+13.0, +117.4] (n=2722, clusters=1758) | 7% | 2443/183/96 |
| 1 | 0.8 | +151.2 | +142.2 | +80.6 [+27.7, +176.0] (n=2722, clusters=1758) | 15% | 1812/403/507 |
| 1.5 | 0.3 | +76.7 | +67.7 | +8.4 [-10.2, +38.3] (n=2722, clusters=1758) | 1% | 2671/32/19 |
| 1.5 | 0.5 | +118.4 | +109.4 | +49.0 [+12.2, +116.7] (n=2722, clusters=1758) | 5% | 2457/135/130 |
| 1.5 | 0.8 | +150.1 | +141.1 | +79.5 [+26.7, +175.2] (n=2722, clusters=1758) | 10% | 1826/280/616 |
| 2.5 | 0.3 | +76.4 | +67.4 | +8.1 [-10.7, +37.9] (n=2722, clusters=1758) | 1% | 2673/20/29 |
| 2.5 | 0.5 | +117.8 | +108.8 | +48.3 [+11.5, +116.0] (n=2722, clusters=1758) | 3% | 2463/76/183 |
| 2.5 | 0.8 | +149.0 | +140.0 | +78.4 [+25.1, +174.2] (n=2722, clusters=1758) | 5% | 1839/145/738 |
| none | 0.3 | +76.6 | +67.6 | +8.3 [-10.3, +38.2] (n=2722, clusters=1758) | 0% | 2676/0/46 |
| none | 0.5 | +118.9 | +109.9 | +49.5 [+12.7, +116.4] (n=2722, clusters=1758) | 0% | 2468/0/254 |
| none | 0.8 | +151.1 | +142.1 | +80.5 [+27.7, +176.6] (n=2722, clusters=1758) | 0% | 1845/0/877 |

## DECISION: yes

Yes with a stop. `k_stop=1`, `k_target=0.8` keeps +80.6 of +84.6 bps at 15% stop-outs, with every neighbouring cell positive. A stop is insurance: it is not expected to beat holding on a reverting sample, it is expected to cost little while bounding the loss on the ones that do not revert. These are the values the config writeback takes. 2722 episodes, 1758 clusters, 12 calendar months.
