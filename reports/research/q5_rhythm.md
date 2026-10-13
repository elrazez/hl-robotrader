# Q5: the partial liquidation rhythm

Generated 2026-09-05 18:04 UTC from the Parquet lake.

Window: 2025-09-01 to 2026-08-30. Qualifying episodes: **2722**, gaps between consecutive liquidations inside them: **1680604**. Only gaps under 60s appear, since a longer one would have ended the episode.

This question is about timing, not money, so it carries no cost columns. Nothing in it can produce an edge on its own; a yes gives the exhaustion rule something to wait for and a no says the rule has to read flow instead.

## Gaps between consecutive liquidations

| gap | events | share |  |
|---|---|---|---|
| 0 to 5s | 1667157 | 99.2% | ############################## |
| 5 to 10s | 5979 | 0.4% |  |
| 10 to 15s | 1990 | 0.1% |  |
| 15 to 20s | 1470 | 0.1% |  |
| 20 to 25s | 936 | 0.1% |  |
| 25 to 30s | 716 | 0.0% |  |
| 30 to 35s | 626 | 0.0% |  |
| 35 to 40s | 490 | 0.0% |  |
| 40 to 45s | 359 | 0.0% |  |
| 45 to 50s | 329 | 0.0% |  |
| 50 to 55s | 283 | 0.0% |  |
| 55 to 60s | 268 | 0.0% |  |

## DECISION: no

No mass at the documented cooldown: liquidation timing inside an episode looks continuous, so there is no rhythm to wait out and exhaustion has to be read from flow rather than from the clock. The 25 to 35s bins hold 1342 gaps against a neighbouring mean of 814, a lift of 1.65x against a bar of 2x. 1680604 gaps from 2722 episodes. 2722 episodes, 1758 clusters, 12 calendar months.
