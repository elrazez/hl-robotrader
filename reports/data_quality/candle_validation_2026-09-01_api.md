# Candle validation

Day: 2026-09-01. Ours: `tardis`. Reference: `api`.

Minutes: 5,760 ours, 4,080 reference, 4,080 shared.

`o/h/l/c` and `n` are exact equality; `v` is equal to within a relative 1e-09. The last column is the one that says something about the derivation: on a minute where the two sides disagree on `n`, one of them missed a trade, and `o` and `c` can differ for that reason alone.

| coin | minutes | o | h | l | c | v | n | o/h/l/c where n agrees |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| BTC | 1,059 | 100.00% | 100.00% | 99.53% | 100.00% | 99.34% | 99.34% | 99.71% of 1,052 |
| ETH | 1,056 | 100.00% | 100.00% | 99.91% | 100.00% | 99.62% | 99.62% | 99.90% of 1,052 |
| HYPE | 918 | 100.00% | 100.00% | 99.78% | 99.89% | 99.24% | 99.24% | 99.78% of 911 |
| SOL | 1,047 | 100.00% | 99.90% | 99.33% | 100.00% | 99.62% | 99.62% | 99.42% of 1,043 |
