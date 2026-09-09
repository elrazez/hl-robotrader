# Q10 preregistration: the projected liquidation map

Committed before any script in this branch read the real lake. Transcribed from
`prompts/05e_projected_liqmap.md`, prompt G, sections "The claim, stated so it can fail",
"Decisions already made" and the four question bars. `reports/research/q10_ladder.md` cites
this file so anyone reading a DECISION can check it against what was promised beforehand.

Any later change is a dated amendment in `docs/DECISIONS.md` stating what had already been
seen when the change was made. Nothing in this file may be edited after the first run.

## The question

**Does a ladder of projected liquidation prices, anchored on untouched swing extremes, mark
where price reacts, where cascades end, and when forced flow is spent?**

A trader who opened at a recent swing extreme with leverage `L` is liquidated a fixed
fraction away, `d = 1/L - m`, where `m` is the maintenance margin. So every untouched swing
high projects a ladder of prices below it where trapped longs are force-sold, and every
untouched swing low projects a ladder above it where trapped shorts are force-bought. Where
ladders from several different anchors land on the same price, that price is where forced
flow concentrates. The confluence count is the actual claim; a single band is not.

The idea is decoded from a public TradingView indicator that draws the same ladder in
dollars, hardcoded for Bitcoin at roughly $40,000. `docs/research/projected_liqmap.md`
records the decode. Stored as percentages the ladder works on any coin at any price.

## This question inherits Q7's burden of proof

**Q7 already tested a neighbouring claim and it failed.** Cascades ending within 15 bps of a
raw swing level reverted **+36.1 bps** net-stress against **+146.3 bps** for cascades ending
nowhere near one, over 2,722 episodes. That is the opposite of the expected sign, and it is
the result this question has to beat.

Q10 is a different mechanism: the level seeds a projected band some distance away rather
than acting as the target itself. But it is the same family of claim, tested on the same
episodes, and **it inherits the burden of proof**. Q7's corresponding row is printed beside
every row of Q10b's table so the two are read together and no reader has to go looking.

## This is not a re-cut of the episode definition

The episodes are Q7's episodes, unchanged: `_lib.build_episodes` with the shipped
`features.episodes` config, the same qualifying rule, the same floors. Nothing in the
episode detector, its floors or its universe rule is touched. **This question therefore
consumes neither of the two re-cuts the Phase 3 preregistration allows**, and no result
here may be labelled exploratory on that ground.

## Ground rules, inherited from `PREREGISTRATION.md`

- **No lookahead, mechanically enforced.** Every band carries the `known_from` of its
  anchor, unchanged, and every query filters on it. `map_features` has a property test that
  mutates every row at or after `t` and asserts the output at `t` is byte-identical.
- **Every number is net-stress**: 0.09% round trip, entry one second after the signal, one
  effective spread per side. Reported in the same three-column form Q7 uses: gross,
  net-9bps, net-stress. The DECISION reads net-stress and nothing else.
- **Q10a's entry is the close of the bar that touched the band**, not one second after it.
  Q10b and Q10c are episode questions and inherit the one-second delayed entry through
  `_lib.attach_outcomes` unchanged. Q10a is not: a year of band touches is hundreds of
  thousands of events and pricing every one at second resolution means re-reading the
  year's tape, which the research machine cannot do in one pass. The touching bar's close
  is up to 60 seconds later than the preregistered entry, so on a reverting move it
  captures **less**, and the substitution therefore biases against Q10a's own hypothesis.
  Written here before any script read the lake, not amended after.
- **Cluster bootstrap on every interval.** A cluster is every episode on the same side whose
  start falls within a 5-minute window, across all coins. The per-episode bootstrap is
  banned.
- **Top-five-cluster removal on every headline.** An effect that dies without its five
  biggest clusters is a lottery, not an edge.
- **Under 40 episodes in a cell is `inconclusive`, never a result.**

## Parameters, fixed here, not tuned later

- `features.liqmap.ladder: [3, 5, 10, 15, 20, 25, 40, 50]`. The exchange leverage menu, in
  multiples, never in dollars.
- `features.liqmap.mm_frac: 0.005`. One flat maintenance margin. Hyperliquid's real rule is
  half the initial margin at the asset's max leverage, so the exact value is per coin, but
  it shifts every band by well under a tenth of the gap between rungs. No per-coin table is
  built in this question.
