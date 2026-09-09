# Hyperliquid reference for execution

Researched 2026-08-30 against official docs and third-party measurements. When something here conflicts with a live testnet probe, the probe wins; update this file and note it in `docs/DECISIONS.md`.

## Accounts and signing

- Two wallets: the **main account** (holds funds, address used as `account_address` everywhere) and an **API wallet / agent** (a separate keypair approved at `app.hyperliquid.xyz/API`). The bot signs with the agent key. Agents cannot withdraw. Use one agent per environment and per process.
- Nonces are millisecond timestamps. They must be unique and increasing per signer within the recent window. One signer per process; never share an agent key between two running processes.
- Testnet: `https://api.hyperliquid-testnet.xyz`, UI at `app.hyperliquid-testnet.xyz`, faucet available. Mainnet: `https://api.hyperliquid.xyz`.
- Official SDK: `hyperliquid-python-sdk` (`from hyperliquid.info import Info`, `from hyperliquid.exchange import Exchange`). It is synchronous and uses `requests`. Use it for signing and REST actions inside a thread executor; use our own `websockets` client for streams so reconnect logic is ours.

## Endpoints

- `POST /info` (no auth): `meta`, `metaAndAssetCtxs`, `allMids`, `l2Book`, `candleSnapshot`, `clearinghouseState`, `openOrders`, `frontendOpenOrders`, `orderStatus`, `userFills`, `userFillsByTime`, `fundingHistory`, `userFunding`, `userFees`.
- `POST /exchange` (signed): `order`, `cancel`, `cancelByCloid`, `modify`, `batchModify`, `updateLeverage`, `updateIsolatedMargin`, `scheduleCancel`.
- WS `wss://api.hyperliquid.xyz/ws`: public `trades`, `l2Book`, `bbo`, `candle`, `activeAssetCtx`, `allMids`; per-user `orderUpdates`, `userFills`, `userEvents`, `userFundings`, `userNonFundingLedgerUpdates`. Send `{"method":"ping"}` every 20s. Server may disconnect without notice; user-stream snapshots on resubscribe include missed data (`isSnapshot: true` on the first message).

## Assets, prices, sizes

- Perp asset id = index of the coin in `meta.universe`. Cache the mapping at startup and refresh on universe change. Spot ids are offset by 10000 and are out of scope.
- Size precision: round to the asset's `szDecimals` (from `meta.universe[i].szDecimals`).
- Price precision: at most 5 significant figures, and at most `6 - szDecimals` decimal places for perps. Integer prices are always valid. Rounding lives only in `execution/rounding.py`; add a property test that round-trips random prices for every universe asset.
- Minimum order value: 10 USDC notional. Sizing must clamp to zero below this and report it.
- Max leverage is per asset (`maxLeverage` in meta). Cross margin by default; isolated is a config option. Leverage on HL is set with `updateLeverage` per asset and only changes the margin requirement; our sizing decides notional independently.

## Order semantics

- Order action fields: `a` asset, `b` is_buy, `p` price, `s` size, `r` reduce_only, `t` order type, `c` cloid (optional 16-byte hex, we always set it).
- Order types: `{"limit": {"tif": "Gtc" | "Ioc" | "Alo"}}`. `Alo` is post-only and is rejected if it would cross. `Ioc` is fill-or-cancel remainder. "Market" orders are `Ioc` limits at a slippage-bounded price; never use the SDK's default 5% slippage, pass our own bound from config.
- Trigger orders: `{"trigger": {"isMarket": bool, "triggerPx": px, "tpsl": "tp" | "sl"}}`. Use `grouping: "positionTpsl"` when attaching TP/SL to an existing position so they cancel each other when one fills.
- `modify` and `batchModify` replace price or size while keeping queue position only if size decreases at the same price. Treat any other modify as cancel plus new for queue purposes.
- `scheduleCancel` is a dead man's switch: cancels all open orders at a time at least 5s in the future. Docs attach a volume requirement to it; verify on testnet. If unavailable, the app-level watchdog in `execution/watchdog.py` must do the same job (cancel all if the strategy loop misses two heartbeats).
- Fills arrive on `userFills` with `crossed` (true means we were taker), `fee`, `closedPnl`, `dir`, `startPosition`. Order state arrives on `orderUpdates` with statuses such as `open`, `filled`, `canceled`, `triggered`, `rejected`, `marginCanceled`. Map every status; unknown statuses raise and page the owner via Telegram.

