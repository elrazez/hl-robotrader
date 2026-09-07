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
| 1 | `Gtc` limit far from market, cancel by cloid, confirm the `orderUpdates` sequence | `gtc_cancel` | **run 2026-09-07, void: unfunded account** |
| 2 | `Alo` that would cross, confirm the rejection string | `alo_cross` | **run 2026-09-07** |
| 3 | Entry then TP/SL with `positionTpsl` grouping, fill one, confirm the other cancels | `tpsl_group` | **not run: needs margin** |
| 4 | 50 submit-to-ack latencies at a quiet and a busy time | `latency` | **not run: would measure void orders** |
| 5 | `scheduleCancel` availability and its exact error when gated | `schedule_cancel` | **run 2026-09-07** |
| 6 | `userFees` payload shape and current rates | `user_fees` | **run 2026-09-07** |
| 7 | Reprice by cloid and by oid, and after a cancel | `modify` | **run 2026-09-07, void: unfunded account** |
| 8 | Reduce-only with and without a position | `reduce_only` | **not run: needs margin** |
| 9 | Raw `clearinghouseState`, `openOrders`, `frontendOpenOrders`, `userFills` | `state_shape` | **run 2026-09-07, empty account** |

**Read the "void" rows as not run.** The testnet account had `accountValue: 0.0` on
2026-09-07. On a zero-equity account Hyperliquid answers an order with
`{"resting": {"oid": ...}}` and a real oid, and then books nothing: `openOrders`,
`frontendOpenOrders` and `historicalOrders` all stay empty, `totalMarginUsed` stays 0.0,
`orderUpdates` sends no frame, and `cancel_by_cloid` on the phantom returns
`statuses: ["success"]`. Every probe whose answer depends on an order actually resting
therefore measured nothing. Claim the faucet, then re-run 1, 3, 4, 7 and 8.

### What the three trustworthy probes found

**Probe 6, `userFees`.** `userCrossRate: "0.00045"` and `userAddRate: "0.00015"`: 0.045%
taker and 0.015% maker, exactly the defaults CLAUDE.md rule 2 puts in every backtest. The
cost model is confirmed against the venue rather than against a doc. No referral or
staking discount is active (`activeReferralDiscount: "0.0"`), so there is no hidden edge
in the fee line and none to lose.

**Probe 2, `Alo` that crosses.** The exact string, verbatim:

    Post only order would have immediately matched, bbo was 2491.3@2491.4. asset=4

It arrives with HTTP 200 and a top-level `"status": "ok"`, inside
`response.data.statuses[0].error`. **A rejected order is an `ok` response.** Anything that
branches on the envelope's status treats a rejection as a fill; `classify_order_response`
has to keep reading down to the per-status objects. The same holds for the modify refusal,
`"Cannot modify canceled or filled order"`, which is also carried inside an `ok`.

**Probe 5, `scheduleCancel`: gated, and this settles an open design question.**

    Cannot set scheduled cancel time until enough volume traded. Required: $1000000. Traded: $0.

Clearing it (`schedule_cancel(None)`) is refused by the same gate, so the dead man's
switch is not available to this account and will not be until a million dollars of volume
has gone through it. `execution/watchdog.py` says the venue half of the kill path is
"owed" and that this probe would decide whether `scheduleCancel` or a signing watchdog
closes it. It has decided: **the file-based watchdog is the only dead man's switch we
have.** An order resting at Hyperliquid when the whole box dies stays there until someone
cancels it by hand. Phase 6 must either accept that or fund a signing watchdog.

**The WebSocket user channel, observed while the probes ran.** Both `orderUpdates` and
`userFills` accept a subscription for the account and echo it back with the address
lowercased, whichever case is sent. `userFills` immediately pushes a snapshot frame
(`{"channel": "userFills", "data": {"isSnapshot": true, "fills": []}}`); `orderUpdates`
sends nothing until an order changes state, and on the unfunded account nothing ever did.
So the shape of a live `orderUpdates` frame is still unknown and the user-stream client
cannot be written against a measured payload yet. That is probe 1's job once funded.

### Provisional, from the void runs. Do not build on these.

Recorded only so the re-run has something to disagree with. `modify` addressed by cloid
answered with a **new oid** and the same cloid, which if real would mean a reprice is a
cancel-and-replace at the venue and loses queue priority, making the "prefer modify"
preference worth one round trip rather than any priority. The original oid then answered
`"Cannot modify canceled or filled order"`. One modify round trip measured 789ms, which is
a REST number from Tokyo to Tokyo and is implausibly slow for a booked order; it is more
likely the cost of a rejected one. None of this is evidence until an order actually rests.

Probe 4 writes the number `docs/STRATEGY_AND_RISK.md` uses for the latency model, and
probe 5 decides whether `execution/watchdog.py` is a convenience or the only dead man's
switch we have.