- `features.liqmap.max_dist_bps: 1200`. Its own key, not `features.levels.max_dist_bps`,
  which is 200 and far too tight for a ladder whose far rungs sit 10% out. Changing the
  levels key would silently move Q7's result.
- **Anchors are untouched extremes only**, by the existing touch count in
  `levels._with_touches`. An anchor price that has been revisited is one whose positions
  were already resolved. `features.levels.fractal_n` stays at its shipped default, so the
  anchors are identical to Q7's.
- **Uniform rung weights are the primary result.** Q10b and Q10c run first with every rung
  weighted equally. OI-scaled and histogram-weighted is **one** preregistered secondary,
  run once. If the dumb version works the clever one is never needed, and a weighting scheme
  tuned until a table passes is the failure mode this file exists to prevent.
- **The starting histogram is measured, not invented.** From 194 addresses and $1,561M of
  tracked notional on the VPS on 2026-09-09, share of notional by leverage: under 3x 5.2%,
  3 to 5x 7.2%, 5 to 10x 22.3%, 10 to 20x 32.1%, 20 to 25x 17.5%, 25 to 40x 15.8%, 40x and
  up 0%. One day, 194 addresses, one venue. It is recomputed over whatever `hl_positions`
  days are synced and both are reported. This is the only free source of a leverage
  distribution anywhere: Binance, Bybit and OKX publish open interest and funding and never
  leverage.

## Q10a, reaction

For each projected band that price first reaches at time `t` over the year, measure the move
over the next 5, 15 and 30 minutes in the direction that fades the approach.

**Control: shifted rungs.** The same anchors and the same distance scale, each rung moved by
half the gap to the next rung, so the only thing varying is whether the distance is a real
leverage level.

**DECISION yes** when the ladder's 15-minute net-stress mean exceeds the shifted control's
and the cluster-bootstrap interval on the difference excludes zero; **no** when it excludes
zero the other way; **inconclusive** otherwise.

## Q10b, cascade confluence

Q7's question with projected bands in place of raw levels. The same qualifying episodes from
`build_episodes`, the same outcomes from `attach_outcomes`, the same widths of 10, 15 and 25
bps, asked as of cascade start so a band the cascade itself created is invisible.

Report the in-band and out-of-band intervals side by side, and separately by
`bands_stacked` of one, two, and three or more, because confluence is the actual claim.
**Q7's corresponding row is printed beside every row of this table.**

**DECISION yes** when some width has in-band beating out-of-band with a bootstrap interval
on the difference excluding zero and the effect surviving the top-five-cluster removal;
**no** when no width does; **inconclusive** on sample.

## Q10c, fuel and exhaustion

At each episode's start, split episodes into terciles by `fuel_ahead_usd`, the summed weight
of bands the cascade would have to run through in its own direction within
`fuel_window_bps`.

**Preregistered direction: the low-fuel tercile reverts better**, because the forced flow
ahead is already spent.

Outcomes at 5, 15 and 30 minutes plus the stop-out rate in Q8's winning `(k_stop, k_target)`
cell, which is `k_stop=1.0`, `k_target=0.8`. The comparison that matters is against Q2's
`fixed_delay` baseline, not against zero.

**DECISION mirrors Q9b in `prompts/05d_external_ideas.md` exactly**, so the projected map and
the real map are graded on the same question with the same bars: **yes** when low minus high
at 15 minutes net-stress has a cluster-bootstrap interval excluding zero in the
preregistered direction **and** the stop-out rate difference has the same sign; **no** when
the interval excludes zero the other way; **inconclusive** otherwise or under 40 episodes
per tercile.

## Q10d, does the projection match reality

Descriptive. **No DECISION.**

Over whatever `hl_positions` days are synced, for each projected band report the real
notional whose `liquidation_px` falls within `band_width_bps` of it, the share of bands with
any real notional, and the rank correlation between projected weight and real notional.

Reported separately for isolated and cross margin, because a cross-margin liquidation price
moves with the rest of that account's book and 98% of the tracked notional is cross. This is
the sanity check on the whole approach and it needs no waiting.

## Stop rules

- **Q10a no and Q10b no**: the ladder is dead. `features/liqmap.py` stays in the repo
  switched off, exactly as the zone code did after Q7, and the result is recorded beside Q7
  so nobody re-derives it.
- **Q10b yes**: the next prompt is one confluence rule in `strategy/rules.py`, disabled by
  default, plus a Q8-style stop and target grid keyed on band distance.
- **Q10c yes alone**: an exhaustion feature only, and it merges with whatever Q9b concludes.