## Order manager rules

- State machine per cloid: `NEW -> SUBMITTED -> ACKED -> (PARTIAL)* -> FILLED | CANCELED | REJECTED`. Persist to `data/orders.sqlite` on every transition.
- Entry default: `Alo` limit at the best bid (long) or best ask (short), TTL 3s, at most 3 reprices, then either give up or send `Ioc` at a bounded price if the intent's urgency flag is set and the expected edge exceeds 3x the taker fee.
- Exit default: stop loss as a `sl` trigger, `isMarket: true`, `reduceOnly: true`, placed immediately after the entry fill acks. Target as a `Gtc` reduce-only limit. Time stop enforced by the bot with `Ioc` reduce-only.
- Reconciliation every 60s and on every reconnect: `clearinghouseState` positions vs our book, `openOrders` vs our registry. Unknown orders get canceled. Unknown positions trigger a halt and a Telegram alert; the bot does not "adopt" positions it did not open.
- Rejects are never retried. Transient transport errors retry with jitter up to 3 times and then surface.

## Fees (base tier, 2026-08)

- Perps: 0.045% taker, 0.015% maker, charged on notional in USDC. A taker round trip is 0.09% of notional. Tiers drop with 14-day rolling volume above $5M; a 4% referral discount and HYPE staking discounts stack. Query `userFees` at startup and use the live values in cost models rather than constants.
- Funding is paid hourly on HL (one eighth of the 8h rate each hour). Include it in backtests for any hold that crosses an hour boundary.
- No gas on HyperCore actions. Withdrawals to Arbitrum cost 1 USDC.

## Rate limits

Per IP (REST): 1200 weight per minute. Exchange actions weigh 1 (+1 per 40 items in a batch). Info weights: `l2Book`, `allMids`, `clearinghouseState`, `orderStatus` = 2; `userRole` = 60; everything else = 20, plus extra weight per 20 items returned for fills and history endpoints, and per 60 candles for `candleSnapshot`.

WebSocket: max 100 connections, 1000 subscriptions, 10 unique users in user-specific subscriptions, 2000 outbound messages per minute, 100 inflight post messages.

**Address-based action limit (the one that bites small accounts):** each address starts with a buffer of 10,000 actions and then earns 1 additional action per 1 USDC of cumulative traded volume. Every order, cancel and modify counts, and batched actions count as n. A repricing scalper on a small account can exhaust this. Rules:

- Budget actions per hour in config and expose the remaining estimate in `/status`.
- Cap reprices per intent (default 3) and per-coin intents per minute.
- Prefer `modify` over cancel plus place when queue position does not matter.
- Log the exact error string when it happens (`Too many cumulative requests sent ... Place taker orders to free up 1 request per USDC traded`) and halt new entries until volume catches up.

### How the per-IP budget is split across the processes on the box

Enumerated from the code 2026-09-09, because the VPS runs the recorder, the paper loop
and the testnet loop behind one address and the limit is per IP, not per process.

| Process | Mainnet `/info` calls | Interval | Weight/min |
| --- | --- | --- | --- |
| recorder, positions poller | `clearinghouseState` x `max_addresses`, weight 2 | `poll_s` 60s | 800 at 400 addresses |
| recorder, universe | `metaAndAssetCtxs`, weight 20 | `universe.refresh_min` 60 | 0.3 |
| paper loop | none | | **0** |
| testnet loop | `openOrders` 2 + `frontendOpenOrders` 20 + `clearinghouseState` 2 + `spotClearinghouseState` 20 | `execution.reconcile_s` 60s | 44, **against a different host** |
| a future mainnet loop | the same four | 60s | 44, plus one reconcile per reconnect |

