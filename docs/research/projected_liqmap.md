# The projected liquidation map

Where the real map (`hl_positions`, Q9) records liquidation prices the venue computed for
accounts we polled, this one *derives* them from price alone. That is the whole difference
and it is why this one can be backtested over the year already in the lake while the other
has to wait weeks for the recorder.

Written so the next person does not re-derive the arithmetic, the decode or the verdicts.

## Where the idea comes from

A public TradingView indicator, **"Tomo's Flat Fibs"**, draws lines at fixed dollar offsets
below the last pivot high and above the last pivot low:

    600, 1000, 1400, 1900, 2700, 3400, 4000, 4800

Those numbers are hardcoded for Bitcoin at roughly $40,000, where the indicator was written
in April 2022. Converted to percentages of that price and read through the liquidation
formula `L = 1 / (d + m)` at a maintenance margin `m` of 0.5%:

| offset | distance | implied leverage |
| --- | --- | --- |
| 600 | 1.50% | 50x |
| 1000 | 2.50% | 33x |
| 1400 | 3.50% | 25x |
| 1900 | 4.75% | 19x |
| 2700 | 6.75% | 14x |
| 3400 | 8.50% | 11x |
| 4000 | 10.0% | 9.5x |
| 4800 | 12.0% | 8x |

**That is the exchange leverage menu.** The indicator is not drawing Fibonacci levels. It is
drawing the liquidation ladder of everyone who entered at the swing extreme, and it is doing
it in dollars, which is why it only works on one coin at one price era. Stored as
percentages it works on any coin at any price, which is what `liqfade/features/liqmap.py`
does.

The furthest line, 12%, is also where `features.liqmap.max_dist_bps` is set: 1200 bps. That
is not a coincidence and the config comment says so.

## The arithmetic

A trader who opens at price `P` with leverage `L` is liquidated when the loss eats the
margin down to the maintenance requirement `m`, which is a fixed fraction of notional away:

    d = 1/L - m

So a swing **high** traps longs and projects bands **below** it at `P * (1 - d)`, where they
are force-sold. A swing **low** traps shorts and projects bands **above** it at `P * (1 + d)`,
where they are force-bought. `ladder_bands` is those two lines and nothing else.

The rungs and their distances at `mm_frac = 0.005`:

| leverage | distance | bps | inside the 1200 bps cap |
| --- | --- | --- | --- |
| 3x | 32.83% | 3283 | no |
| 5x | 19.50% | 1950 | no |
| 10x | 9.50% | 950 | yes |
| 15x | 6.17% | 617 | yes |
| 20x | 4.50% | 450 | yes |
| 25x | 3.50% | 350 | yes |
| 40x | 2.00% | 200 | yes |
| 50x | 1.50% | 150 | yes |

3x and 5x stay in the configured ladder so the menu is written down in full rather than
pre-filtered by one cap's value, but at the shipped cap they never project a band.

**One flat maintenance margin.** Hyperliquid's real rule is half the initial margin at the
asset's own max leverage, so the exact value is per coin. At these rungs it shifts a band by
well under a tenth of the gap to the next rung, so `liqmap.py` carries a scalar and a
`ponytail:` comment naming the per-coin upgrade path. No per-coin table is built.

## The leverage histogram, measured rather than invented

Weighting the rungs needs a distribution of leverage, and **`hl_positions` is the only free
source of one anywhere**. Binance, Bybit and OKX publish open interest and funding and never
leverage. From 194 addresses and $1,561M of tracked notional on the VPS on 2026-09-09:

| leverage bucket | share of notional |
| --- | --- |
| under 3x | 5.2% |
| 3 to 5x | 7.2% |
| 5 to 10x | 22.3% |
| 10 to 20x | 32.1% |
| 20 to 25x | 17.5% |
| 25 to 40x | 15.8% |
| 40x and up | 0% |

One day, 194 addresses, one venue. `config/default.yaml` gives each rung the share of the
bucket containing it, splitting 10-20x across its two rungs; the missing 5.1% is the
under-3x bucket, which projects no rung at all.

**This histogram is the secondary, not the primary.** Q10's preregistration fixes uniform
rung weights as the headline and the open-interest-scaled histogram version as one
preregistered secondary, run once. If the dumb version works the clever one is never needed,
and a weighting scheme tuned until a table passes is exactly the failure mode the
preregistration exists to prevent.

## What is being claimed, and what it has to beat

Three claims that can fail independently: bands mark where price reacts (Q10a), where
cascades end (Q10b), and when forced flow is spent (Q10c).

**Q7 already tested the neighbouring claim and it failed.** Cascades ending within 15 bps of
a raw swing level reverted **+36.1 bps** net-stress against **+146.3 bps** for cascades
ending nowhere near one, over 2,722 episodes: the opposite of the expected sign. Q10 is a
different mechanism, since the level seeds a projected band some distance away rather than
acting as the target itself, but it inherits that burden of proof, and Q7's rows are printed
beside Q10b's so the two are always read together.

The preregistration is `reports/research/PREREGISTRATION_Q10.md`, committed before any
script read the lake.

## Verdicts

_Pending the run. `scripts/research/q10_ladder.py` writes `reports/research/q10_ladder.md`;
the four verdicts and their sample sizes are transcribed here when it has run over the year._

## Relationship to the real map

| | real map (Q9) | projected map (Q10) |
| --- | --- | --- |
| source | `hl_positions`, polled from `clearinghouseState` | `candles_1m` alone |
| available | forward only, from 2026-09-10 | the whole year in the lake |
| covers | the addresses we poll | every trader who entered at an extreme |
| knows | the venue's own `liquidation_px` | a distribution over the leverage menu |
| blind to | anyone we do not poll, and 31% of positions carry no level at all | who actually holds anything |

They answer the same question from opposite ends and Q10c is graded on the same bars as
Q9b deliberately, so the two can be compared rather than argued about.
