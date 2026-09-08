# What four public liquidation bots do that we do not

Scanned 2026-09-08. Four public projects that trade or watch Hyperliquid liquidations were
read end to end and measured against this repo as it stands at `4b4fe7f` (Phase 5, day one
of the paper acceptance run). The question was narrow: **is there anything in them we should
build?** Most of what they contain we already have in a better form, and that is recorded
here too, because the next person to find these repos should not have to read them again.

| Source | What it is | Verdict |
|---|---|---|
| [moondev.com/hip-3-liquidation-trading-bot](https://moondev.com/hip-3-liquidation-trading-bot) | Single-file HIP-3 bot. Trades *toward* a cascade when $250k of positions sit within 2.5% of their liquidation price. | The liquidation map is worth a research question. The rest is beneath what we have. |
| [braindeadpt/Trading-bot-hyperliquid_V1](https://github.com/braindeadpt/Trading-bot-hyperliquid_V1) | 57k lines, 27 strategies, one of them a liquidation fader. | Two engineering practices worth copying. Its strategy layer is what `CLAUDE.md` forbids. |
| [wilsontiger2222/liquidation-hunter](https://github.com/wilsontiger2222/liquidation-hunter) | 1.4k lines. Funding + OI + liquidation-cluster voting, alert mode only. | The map again, plus one Hyperliquid data fact we had not recorded. Signal blending rejected. |
| [syzz-xd/hyperliquid-liquidations](https://github.com/syzz-xd/hyperliquid-liquidations) | 318 lines. Discord webhook. | Nothing. See the rejection list. |

Four ideas survived. One is a Phase 3 research question, one is a small extension of it, and
two are Phase 5 engineering. None of them is a strategy change.

---

## 1. The Hyperliquid liquidation map

**All three real projects build the same thing, and it is the one idea here that attacks a
problem we are actually stuck on.**

Every Hyperliquid position is public. `clearinghouseState` for any address returns, per
position, `coin`, `szi`, `entryPx`, `leverage`, `marginUsed`, `positionValue` and, the field
that matters, **`liquidationPx`**, the venue's own computed liquidation price. Verified
against <https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals.md>,
read 2026-09-08. No auth, no key, no paid tier.

Poll a set of addresses, bin their `liquidationPx` by price, and you have a forward-looking
map of how much notional is queued to be force-closed at each price level, by side. The three
projects use it the same way and we would not: they buy *into* the cluster and ride the
cascade. We fade cascades. The map is worth more to a fader than to a chaser, in three
distinct ways, and they should be treated as three separate claims because they can fail
independently.

**1a. It is a candidate replacement for the proxy that parked Phase 3.** `docs/research/hl_realtime_liquidations.md`
asked whether the live bot can *observe* Hyperliquid liquidations in real time and answered
no: the tape carries no liquidation flag, the S3 archive is a day late, and the only complete
source is a 128 GB node at $100/month. `features/proxy.py` was the workaround and it reached
precision 0.01, which is why `cascade.trigger` fell back to cross-venue Bybit. The map asks a
different question, not *what just liquidated* but *what is about to*, and then, one poll
later, *what is gone*. Positions that held a `liquidationPx` inside the range price just
traversed, and are absent from the next snapshot, liquidated. That is an estimate of
Hyperliquid's own forced flow derived from the accounts themselves rather than from the shape
of the tape, and there is no obvious reason it should be as weak as a tape heuristic. It is
also the only free HL-native forced-flow candidate that the realtime-liquidations research
did not consider, because that research was scoped to observation.

**1b. Fuel remaining is an exhaustion feature, and exhaustion is where our PnL is.** Q2 found
that a flat `fixed_delay_s` after cascade start carried more PnL than the spec's exhaustion
rule over 2,722 episodes, which is a finding about entry *timing* and an admission that we
have no good measure of when the forced flow is spent. The map gives one directly: with
price at P and $40M of long positions still liquidating between P and P−2%, the cascade has
fuel and a fade is early. With the band empty, the sellers are done. This is the use with
the clearest line to a config change (`exhaustion.mode`), and it is testable against the
`fixed_delay` baseline we already have.

**1c. Targets and stops.** A dense cluster below the entry is a bad place to hide a stop, and
the far edge of a cleared band is a natural mean-reversion target. Third in line, do not
build this until 1a or 1b pays.

### Where the addresses come from

The obvious objection is that you cannot poll every Hyperliquid account. You do not have to;
cascade fuel is concentrated in large leveraged positions, so a top-N-by-notional set carries
most of the notional and misses the retail tail. Two free harvests, both of which we already
have the data for:

- **Live, from a feed we already consume.** The public `trades` WS payload carries
  `users: [buyer, seller]`, documented, and already quoted in
  `docs/research/hl_realtime_liquidations.md`. **We currently discard it**: `TRADES` in
  `data/schemas.py` stops at `tid`. Every large HL fill names its counterparties and we throw
  the names away. Note that `TRADES` is in `MULTI_VENUE_TABLES`, shared with Bybit, OKX and
  Binance rows that have no such field, so this must not become a column on `TRADES`; harvest
  into a separate table or hold the address set in memory in the poller.
- **Historical, from the lake.** `node_fills` already holds a year with addresses attached.
  braindeadpt seeds its address list exactly this way. Good enough to bootstrap a ranked set
  on day one instead of waiting for the live harvest to warm up.

Both moondev and wilsontiger dodge this problem rather than solving it: moondev buys the
positions from a keyed third-party API, and wilsontiger hardcodes thirteen wallet addresses
in `config.yaml`. Neither is acceptable here, one adds a paid dependency and a data source
we cannot audit, the other rots the moment a whale closes an account.

### Rate budget

`docs/HYPERLIQUID.md` puts the info limit at 1200 weight/min per IP with `clearinghouseState`
at weight 2, so 600 calls/min is the ceiling and the recorder's existing info traffic comes
out of the same budget. A few hundred addresses per minute is comfortable; the whole HL
account set is not. The poller belongs next to `recorder/streams/coinalyze.py`, which is
already a REST poller writing canonical rows on an interval rather than a stream, the shape
exists, this is one more of them plus one schema.

### What could kill it

Written down first, so the research question is not graded on a curve:

1. **Coverage.** If the liquidations that move price are a long tail of small accounts rather
   than the tracked whales, the map measures the wrong flow. This is the one that decides it.
2. **Absence is not liquidation.** A position that leaves the snapshot may have been closed
   voluntarily or had margin added. Without the fills to confirm, every "liquidated" figure
   is an upper bound.
3. **Cadence.** A cascade resolves in seconds; a poll cycle over hundreds of addresses does
   not. The map may be a regime feature and not a scalp feature, which weakens 1a and 1b both.
4. **`liquidationPx` moves.** It is a function of margin, not a fixed level, so a snapshot
   ages faster than the position does.
5. **Cross versus isolated margin.** A cross-margin account's per-coin `liquidationPx`
   depends on the rest of the book, so binning it as if it were an independent level is wrong
   for exactly the accounts that carry the most notional.

### How to start, and how it gets graded

**Prospectively, and only prospectively.** Historical `clearinghouseState` cannot be
reconstructed, the endpoint answers for now, not for a past block, so there is no way to
backtest this against the year in the lake. braindeadpt reached the same wall and its Phase 2
reaction analysis honestly reports "no evidence yet". Which makes the first step small and
the second step patient:

- **Step one, recorder only.** Snapshot the map on an interval into Parquet. No feature, no
  strategy wiring, no config surface. It costs a poller and a schema and it starts the clock,
  and until the clock has run there is nothing to grade.
- **Step two, once weeks of snapshots exist.** A preregistered research question, in the
  Phase 3 style: does snapshot-to-snapshot disappearance of at-risk notional predict
  Hyperliquid liquidations we can see a day later in `node_fills` (that is the validation set,
  and it is exact); and does fuel-remaining at cascade start separate the episodes that revert
  from the episodes that keep going, measured against the `fixed_delay` baseline?

Nothing enters `strategy/` before step two answers. If step two says the coverage is not
there, the poller is deleted and this document is the record of why.

---

## 2. Resting stop and take-profit triggers

wilsontiger's README carries one Hyperliquid fact worth keeping, and its `whale_tracker.py`
implements it: **`frontendOpenOrders` returns `orderType`, `triggerCondition` and `triggerPx`
per order**, so every wallet's resting stop-loss and take-profit levels are public too.
`docs/HYPERLIQUID.md` lists the endpoint under available info calls but says nothing about
what it exposes about *other* people.

Stops are forced flow that is not a liquidation, and they cluster at round numbers and under
swing lows, where `features/levels.py` already draws its zones. Same address set, same
poller, one more call per address. Strictly an extension of §1 and only worth building after
§1 has paid; noted here so that when someone asks "can we see stops", the answer and its
source are already written down.

---

## 3. A live-versus-replay drift check

braindeadpt's `src/research/live_vs_replay.py` takes a window the live engine actually
traded, replays *the same stored candles* through the backtest engine under *the same
effective config*, and diffs the two: trades, and per-gate rejection counts. Divergence means
live and backtest have silently drifted apart, which is the failure mode that invalidates
every backtest number at once.

`CLAUDE.md` calls backtest/live parity the single most important design constraint of this
project, and we test around it rather than at it, `test_backtest_golden.py` pins the
backtest against itself, `test_strategy_cascade_consistency.py` holds the online detector to
the vectorized one. Neither takes a real run and replays it.

The timing is the argument. The 05c acceptance run started 2026-09-07 with an explicit
qualification in `docs/DECISIONS.md`: the live loop does not yet have the entry policy, so
`execution.entry_ttl_s` and `execution.max_reprices` are honoured by `backtest/engine.py` and
by nothing in the live path, and the two are running different order policies by
construction. The Phase 5 DoD asks whether paper lands in a plausible band of the backtest
over the same days. **That is this check, and right now it is a sentence in a decision log
rather than something a command can answer.** Build it when the entry policy lands and it
grades its own arrival.

One discipline worth copying verbatim: where the live side genuinely does not persist a
field the backtest computes, braindeadpt reports that dimension as `NOT_COMPARABLE` rather
than defaulting it to zero and diffing against the default. A comparator that quietly invents
the missing half is worse than no comparator.

---

## 4. Scoring the intents we refused

braindeadpt's `shadow_outcome_evaluator.py` replays every decision the bot did *not* act on
through recorded candles and reports what it would have made.

We are half of the way there already: `live/loop.py` logs every refusal with its gate and
reason, rate-limits the Telegram post per reason, and the backtest report prices what each
filter cost over a run. What is missing is the live half, after a week of paper, "what did
`spread_too_wide` cost me" is answerable from the backtest and not from the run that just
happened. The pieces exist (`backtest/fills.py`, `backtest/costs.py`, the refusal log, the
lake); the lazy version is a CLI that joins them, not new machinery.

Two fill rules from their implementation are worth taking because they bias the counterfactual
the right way: when one bar's range touches both the stop and the target, **resolve as the
stop**; when a bar opens beyond a level, **fill at the open**, not at the level. A
counterfactual that flatters the trades you did not take is worse than not measuring, because
it argues for removing filters.

Lowest priority of the four. It is observability, not edge.

Built 2026-09-08 on branch `05d-refusals`: refusals are persisted from both live sessions into `data/orders.sqlite`, and `liqfade refusals` scores them. Both fill rules above were taken.

---

## Rejected, with reasons

- **"Any trade over $50k is a liquidation"** (syzz, whole repo). A notional threshold on the
  public tape with a hardcoded per-coin table and a comment that round-numbered sizes are
  "common in liquidations". This is the family of heuristic `features/proxy.py` already
  measured at precision 0.01. There is nothing here we have not already tried and rejected
  with evidence.
- **Weighted signal voting** (wilsontiger's `signal_aggregator.py`: funding 0.35, OI 0.30,
  liquidation 0.35, blended into a confidence score, threshold 0.6). The weights are asserted,
  not fitted, and a blended scalar cannot be attributed, when it underperforms you cannot
  tell which input did it. Our `strategy/rules.py` confluence rules are named, individually
  priced by the backtest report, and individually switchable. Strictly better; do not trade it
  for a weighted sum.
- **HMM regime detection** (braindeadpt's `core/hmm_regime.py`, 514 lines of hand-rolled
  Baum-Welch with a k-centroid fallback, replacing an ADX threshold). Speculative complexity
  of exactly the kind `CLAUDE.md` rules out, and it is not even load-bearing in their own bot.
- **The 27-strategy ensemble** (braindeadpt's `strategies/`, plus a 4,631-line `engine.py`, an
  `ensemble.py` and a `checklist_meta.py` that scores strategies against each other). This is
  the architecture our `CLAUDE.md` was written to prevent.
- **moondev's trading rules**, momentum confirmed by whether the last 1h candle closed higher
  than the one before, fixed $15 notional, fixed 3x leverage, one position at a time, 2-hour
  hard exit. Every one of these is beneath `sizing/`, `risk/` and `strategy/` as they stand.
  The 1h-close momentum check is genuinely worse than nothing at our horizon: our hold is 1 to
  60 minutes.
- **The "nudge trade"** (wilsontiger's README suggests placing a market sell to *start* the
  cascade you then trade, calling it "ethically gray"). No. Separately, it is arithmetic: our
  size cannot move a liquid HL perp into a cluster, so it would be paying taker fees to
  achieve nothing even if we wanted it.
- **A trend or ADX filter** (braindeadpt's liquidation catcher blocks entries above ADX 40 ,
  do not fade a strong trend). Not rejected on merit; rejected as a *build*. It is a research
  question, and the lab can answer it against the year in the lake whenever there is a reason
  to ask. It is not one of the four things above.

## Sources

- <https://moondev.com/hip-3-liquidation-trading-bot>, read 2026-09-08.
- <https://github.com/braindeadpt/Trading-bot-hyperliquid_V1>, read 2026-09-08.
- <https://github.com/wilsontiger2222/liquidation-hunter>, read 2026-09-08.
- <https://github.com/syzz-xd/hyperliquid-liquidations>, read 2026-09-08.
- <https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals.md>,
  read 2026-09-08, for the `clearinghouseState` position schema and `liquidationPx`.