Two of those rows are easy to get wrong. The **paper loop spends nothing**: its
reconciler is built on `live/loop.py::PaperBooks`, which answers `open_orders` and
`trigger_orders` out of the simulator and never calls `account`, and its feeds are all
WebSocket. The **testnet loop talks to `api.hyperliquid-testnet.xyz`**, a separate
deployment with its own limiter, so its 44/min is not in the mainnet bucket; it is
budgeted as if it were, because being wrong about that in the safe direction costs 44.

That leaves 800 for the poller, 100 reserved for a future mainnet loop, and about 250
spare. The poller logs `weight_per_min` (`StreamStats.weight_per_min`) every cycle, so
the number in the config is checked against a measurement rather than against this table.

**Round trip to `api.hyperliquid.xyz` from the VPS: 52 to 63 ms, measured 2026-09-09.**
A sequential sweep of 400 addresses is therefore about 23s and of 600 about 33s, inside
a 60s `poll_s` either way. The clock is not the binding constraint; the weight budget is.

## `clearinghouseState` is public for every address

Probed 2026-09-08 with `scripts/probe/hl_positions.py`, keyless, against two addresses
taken off the public trades tape. **Every Hyperliquid position is readable by anyone.**
Post `{"type": "clearinghouseState", "user": "0x..."}` to `/info` for any wallet and each
open position comes back as

    {"type": "oneWay", "position": {"coin": "BTC", "szi": "0.99782",
      "leverage": {"type": "cross", "value": 40}, "entryPx": "78072.5",
      "positionValue": "77654.34368", "unrealizedPnl": "-248.03624",
      "liquidationPx": null, "marginUsed": "1941.358592", "maxLeverage": 40,
      "cumFunding": {...}}}

with the response carrying its own `time`. `liquidationPx` is the venue's own computed
liquidation level and it is what `hl_positions` exists to record.

Four things the shape teaches, all measured rather than assumed:

- **`liquidationPx` is often null.** Three of the first probed account's five open cross
  positions had none. It is nullable in the schema and stays null in the lake: a zero
  would put a cluster at price zero and every map built from the table would be wrong
  there.
- **An isolated position carries an extra `leverage.rawUsd`**, and its `leverage.value`
  is the position's own leverage while `maxLeverage` is the asset's cap. The second
  probed account held BTC isolated at 15 against a `maxLeverage` of 40, so the two
  columns are genuinely different numbers.
- **A flat coin stays in `assetPositions` with `szi: "0.0"`**, the same fact
  `execution/hl.py::parse_clearinghouse_state` already drops. It is not a position and it
  is not a row.
- **There is no history.** The endpoint answers for now and not for a past block, so the
  liquidation map can only be recorded forward. That is why the recorder started before
  there was anything to grade.

Weight 2 (see rate limits above), so the recorder's default of 400 addresses a minute
costs 800 of the 1200 per-IP budget. `docs/research/external_bots_scan.md` section 1 has
the full argument, including what could kill the idea.

## Liquidation mechanics (relevant to the thesis)

- Liquidations trigger on **mark price**, which blends the oracle (CEX medians) with HL book state, not on last trade. Wicks on HL alone cannot liquidate; cross-venue moves can.
- Maintenance margin is roughly half of the initial margin at the asset's max leverage. Liquidatable positions are first sent to the book as market orders. Positions above 100,000 USDC notional are liquidated in roughly 20% chunks with a 30s cooldown between chunks, which spreads large cascades over minutes rather than seconds. This is important for exhaustion timing.
- If equity falls below two thirds of maintenance margin without a clean book close, the **backstop liquidator** (a strategy inside the HLP vault) takes the position at mark. Backstop liquidations keep the maintenance margin. Auto-deleveraging exists as a last resort.
- Fill records mark liquidations with `liquidation: {liquidatedUser, markPx, method: "market" | "backstop"}`. Backstop fills have the HLP liquidator vault as the counterparty; capture its address by probing a known backstop fill on mainnet and store it in config.

## Latency

