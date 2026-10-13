# Q7: supply and demand zone confluence

Generated 2026-09-05 18:24 UTC from the Parquet lake.

Every table below carries three columns: gross, net-9bps (0.09% round trip) and
net-stress (that fee, entry one second after the extreme, and one effective
spread per side). The DECISION reads net-stress and nothing else. Funding is
ignored: at a 1 to 60 minute hold it is under a tenth of a basis point.

Window: 2025-09-01 to 2026-08-30. Qualifying episodes: **2722**. Levels are fractal swings plus rolling and prior-period range edges, each usable only from the bar that confirms it. Every zone question is asked as of the cascade start, so a level the cascade itself created is invisible.

## In zone against out of zone, by zone width

The loss tail here is the worst quartile by net-stress, not Q8's stopped-out set: this question is about where a cascade ends, and Q8 owns the stop grid.

| zone width | in zone, net-stress | out of zone, net-stress | hit rate in | hit rate out | loss tail in | loss tail out |
|---|---|---|---|---|---|---|
| 10 bps | +34.5 [+17.3, +55.9] (n=1431, clusters=1003) | +140.3 [+22.2, +337.6] (n=1289, clusters=893) | 62% | 67% | 25% | 25% |
| 15 bps | +36.1 [+19.1, +57.7] (n=1523, clusters=1052) | +146.3 [+22.6, +367.1] (n=1197, clusters=833) | 62% | 67% | 25% | 25% |
| 25 bps | +37.9 [+20.0, +61.6] (n=1635, clusters=1126) | +155.1 [+21.0, +388.9] (n=1085, clusters=757) | 62% | 67% | 25% | 25% |

## The reverse use: does room predict extension?

If a cascade that starts far from any level runs further, distance to the nearest level is a stop-placement input even when it is not an entry filter.

| room to the nearest level at start | episodes | mean move bps |
|---|---|---|
| under 25 bps of room | 2108 | 158.6 |
| 25 to 75 bps | 305 | 182.0 |
| 75 to 150 bps | 144 | 177.4 |
| over 150 bps | 47 | 398.2 |

## DECISION: no

No zone width separates the two intervals. The zone code stays in the repo and stays off: it is built, tested and unused, which is cheaper than deleting it and rebuilding it in Phase 7 if the picture changes. 2722 episodes, 1758 clusters, 12 calendar months.
