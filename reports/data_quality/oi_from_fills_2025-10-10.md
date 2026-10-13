# Open interest from fills, against the asset-context archive

Day: 2025-10-10. Ours is the exact change per minute computed from every fill's
`start_position`; the reference is the archive's own minute series, differenced.

**Gate: PASS.** `sides` is the archive's change divided
by the long-only sum, measured rather than assumed, and it has to be
2: Hyperliquid counts both sides of the book. `worst` has to be under a
relative 1e-06 of the biggest move. Both are in coin units.

| coin | minutes | correlation | sides | median abs diff | worst abs diff | biggest move |
|---|---:|---:|---:|---:|---:|---:|
| BTC | 1,439 | 1.000000 | 2.000000 | 2.55351e-12 | 2.00558e-10 | 7,454.4433 |
| ETH | 1,439 | 1.000000 | 2.000000 | 1.74585e-10 | 7.65669e-09 | 168,835.2506 |
| HYPE | 1,439 | 1.000000 | 2.000000 | 5.39853e-09 | 2.30037e-07 | 7,757,751.7400 |
| SOL | 1,439 | 1.000000 | 2.000000 | 1.18507e-09 | 1.81608e-08 | 1,991,461.3600 |