- Validators and API are in AWS Tokyo (ap-northeast-1), fronted by CloudFront. Network RTT from Tokyo is single-digit ms; from other continents roughly 200ms; Singapore about 100ms.
- Independent March 2026 measurement from AWS Tokyo: median order-to-fill about 0.9s, almost all of it server-side processing. Official docs quote 0.2s. Backtests use 1.0s signal-to-fill by default and the live system records the measured distribution (`submit_ms` to first `orderUpdates` ack) so the number can be updated from evidence.
- Blocks are roughly 0.5s apart for book snapshots on the WS feed; do not expect sub-second reaction to be rewarded. The edge is in the minute-scale reversion, not in speed.

## Known error strings to handle

- Price outside allowed band relative to reference price: back off, re-read BBO, reprice.
- `Post only order would have immediately matched`: expected for `Alo`; reprice or drop.
- Insufficient margin: sizing bug or stale equity; halt entries, reconcile, alert.
- Minimum order value 10 USDC: sizing clamp failed; treat as bug.
- Cumulative request limit: see rate limits above.

## Probes to run on testnet in Phase 5 (write results back here)

All six live in `scripts/probe/hl_testnet.py`, one subcommand each. The script refuses any
URL without "testnet" in it and reads no key itself; `execution/signer.py` does that.

    uv run python scripts/probe/hl_testnet.py --list
    LIQFADE_ENV=testnet uv run python scripts/probe/hl_testnet.py gtc_cancel --coin ETH

| # | Probe | Subcommand | Answer |
| --- | --- | --- | --- |
| 1 | `Gtc` far from market, cancel by cloid, the `orderUpdates` sequence | `gtc_cancel` | **run 2026-09-08** |
| 2 | `Alo` that would cross, the rejection string | `alo_cross` | **run 2026-09-08** |
| 3 | Entry then TP/SL with `positionTpsl`, confirm the legs die with the position | `tpsl_group` | **run 2026-09-08** |
| 4 | 50 submit-to-ack latencies | `latency` | **run 2026-09-08, quiet hour only** |
| 5 | `scheduleCancel` availability and its error when gated | `schedule_cancel` | **run 2026-09-08** |
| 6 | `userFees` payload and current rates | `user_fees` | **run 2026-09-08** |
| 7 | Reprice by cloid and by a stale oid | `modify` | **run 2026-09-08** |
| 8 | Reduce-only with and without a position | `reduce_only` | **run 2026-09-08** |
| 9 | Raw `clearinghouseState`, `openOrders`, `frontendOpenOrders`, `userFills` | `state_shape` | **run 2026-09-08** |

**Run `userRole` first, every time.** Everything below was measured twice: once against the
agent wallet's own address, where it produced a coherent and completely wrong picture, and
once against the master account. See `docs/DECISIONS.md` 2026-09-08.

### 1. The `orderUpdates` sequence

A place-and-cancel produces exactly two frames, each carrying the full order and the cloid:

    {"channel": "orderUpdates", "data": [{"order": {"coin": "ETH", "side": "B",
      "limitPx": "1239.3", "sz": "0.0096", "oid": 59610337202, "origSz": "0.0096",
      "cloid": "0xec15..."}, "status": "open", "statusTimestamp": 1788859194749}]}
    ... same order, "status": "canceled", "statusTimestamp": 1788859195525

`status` is the state machine: `open`, `canceled`, and `filled` for the fill path. Every
frame carries the cloid, so the user-stream client can key on it and never needs the oid.

### 2. `Alo` that would cross

    Post only order would have immediately matched, bbo was 2491.3@2491.4. asset=4

HTTP 200, top-level `"status": "ok"`, words in `response.data.statuses[0].error`. **A
rejection is an `ok` response.** Anything branching on the envelope reads a rejection as a
fill; keep reading down to the per-status objects.

### 3. TP/SL with `positionTpsl`

`bulk_orders(requests, grouping="positionTpsl")` answers
`{"statuses": ["waitingForTrigger", "waitingForTrigger"]}`. Both legs appear in
`frontendOpenOrders` (not in `openOrders`) as `Take Profit Market` and `Stop Market`, each
`isTrigger: true`, `reduceOnly: true`, with its own cloid and a readable
`triggerCondition` ("Price above 2490.8" / "Price below 2466.1"). Note `isPositionTpsl`
reads **false** on both despite the grouping, so it cannot be used to identify them; the
cloid is the identity to track.

