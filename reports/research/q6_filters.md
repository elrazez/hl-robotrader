# Q6: which filters remove the losing tail

Generated 2026-09-05 18:31 UTC from the Parquet lake.

Every table below carries three columns: gross, net-9bps (0.09% round trip) and
net-stress (that fee, entry one second after the extreme, and one effective
spread per side). The DECISION reads net-stress and nothing else. Funding is
ignored: at a 1 to 60 minute hold it is under a tenth of a basis point.

Window: 2025-09-01 to 2026-08-30. Qualifying episodes: **2722**, of which **135** are the loss tail, meaning Q8's configured stop took them out at `k_stop=1.5`, `k_target=0.5`, `time_stop_s=900`. The answer moves with those, so they are stated rather than assumed.

A filter earns its place by concentration, not by removing losses: removing a third of the sample and a third of the losses is 2x nothing.

## Candidate filters

| filter (blocks when true) | sample removed | loss tail removed | concentration | net-stress of what remains | verdict |
|---|---|---|---|---|---|
| spread over the tier cap | 1413 (51.9%) | 49 (36.3%) | 0.70x | +13.5 | drop |
| BTC itself cascading | 726 (26.7%) | 45 (33.3%) | 1.25x | +23.7 | drop |
| funding at an extreme against the trade | 16 (0.6%) | 0 (0.0%) | 0.00x | +85.0 | drop |
| listed under 14 days | 0 (0.0%) | 0 (0.0%) | 0.00x | +84.6 | drop |
| skip session asia 00-08 | 802 (29.5%) | 39 (28.9%) | 0.98x | +89.2 | drop |
| skip session europe 08-16 | 886 (32.5%) | 51 (37.8%) | 1.16x | +108.0 | drop |
| skip session americas 16-24 | 1034 (38.0%) | 45 (33.3%) | 0.88x | +54.0 | drop |

## DECISION: no

No candidate filter concentrates on the loss tail at 2x. Phase 4 ships with the risk gates and no signal filters, and any filter added later needs its own evidence. 2722 episodes, 1758 clusters, 12 calendar months.