**They die with the position.** `market_close` on the position left `positions=[]` and
`frontendOpenOrders=[]` within 2 seconds, with no cancel of our own. The bot must still
track them by cloid, but it does not have to cancel them on a normal exit.

### 4. Submit-to-ack latency, from the Tokyo VPS, quiet hour

50 REST order round trips, testnet:

| | min | p50 | p95 | max | mean |
| --- | --- | --- | --- | --- | --- |
| submit | 656.6 | **718.9** | 819.0 | 982.4 | 735.4 |
| cancel | 657.8 | 718.6 | 792.1 | 821.1 | 727.6 |

All ms. This is much slower than a Tokyo-to-Tokyo round trip has any right to be, and it
matters: `docs/STRATEGY_AND_RISK.md` assumes a 1.0s signal-to-fill model, and a 719ms
median submit plus fill time makes that assumption roughly right rather than conservative.
**Still owed: the same 50 at a busy hour**, and the same on mainnet, since testnet
infrastructure is not the one that will take the orders.

### 5. `scheduleCancel` is gated, and this settles a design question

    Cannot set scheduled cancel time until enough volume traded. Required: $1000000. Traded: $89.21.

Clearing it (`schedule_cancel(None)`) is refused by the same gate. The `$89.21` is this
session's own probe volume, which is how we know the actions reached the master account.

`execution/watchdog.py` said the venue half of the kill path was owed, and that this probe
would decide between `scheduleCancel` and a signing watchdog. **It has decided: the
file-based watchdog is the only dead man's switch this account has**, and an order resting
at Hyperliquid when the box dies stays there until a human cancels it. Phase 6 accepts that
or funds a signing watchdog.

### 6. Fees

`userCrossRate: "0.00045"`, `userAddRate: "0.00015"`. 0.045% taker, 0.015% maker, exactly
the defaults CLAUDE.md rule 2 puts in every backtest, now confirmed against the venue. No
referral or staking discount active.

### 7. `modify` keeps the cloid and loses the queue

Placed at 1239.3 as oid 59610349613. Modified by cloid to 1251.7:

    {"resting": {"oid": 59610350495, "cloid": "0xe9589f08..."}}   <- new oid, same cloid

`openOrders` then shows one order at the new price with the **new oid and a new
timestamp**. So a modify is a cancel-and-replace at the venue: **queue priority is not
preserved.** What it does buy is one round trip instead of two (794ms measured) and a
stable cloid, which is the identity the registry tracks an entry by across its reprices.

Modifying by the **stale** oid answers `"Cannot modify canceled or filled order"` and
leaves the live order untouched, so a reprice racing a fill is a safe no-op rather than a
double order. The order manager should therefore address reprices **by cloid**, and treat
that error as "already gone, reconcile" rather than as a transport failure.

### 8. Reduce-only

With no position behind it:

    Reduce only order would increase position. asset=4

Against a position it rests normally and `openOrders` carries `"reduceOnly": true`. The
refusal is the case that matters: it is how an exit for a position that has already closed
comes back, and it must be read as "position gone" rather than retried.

### 9. The shapes reconciliation reads

`openOrders` rows carry `coin, side, limitPx, sz, oid, timestamp, origSz, cloid` plus
`reduceOnly` when set. Trigger orders appear only in `frontendOpenOrders`, with the extra
`triggerCondition, isTrigger, triggerPx, children, isPositionTpsl, orderType, tif`.
An `assetPositions[].position` carries `coin, szi, leverage {type, value}, entryPx,
positionValue, unrealizedPnl, returnOnEquity, liquidationPx, marginUsed, maxLeverage,
cumFunding {allTime, sinceOpen, sinceChange}`.

**`accountValue` is 0.0 while flat.** It reported 0.0 flat, 1.48872 holding 0.006 ETH and
0.0 again after closing. It is not a funding indicator; the spot USDC balance is.

## What an agent key can and cannot do, measured 2026-09-08

Established by doing each one on testnet with the agent key and watching which address's
balance moved. The split is sharper than the docs suggest and it matters twice: for
recovering misplaced testnet funds, and as a security property on mainnet.

| Action | SDK call | Acts on |
| --- | --- | --- |
| Place, modify, cancel an order | `order`, `modify_order`, `cancel_by_cloid` | **the master account** |
| `scheduleCancel` | `schedule_cancel` | **the master account** |
| Spot to perps and back | `usd_class_transfer` | the agent's own address |
| Send spot tokens | `spot_transfer` | the agent's own address |
| Send perps USDC | `usd_transfer` | the agent's own address |

**Trading acts on the master; moving money acts on the signer.** An agent key can open,
close and cancel positions on the master account and cannot move a cent of the master's
money anywhere. That is the security property the agent-wallet design exists for: a leaked
bot key is a trading risk, not a withdrawal risk. `docs/RUNBOOK.md` should say so, because
it changes what a key compromise costs and therefore how it should be responded to.

The practical consequence is only about *whose* balance the bot can move, not about
needing a separate perps balance. **There is no spot-to-perps step.** The account's USDC
is its collateral directly: `clearinghouseState` reports `accountValue: 0.0` while flat
and rises the moment a position is on (measured: 0.0 flat, 1.48872 holding 0.006 ETH,
0.0 again after closing). A resting order and a market open both work against USDC that
never left the spot balance.

What does matter: **an agent address holding a balance is a balance no order will ever
use**, because orders execute on the master. Funds sent to the agent address by mistake
are recoverable with the agent key through `spot_transfer`, which acts on the agent's own
address, and that is the only reason the split above needs to be understood at all.

## Which balance is equity, measured 2026-09-08

Sizing multiplies equity by `sizing.risk_frac`, so reading the wrong field is either no
trades at all or trades several times the intended size. This is what the venue actually
did, on one account, across one round trip of about $15 of ETH.

| | `accountValue` | `totalRawUsd` | `totalMarginUsed` | `withdrawable` | spot USDC |
| --- | --- | --- | --- | --- | --- |
| flat | 0.0 | 0.0 | 0.0 | 0.0 | 995.648036 |
| holding 0.006 ETH | 1.507194 | **-13.564747** | 0.753597 | 0.0 | 995.641315 |
| closed again | 0.0 | 0.0 | 0.0 | 0.0 | 995.633862 |

**The perps ledger is not where the money is.** Spot USDC fell by 0.0067 opening and
0.0075 closing, which is the taker fee and nothing else: the margin was never moved out of
spot. Opening the position instead drove the perps raw USD *negative*, to -13.56, against a
position marked at +15.07. `accountValue` is the sum of those two, so it reports the
position's net equity inside the perps sub-ledger and reads **0.0 whenever the account is
flat**, however much collateral stands behind it.

**So equity is `marginSummary.accountValue` plus the spot USDC balance**, and that is what
`parse_clearinghouse_state` returns and what `sizing` reads. The sum is right in all three
layouts rather than by luck:

- **spot-collateralised** (this testnet account): 995.65 + 0.0 = 995.65, the tradeable
  capital. Reading `accountValue` alone gives 0 and refuses every entry for `risk_budget`,
  which is what happened before this was measured.
- **perps-funded** (a mainnet deposit, which lands in perps): 0 + 1000 = 1000. Reading the
  spot balance alone would give 0 and refuse everything.
- **mixed**: both halves counted once.

While a position is open the perps half carries its unrealized PnL, which belongs in
equity, so the sum tracks the account rather than the deposit.

**Do not read `withdrawable` as equity.** It was 0.0 with a position open *and* 0.0 while
flat with 995 USDC available, so it answers a different question entirely.

**The check worth keeping:** `accountValue: 0.0` is not an empty account. Read the spot
balance before concluding anything, and `userRole` before concluding anything at all
(see `docs/DECISIONS.md` 2026-09-08).
