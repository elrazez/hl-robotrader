# Can the live bot see Hyperliquid liquidations in real time?

Researched 2026-09-06 against primary sources only: Hyperliquid's own documentation
(`hyperliquid.gitbook.io/hyperliquid-docs`, markdown mirrors fetched by appending `.md`), the
`hyperliquid-dex` GitHub org, and vendors' own pricing and terms pages. Every claim carries a
URL and the date it was read. Where a fact could not be established from a primary source it
is marked **unverified** or **inferred** rather than guessed.

Phase 3 parked on stop rule 5. Q1 and Q8 measured a real edge on Hyperliquid's own
liquidations, but those liquidations reach the lake from the S3 node archive a day late, in
`node_fills`, and the HL-native proxy that was meant to stand in for them live reached
precision 0.01 (`reports/research/q3_proxy.md`, `reports/research/SUMMARY.md`). The trigger
therefore fell to cross-venue Bybit. This document answers the question that decision was
resting on and never asked directly: **can the live bot see Hyperliquid's own liquidations
within a second or two, and what would that cost?**

Short answer: yes, exactly and within a block — from a machine running `hl-visor
run-non-validator --write-fills`, and from nothing else. That machine is the problem, not the
software: Hyperliquid publishes 16 vCPU and 128 GB of RAM for a non-validator, which is well
over the $50/month the whole project runs on. Everything cheaper is either a strict subset of
liquidations, a derived proxy, or an aggregate at the wrong cadence — but two of those free
options are meaningfully better than what Phase 3 actually measured, and neither has been
tried.

## Options

| Option | Latency | Completeness | Monthly cost | Verdict |
|---|---|---|---|---|
| Public WS `trades` | sub-second, block cadence | 100% of the tape, **0% flagged as liquidation** | $0 | The Phase 3 proxy's raw input. No flag exists to recover. |
| Public WS `userFills` on the liquidator vault | sub-second | **backstop only**, a minority by HL's own wording | $0 | Exact but partial. Free, cheap to add, wrong subset. |
| Public WS `userEvents` per liquidated user | sub-second | needs the address *before* the liquidation; cap of 10 unique users | $0 | Unusable: you cannot subscribe to a user you have not identified yet. |
| Explorer API / `explorerBlock`, `explorerTxs` | unknown | liquidations are not signed transactions (inferred) | $0 | Undocumented and probably carries nothing we need. |
| `activeAssetCtx` OI drop + mark-vs-mid | sub-second, block cadence | derived, not observed | $0 | The one free upgrade Phase 3 could not test: block cadence instead of once a minute. |
| **Own non-validating node, `--write-fills`** | **one block + one flush** | **100%, with `liquidation.method`** | hardware only; cheapest 128 GB box priced from a first-party page is **$100/mo + $100 setup** (§2) | The only complete real-time source. Doubles the whole budget on its own. |
| Paid peering (Alchemy, Hydromancer, Hyperdash, QuickNode) | n/a | **sells no liquidations at all** | **~$500/mo** each | Buys mempool quality on top of a node you still have to run. Not our problem, and 10x budget. |
| Tardis.dev | free real-time (it is the public WS feed) | HL channels are `trades, l2Book, fastBook, bbo, activeAssetCtx, activeSpotAssetCtx` — **no liquidations** | $0 live / $650+ historical | Independent confirmation that the public feed has no liquidation flag. |
| Coinglass | polling | HL not documented on the liquidation endpoint; 4h intervals at $29, unrestricted at $299 | $29–$699/mo | Already rejected in `docs/DATA_SOURCES.md`; this confirms it from their own pages. |
| S3 archive `node_fills_by_block` (status quo) | ~1 day (our observation, not an SLA) | 100% | egress only, ~$0.09/GB | What the lake has. Research-grade, not tradeable. |

## 1. Public Hyperliquid feeds

**Source: <https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions.md>, read 2026-09-06.**

The documented subscriptions are `allMids`, `notification`, `webData3` (the SDK still ships
`webData2`), `candle`, `l2Book`, `trades`, `orderUpdates`, `userEvents`, `userFills`,
`userFundings`, `userNonFundingLedgerUpdates`, `activeAssetCtx`, `activeAssetData`,
`userTwapSliceFills`, `userTwapHistory`, `bbo`, `spotState`, `clearinghouseState`,
`openOrders`, `allDexsClearinghouseState`, `allDexsAssetCtxs`, `twapStates`,
`outcomeMetaUpdates`, and `fastAssetCtxs`. **Neither `explorerBlock` nor `explorerTxs` appears
anywhere on that page**, nor in the official SDK's `Subscription` union
(<https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/master/hyperliquid/utils/types.py>,
read 2026-09-06, which lists exactly `allMids, bbo, l2Book, trades, userEvents, userFills,
candle, orderUpdates, userFundings, userNonFundingLedgerUpdates, webData2, activeAssetCtx,
activeAssetData`).

### `trades` has no liquidation flag, and this is structural

```typescript
interface WsTrade {
  coin: string;
  side: string;
  px: string;
  sz: string;
  hash: string;
  time: number;
  // tid is 50-bit hash of (buyer_oid, seller_oid).
  // For a globally unique trade id, use (block_time, coin, tid)
  tid: number;
  users: [string, string] // [buyer, seller]
}
```

Compare with `WsFill`, which is the schema the node writes under `--write-fills` ("Streams
fills in the API fills format",
<https://github.com/hyperliquid-dex/node/blob/main/README.md>, read 2026-09-06):

```typescript
interface WsFill {
  coin, px, sz, side, time,
  startPosition: string;
  dir: string;            // used for frontend display
  closedPnl: string;
  hash: string;
  oid: number;
  crossed: boolean;       // whether order crossed the spread (was taker)
  fee: string;            // negative means rebate
  tid: number;
  liquidation?: FillLiquidation;
  feeToken: string;
  builderFee?: string;
}

interface FillLiquidation {
  liquidatedUser?: string;
  markPx: number;
  method: "market" | "backstop";
}
```

**Precisely what public `trades` lacks compared with `node_fills`:** `liquidation`
(the whole object — the flag, `liquidatedUser` and `markPx`, and the
`method: "market" | "backstop"` discriminator), `startPosition`, `closedPnl`, `crossed`,
`fee`, `feeToken`, `builderFee`, `oid`, and `dir`. It keeps `coin`, `side`, `px`, `sz`,
`hash`, `time`, `tid`, and adds `users` — the two counterparty addresses, which `WsFill` does
not carry because a fill is already scoped to one user. So `trades` gives you *who traded*
but never *why*, and the "why" is the only field Phase 3 needed.

Note also that `node_trades` (the `--write-trades` flag, one rung below `--write-fills`) is
richer than the public feed — `side_info` carries `user`, `start_pos`, `oid`, `twap_id`,
`cloid` per side
(<https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/nodes/l1-data-schemas.md>,
read 2026-09-06) — but it *still* has no liquidation flag. `--write-fills` is the flag that
matters, and the README says it overrides `--write-trades` when both are set.

### `userEvents` carries a real liquidation event, but you cannot subscribe to it usefully

```typescript
type WsUserEvent = {"fills": WsFill[]} | {"funding": WsUserFunding}
                 | {"liquidation": WsLiquidation} | {"nonUserCancel": WsNonUserCancel[]};

interface WsLiquidation {
  lid: number;
  liquidator: string;
  liquidated_user: string;
  liquidated_ntl_pos: string;
  liquidated_account_value: string;
}
```

This is the cleanest liquidation object on the public API — and it is keyed on a `user`
address you must supply at subscribe time. To receive it you would have to already know which
account is about to be liquidated. Worse, the rate limit is **"Maximum of 10 unique users
across user-specific websocket subscriptions"**
(<https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits.md>,
read 2026-09-06). Ten addresses, chosen in advance, out of the whole venue. Dead end for a
market-wide detector.

### The liquidator vault's `userFills` catches **backstop only** — this is the load-bearing finding

The routing is stated plainly in
<https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations.md> (read 2026-09-06):

> "When the account equity drops below maintenance margin, the positions are first attempted
> to be entirely closed by **sending market orders to the book**. The orders are for the full
> size of the position, and may be fully or partially closed."

> "If the account equity drops below **2/3 of the maintenance margin without successful
> liquidation through the book**, a backstop liquidation happens through the liquidator vault."

> "As described above, **the majority of liquidations on Hyperliquid are sent directly to the
> order book.** This allows all users to compete for the liquidation flow, and allows the
> liquidated user to keep any remaining margin."

And the liquidator vault is a component of HLP:

> "Backstop liquidations on Hyperliquid are democratized through the liquidator vault, which
> is a component strategy of HLP. Positions that are below 2/3 of the maintenance margin can
> be taken over by the liquidator vault."
> (<https://hyperliquid.gitbook.io/hyperliquid-docs/hypercore/vaults/protocol-vaults.md>, read
> 2026-09-06 — the page describes HLP as performing liquidations but **publishes no address**;
> the address still has to be obtained by probing a known backstop fill on mainnet, as
> `docs/DATA_SOURCES.md` already says.)

So: a market liquidation is an ordinary market order resting *under the liquidated user's own
address*, matched against whoever is on the book. The liquidator vault is not a counterparty
to it and never sees the fill. Subscribing `userFills` to the vault address therefore yields
**only the `method: "backstop"` tail** — by Hyperliquid's own wording, the minority. That
subset is still free, exact and instant, and is worth capturing (§4), but it is emphatically
not "HL liquidations in real time". `docs/DATA_SOURCES.md` already anticipated this; the
docs quotes above are the primary confirmation.

*Two things not established from a primary source, both cheaply answerable from the lake:*

1. **The market/backstop split.** HL does not publish it. `node_fills` carries
   `liquidation.method`, so one query over the year already in the lake gives the exact ratio.
   This is the number that decides whether the free vault subscription is a curiosity or most
   of the signal. **Unverified here.**
2. **Whether `liquidation` is populated on both counterparties' fills or only the liquidated
   user's.** The docs define `liquidation?: FillLiquidation` on `WsFill` and say nothing about
   which side carries it. This matters more than it looks: HLP "provides liquidity to
   Hyperliquid through multiple market making strategies" as well as performing liquidations
   (protocol-vaults page), so if the flag appears on *both* sides, HLP's market-making arm
   would surface some `method: "market"` liquidations too, and the free subscription would
   catch more than backstops. `node_fills` has both sides of every trade; grouping by `tid`
   answers it. **Unverified — do not assume either way.**

### Explorer and the explorer RPC

Official docs mention an explorer API exactly once, in rate limits: *"All `explorer` API
requests have a weight of 40. `blockList` has an additional rate limit of 1 per block. Note
that older blocks which have not been recently queried may be weighted more heavily. For
large batch requests, use the S3 bucket instead."* (same rate-limits URL, read 2026-09-06).
There is **no schema documentation** for it, no `explorerBlock`/`explorerTxs` subscription in
the docs or the official SDK, and the last sentence is Hyperliquid telling you to use S3 for
bulk instead. The endpoint `rpc.hyperliquid.xyz/explorer` is referenced by third-party SDKs
but I found no first-party page defining it — **unverified**. What *is* first-party is that
explorer blocks are a distinct dataset from fills: the historical-data page lists
`s3://hl-mainnet-node-data/explorer_blocks` and `s3://hl-mainnet-node-data/replica_cmds` as
"historical explorer blocks and L1 transactions", separate from the `node_fills_by_block`
prefix that carries the liquidation flag
(<https://hyperliquid.gitbook.io/hyperliquid-docs/historical-data.md>, read 2026-09-06).

More importantly, **inferred, not documented**: the explorer surfaces *transactions*, and the
node's transaction stream is `replica_cmds`, described in the README as blocks "parsed as
transactions" containing `actions` — i.e. signed user actions. A liquidation is not a signed
user action; it is generated by the protocol when mark price crosses maintenance margin.
There is no reason to expect it in a transaction list, and no primary source says it is
there. I did not probe the endpoint (this is a research-only task). If anyone wants to spend
ten minutes disproving this, that is the probe to write.

### `activeAssetCtx`

```typescript
interface WsActiveAssetCtx { coin: string; ctx: PerpsAssetCtx; }
type SharedAssetCtx = { dayNtlVlm: number; prevDayPx: number; markPx: number; midPx?: number; };
type PerpsAssetCtx = SharedAssetCtx & { funding: number; openInterest: number; oraclePx: number; };
```

Seven fields per coin: `dayNtlVlm`, `prevDayPx`, `markPx`, `midPx`, `funding`,
`openInterest`, `oraclePx`. **The docs state no update cadence for `activeAssetCtx`** — `bbo`
is documented as "sent only if the bbo changes on a block" and `l2Book` as "pushed on each
block that is at least 0.5 since last push", but `activeAssetCtx` gets no such note. Our own
recorder observes it at block cadence; treat that as recorder evidence, not documentation.
See §4 for what that buys.

### Latency of the public feed

Hyperliquid's only statement about the feed itself is *"Use websockets for lowest latency
realtime data"* (rate limits page, read 2026-09-06) — **no numeric latency figure is published
for the WebSocket feed**. The one first-party number is about the request path, not the data
path: *"For an order placed from a geographically co-located client, end-to-end latency has a
median 0.2 seconds and 99th percentile 0.9 seconds"*
(<https://hyperliquid.gitbook.io/hyperliquid-docs/hypercore/overview.md>, read 2026-09-06).
The figures in `docs/HYPERLIQUID.md` (single-digit ms RTT from Tokyo, ~0.9s median
order-to-fill) come from that file's own earlier research, not from a Hyperliquid SLA. For our
question this is moot: the public feed's latency is irrelevant when the field we want is not
in it.

## 2. A non-validating Hyperliquid node

**Source: <https://github.com/hyperliquid-dex/node/blob/main/README.md>, read 2026-09-06
(fetched raw from `raw.githubusercontent.com/hyperliquid-dex/node/main/README.md`).**

### What it produces

Run `~/hl-visor run-non-validator` with the flags you need:

- `--write-fills` — "Streams fills in the API fills format to
  `~/hl/data/node_fills/hourly/{date}/{hour}`. Also streams TWAP statuses... The `deployerFee`
  field is included for HIP-3 fills." **This is the flag that carries `liquidation`**, because
  "the API fills format" is `WsFill` above. It overrides `--write-trades` if both are set.
- `--write-trades` — `node_trades`, schema in `l1-data-schemas.md`, no liquidation flag.
- `--write-order-statuses` — `node_order_statuses`; "orders can be a substantial amount of
  data".
- `--write-raw-book-diffs`, `--write-misc-events`, `--write-hip3-oracle-updates`,
  `--write-system-and-core-writer-actions`.

One consequence is worth stating plainly for CLAUDE.md's replay-parity rule: the lake's
historical liquidations *are already this exact stream*. Hyperliquid's own historical-data
page says `s3://hl-mainnet-node-data/node_fills_by_block` "has fills which are streamed via
`--write-fills --batch-by-block` from a non-validating node" and that "`node_fills` matches
the API format"
(<https://hyperliquid.gitbook.io/hyperliquid-docs/historical-data.md>, read 2026-09-06).
Running our own node would therefore produce byte-identical records to the ones Q1, Q3 and Q8
were computed on, live instead of a day late — no schema translation, no live-only branch in
`features/`. The same page also warns that the *other* bucket, `hyperliquid-archive` (our
`l2Book` and `asset_ctxs` history), is "uploaded to the bucket approximately once a month.
**There is no guarantee of timely updates and data may be missing.**" It does not state an
upload latency for `node_fills_by_block`; the "a day late" figure is our own observation of the
lake, not a published SLA.

Two flags matter for our latency question and are easy to miss:

- `--stream-with-block-info` — "Writes events **as they are processed instead of once per
  block**, but uses the same data schema as `--batch-by-block` to include block metadata"
  (`{local_time, block_time, block_number, events}`).
- `--disable-output-file-buffering` — "Flush each line immediately when writing output files.
  **This reduces latency** but leads to more disk IO operations."

Together those two are the difference between "a file appears at the end of the hour" and "a
line appears on disk the moment the fill is applied". The `local_time` field gives us exactly
the `rx_ms` that CLAUDE.md's no-lookahead rule demands, with `block_time` alongside as
`ts_ms`. **Latency is therefore one block plus a flush** — the node applies the block, writes
the line, our tailer reads it. The README does not put a number on it; the observable proxy is
the `applied block X` log line. **No published latency figure; inferred from the flag
semantics.**

There is also `--serve-info`, "a local HTTP server at `http://localhost:3001/info` to handle
info requests with the API request/response format... Running a local info server can help
with rate limits and reduces trust assumptions on external operators." Its supported request
list includes **`liquidatable`** — which does *not* appear in the public info-endpoint
documentation
(<https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals.md>,
read 2026-09-06, whose sections cover metadata, asset contexts, account summary, funding,
predicted funding, OI caps, deploy auction, active asset data, builder limits, market status,
annotations and categories — no `liquidatable`). A node operator can poll for accounts that
are *about to be* liquidated, which is strictly ahead of observing the fill. That is a
genuinely different signal from anything in Phase 3 and worth a note for Phase 7. The README
also warns: "the local server only supports a subset of requests that are entirely a function
of local state. In particular, historical time series queries and websockets are not
currently supported."

### Published hardware requirements

Verbatim from the README, read 2026-09-06:

| Role | vCPUs | RAM | Storage |
|---|---|---|---|
| Validator | 32 | 128 GB | 1 TB SSD |
| Non-Validator | **16** | **128 GB** | **500 GB SSD** |

"Currently only Ubuntu 24.04 is supported." "Ports 4001 and 4002 are used for gossip and must
be open to the public. Otherwise, the node IP address will be deprioritized by peers in the
p2p network." "For lowest latency, run the node in Tokyo, Japan."

That last line is a *recommendation for latency*, not a functional requirement, and our use
does not need the lowest latency — we need to see a liquidation within a second or two, and
the strategy's own edge lives at minute scale (`docs/HYPERLIQUID.md`: "The edge is in the
minute-scale reversion, not in speed"). **A node outside Tokyo is fine for this purpose**, at
the cost of gossip RTT — a distant region adds roughly 200ms of network on top of block time, which is
well inside our budget. What is *not* optional is the public gossip ports; a node behind a
restrictive firewall gets deprioritized by peers, which is a data-quality problem, not just a
latency one.

### Disk growth and pruning

"The node writes data to `~/hl/data`. **With default settings, the network will generate
around 100 GB of logs per day**, so it is recommended to archive or delete old files."
(README and `l1-data-schemas.md`, both read 2026-09-06.) That figure is for default settings;
the `--write-*` flags add to it. State snapshots land every 10,000 blocks under
`periodic_abci_states/`, and `--replica-cmds-style recent-actions` "only preserves the two
latest height files", which is the built-in pruning knob for the biggest consumer.

The repo ships its own pruner
(<https://github.com/hyperliquid-dex/node/blob/main/pruner/scripts/prune.sh>, read
2026-09-06): a cron script that deletes everything under `~/hl/data` older than
`HOURS=$((60*48))` — **48 hours** — excluding `visor_child_stderr`. At 100 GB/day that is a
~200 GB steady state, which is what makes the published 500 GB SSD a sane floor rather than a
generous one.

For our purpose we want `node_fills` only, tailed and shipped straight into the Parquet lake,
with everything else deleted far more aggressively than 48 hours.  **Data can be streamed off
and pruned hard** — `--replica-cmds-style recent-actions` plus the official pruner on a much
shorter retention. The disk requirement is a function of retention, and our retention is
"until the line is in Parquet".

### Peering: the part that turns out to be free

A non-validator needs at least one root peer IP in `~/override_gossip_config.json` (README,
read 2026-09-06), and the README publishes **a table of 25 free community root peers** run by
ASXN, B-Harvest, Nansen x HypurrCollective, Hypurrscan, Infinite Field, LiquidSpirit,
Imperator.co, Enigma, TMNT, ValiDAO, Hyperbeat x P2P.org, Luganodes, HypurrCorea and
Purrposeful — 21 in Japan, 2 in Singapore, 1 in South Korea, 1 in Germany. A live list is also
queryable:

```bash
curl -X POST --header "Content-Type: application/json" \
  --data '{ "type": "gossipRootIps" }' https://api.hyperliquid.xyz/info
```

That endpoint is documented in the node README, not in the info-endpoint docs
(<https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint.md>, read
2026-09-06, contains no `gossipRootIps`).

This matters because there is a paid peering market and it is easy to mistake it for a
requirement. The Hyper Foundation runs its own non-validating node as a reliable peer
(<https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/nodes/foundation-non-validating-node.md>,
read 2026-09-06) — in `apne1-az1` on AWS, i.e. our own Tokyo region — and explicitly describes
our exact architecture as its use case:

> "Automated traders can run a non-validating node pointing to the Foundation non-validating
> node. The local non-validating node can record fills and orders with output file buffering
> disabled for real-time streaming data."

But eligibility is **"You must have staked 10,000 HYPE"** and **"You must be Tier 1 or above
in Maker Rebate Tiers, i.e., >0.5% of 14 day weighted maker volume"**. Both are far outside
this project. And the commercial peers on the Foundation's own list
(<https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/nodes/foundation-non-validating-node-for-infra-providers.md>,
read 2026-09-06) are benchmarked by the Foundation at *"less than $1k/month, which covers
compute and egress costs"* — see §3 for the two I priced.

**Paid peering buys mempool delivery, not liquidations.** Alchemy's page is explicit that the
free public root peers "are free and work for committed blocks", and that what peering adds is
`split_client_blocks` for *uncommitted* transactions
(<https://www.alchemy.com/docs/chains/hypercore-peering/overview>, read 2026-09-06). A
liquidation is not a user transaction and is not in the mempool; it appears when the block is
applied. **We need committed blocks only, so free public peers are sufficient.**

### Can a smaller machine work?

**No primary source says yes.** The README publishes 16 vCPU / 128 GB / 500 GB for a
non-validator and nothing else; there is no "minimum" table, no statement that a reduced spec
is supported, and I found no official issue or commit relaxing it. Anything you read
elsewhere claiming a 32 GB box is enough is **anecdotal and not primary** — treat it as
untested. If the owner wants to try a smaller box, that is an experiment with a clear failure
mode (the node falls behind and `applied block X` lags), not a documented configuration.

### Cost

The published spec — 16 vCPU and **128 GB of RAM** — is the problem. 128 GB is a
dedicated-server or memory-optimised-instance number, not a small-VPS number, and it is one
tier of machine above anything this project has ever paid for.

A caveat on sourcing before the numbers: **Hetzner and Vultr render their prices in
JavaScript**, so the figures do not appear in the fetched page text (Hetzner's AX102 page,
read 2026-09-06, shows "128 GB up to 192 GB DDR5 ECC", "2x 1.92 TB NVMe DC-Edition",
"Unlimited" traffic, locations FSN1 and HEL1 — but renders no price server-side; Vultr's
pricing page returned HTTP 403 to a plain fetch). Where a price could not be read off a
first-party page it is marked **unknown** below rather than estimated.

| Provider | Spec | Monthly list price | Notes | Source, read 2026-09-06 |
|---|---|---|---|---|
| OVHcloud Rise-Game-2 | AMD Ryzen 7 5800X, 8c/16t, **up to 128 GB**, 2x960 GB | **"$100 /month"** + **"$100 installation fees"** | Below the 16-vCPU spec on cores; "up to 128 GB" means 128 GB is an upgrade, so $100 is a floor, not the price | <https://eco.ovhcloud.com/en/rise/> |
| OVHcloud RISE-1 | Xeon-E 2386G, 6c/12t, up to 128 GB | **"Starting at $64 /month"** + $64–89 install | Same caveat, and 6 cores is well under spec | <https://eco.ovhcloud.com/en/rise/> |
| Hetzner AX102 | Ryzen 9 7950X3D, 128 GB DDR5 ECC, 2x1.92 TB NVMe, unlimited traffic | **unknown** (JS-rendered) | Falkenstein / Helsinki only — **no Asia-Pacific location**, so ~250ms of gossip RTT | <https://www.hetzner.com/dedicated-rootserver/ax102/> |
| Vultr, Tokyo | ≥128 GB instances | **unknown** (page 403s to a plain fetch) | Tokyo region exists; bandwidth overage terms unread | <https://www.vultr.com/pricing/> |
| AWS ap-northeast-1 | 16 vCPU + 128 GB (r-family) + 500 GB gp3 | **unknown** (pricing page is JS-rendered; the bulk pricing JSON is impractical to fetch here) | This is the region the Foundation node runs in (`apne1-az1`) | <https://aws.amazon.com/ec2/pricing/on-demand/> |

The honest summary: **the cheapest first-party price I could actually read for a 128 GB
machine is $100/month plus a $100 setup fee, on a box that is under the published core count
anyway.** That is double the entire project budget before the existing Tokyo VPS, the
Coinalyze key and the S3 egress. **Every option that meets the published spec blows the
budget.** Nothing found suggests a configuration that does not.

The 128 GB line is doing all the damage. If the requirement were 32 GB this would be a
$30–40/month question and the answer would be "obviously do it". It is worth an owner-level
decision whether to spend one month of a $100 box to find out empirically what a
non-validator actually consumes, because that experiment either unblocks a complete
liquidation feed permanently or closes the question — but that is a purchase, and CLAUDE.md
says ask first.

## 3. Third-party real-time sources

Two distinct products get conflated here, and separating them is most of the answer:

- **Peering** — someone else's node is your gossip peer. You still run your own node, pay for
  your own hardware, and get the data from your own disk. Priced around $500/month.
- **Data feeds** — someone else runs the node and sells you the parsed stream. Priced by API
  tier.

Peering is priced by the Foundation's own benchmark at *"less than $1k/month, which covers
compute and egress costs"*
(<https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/nodes/foundation-non-validating-node-for-infra-providers.md>,
read 2026-09-06), and every vendor I could get a price from lands right there.

### Peering vendors

| Vendor | Price | What it includes | Source, read 2026-09-06 |
|---|---|---|---|
| Alchemy | **"$499 per month. Flat, published, and identical for every customer."** | Reserved Tokyo peers, mempool via `split_client_blocks`, 99.99% SLA | <https://www.alchemy.com/docs/chains/hypercore-peering/overview> |
| Hydromancer | **"500$/mo for peering, including mempool streaming"** | 99.99% uptime, 7-day trial | <https://docs.hydromancer.xyz/peering> |
| Hyperdash | **"$500 per month, or $5,000 per year, including three whitelisted IPs"** | Blocks and state via gossip, mempool txs; discount for staking HYPE to their validator | <https://docs.hyperdash.com/data/direct-sentry-peering> |
| QuickNode | **"$500/month"**, and "an active Quicknode plan is also required" | "3 dedicated peer IPs on Quicknode sentry nodes across multiple datacenters in Tokyo", mempool, 99.99% SLA. Explicitly **not** fills or liquidations — "blocks and the raw mempool"; they point at their managed endpoint for trades/transactions | <https://www.quicknode.com/hyperliquid-peering> |
| ASXN, Proof Group | **unknown** — pages did not render a price to a plain fetch | | <https://hyperscreener.asxn.xyz/direct-sentry-peering>, <https://www.purroofgroup.com/docs/peering> |

The market has converged on $500/month, matching the Foundation's benchmark. **All of them
blow the budget tenfold, and none of them sells what we want** — peering is a gossip-quality
product for people who already run a node, and QuickNode says so in as many words. §2
established that free community root peers are sufficient for committed blocks, which is all
a liquidation needs.

### Data vendors

| Vendor | HL liquidations? | Latency | Price | Source, read 2026-09-06 |
|---|---|---|---|---|
| **Tardis.dev** | **No.** Their own API lists Hyperliquid as supported since `2024-10-29` with `availableChannels: ["trades","l2Book","fastBook","bbo","activeAssetCtx","activeSpotAssetCtx"]` — the public WS feed and nothing more | real-time streaming is "free, no vendor lock-in", because it is a client library onto the exchange's own WebSocket | historical plans from **$650/mo** (Academic) to $6,000/mo (Business), All Exchanges | <https://api.tardis.dev/v1/exchanges> (first-party API), <https://tardis.dev/> |
| **Coinglass** | Not documented for HL on the liquidation endpoint; supported-exchange examples given are Binance and OKX. **No liquidation WebSocket documented.** | n/a | **"$29/mo"** Hobbyist, **"$79/mo"** Startup, **"$299/mo"** Standard, **"$699/mo"** Professional, Enterprise on request. The liquidation-history endpoint's interval limit is `>=4h` on Hobbyist and `>=30m` on Startup; "No Limit" starts at Standard | <https://docs.coinglass.com/reference/liquidation-history>, <https://www.coinglass.com/pricing> |
| **HyperTracker** (Coinmarketman) | **unknown** — the pricing page does not say what data types are covered | n/a | Free (100 tokens/day), Pulse $179, Surge $399, Flow $799, **Stream $1,999** — WebSocket streams only on the top tier | <https://docs.coinmarketman.com/endpoints/rate-limits-and-pricing> |
| **Hypurrscan** | **unknown** — the site rendered no API documentation or pricing to a plain fetch. It runs two of the free public root peers listed in the node README, so it operates nodes, but I found no first-party page offering a liquidation feed | unknown | unknown | <https://hypurrscan.io/> |
| **Hyperdash** | No liquidations API documented; their published product is peering | n/a | see peering table | <https://docs.hyperdash.com/data/direct-sentry-peering> |

The Tardis row is the most informative one in this document. Tardis is a serious market-data
vendor that records everything an exchange publishes, and for Hyperliquid it records exactly
six channels, none of which is liquidations. **That is independent first-party confirmation of
§1: the field does not exist on the public feed, so no one reselling the public feed can have
it.** Any vendor that does have HL liquidations in real time is, necessarily, running a node —
which is the same purchase decision as §2 with someone else's margin on top.

Coinglass is the one to be explicit about because `docs/DATA_SOURCES.md` already rejected it
and this confirms that judgment on the vendor's own pages: the $29 tier caps liquidation
history at 4-hour intervals, unrestricted intervals begin at $299/mo, and Hyperliquid is not
documented as a supported exchange for the liquidation endpoint at all.

**Terms of service: not checked.** No option in this section is recommended, so I did not
spend the budget establishing whether their terms permit automated trading use. The Coinglass
terms URL 404'd on a plain fetch. **If anyone ever proposes buying one of these, read the
terms first — this document does not clear any of them.**

## 4. What the lake already has that could substitute

The recorder already captures Bybit and OKX liquidations, plus Hyperliquid `trades`, `bbo`,
`l2Book` and `activeAssetCtx`. Three things a live detector can see that the Phase 3 proxy
could not, all free:

**Block-cadence `activeAssetCtx` instead of once-a-minute archive sampling.** This is the
single biggest difference, and Q3's own closing line already flagged it: "Rerun both tables at
60 recorder days, where the gates read live block-level values instead of the archive's
one-a-minute sampling." Both of Q3's gates — falling open interest and mark-versus-mid over
its trailing p95 — are computed from `openInterest`, `markPx` and `midPx`, which the WS feed
delivers per block rather than per minute. A liquidation cascade that resolves in fifteen
seconds is a single sample in the archive and roughly thirty samples live. The gates were
doing real work in Phase 3 (they lifted recall from 0.30 to 0.68 and cut qualifying ticks
38-fold) while being fed the coarsest possible version of their inputs. What they do at block
cadence is genuinely unknown and is the cheapest experiment available.

**Open interest deltas at block level.** `openInterest` is a contract-count level, so a drop
between consecutive blocks is closed positions. Forced closes and voluntary closes look the
same in that field, but a *sharp* OI drop coinciding with heavy taker volume in one direction
is the shape of forced flow, and at block cadence you can see whether the drop is one block or
spread over thirty seconds — which is exactly the signature of HL's own partial-liquidation
rule ("only 20% of the position will be sent as a market liquidation order to the book. After
a block where any position of a user is partially liquidated, there is a cooldown period of 30
seconds", liquidations doc, read 2026-09-06). **A 20%-chunk-every-30-seconds staircase in OI
is a large-position liquidation, and it is visible for free.** The archive's one-minute
sampling flattens that staircase into a single step. This is the most promising untested idea
in this document and it costs nothing but recorder days.

**Mark/oracle divergence, which the archive proxy never used.** `PerpsAssetCtx` carries
`markPx`, `oraclePx` *and* `midPx`. Q3's gate used mark-versus-mid. Liquidations trigger on
mark price, and mark "combines external CEX prices with Hyperliquid's book state"
(liquidations doc). `oraclePx` is the external-CEX side of that blend, so `markPx - oraclePx`
and `oraclePx - midPx` separate "HL's book has moved away from the world" from "the world has
moved and HL is following" — and it is the second case that liquidates HL accounts, since
"Wicks on HL alone cannot liquidate; cross-venue moves can" (`docs/HYPERLIQUID.md`). This is a
third gate the Phase 3 proxy did not have, available from a field already being recorded.

**Plus the free exact subset from §1:** `userFills` on the liquidator vault address gives
backstop liquidations exactly, instantly, at zero cost, one of our ten permitted user
subscriptions. Backstops are the tail — by definition the cases where the book could not
absorb the flow — which makes them a small, high-purity label set. Not a trigger on their own,
but a live ground-truth stream to calibrate the derived proxy against, which is what Q3 had to
use S3 for.

## Against the $50/month budget, plainly

The project currently spends its budget on one small Tokyo VPS plus free API keys, with S3
egress on top. Measured against what is left:

- **Fits (free):** `userFills` on the liquidator vault; block-cadence `activeAssetCtx` gates;
  the existing cross-venue Bybit feed; free community gossip root peers if a node ever
  happens. All of §4.
- **Blows the budget outright:** a non-validating node at the published spec — **$100/month
  minimum** for a 128 GB box that is still under the published core count, i.e. 2x the entire
  budget for hardware alone, before the existing VPS. Paid peering at **~$500/month** from all
  four vendors that publish a price. Coinglass at **$299/month** for the tier that removes the
  interval limit, for data that does not document Hyperliquid anyway. HyperTracker's streaming
  tier at **$1,999/month**. Tardis historical at **$650/month**.
- **Cheapest thing that would actually solve the problem:** the node. Everything more
  expensive than the node is worse than the node.

There is no option in this document that both sees every Hyperliquid liquidation in real time
and costs under $50/month. That is the answer to the question as asked.

## Recommendation

Buy nothing and take the two free upgrades first: subscribe `userFills` to the HLP liquidator
vault address for exact, instant backstop liquidations, and re-run Q3's two gates on
block-cadence `activeAssetCtx` instead of the archive's once-a-minute sampling, because both
are strictly better than what Phase 3 measured, both cost zero, and neither needs a decision
from anyone. The complete real-time feed does exist and is unambiguous — a non-validating node
run with `--write-fills --stream-with-block-info --disable-output-file-buffering`, peered to
the free community root peers listed in the node README — but Hyperliquid publishes 16 vCPU /
128 GB RAM / 500 GB SSD for it, no primary source says a smaller machine works, and that spec
prices at multiples of the entire $50/month budget on every provider checked, so the node is a
Phase 7 decision and not a Phase 4 one. Before any of that, run the one query the lake can
already answer — what fraction of `node_fills` liquidations carry `liquidation.method ==
"backstop"` — because if that fraction is large, the free vault subscription is most of the
signal and the whole node question goes away.

## What this document could not establish from a primary source

- The **market/backstop split** of HL liquidations. Not published. Answerable from the lake.
- Whether `liquidation` is set on **both** counterparties' fills or only the liquidated user's.
  Not documented. Answerable from the lake.
- **Whether a smaller machine runs a non-validator.** No official minimum below
  16 vCPU / 128 GB / 500 GB exists, and no official statement relaxes it. Anything claiming
  otherwise is anecdotal.
- **`activeAssetCtx` update cadence.** Undocumented; block cadence is our recorder's
  observation.
- **What the explorer API returns.** No first-party schema. My reasoning that liquidations are
  not in it is inference from `replica_cmds` being *actions*, not documentation. A ten-minute
  probe would settle it.
- **Hetzner, Vultr and AWS list prices** for a 128 GB machine. All three render prices in
  JavaScript or refuse a plain fetch; I would not put a number in this table that I had not
  read off their own page.
- **ASXN and Proof Group peering prices.** Pages rendered no price.
- **Whether HyperTracker or Hypurrscan sell HL liquidations at all.** Neither documents it.
- **Vendor terms of service.** Not checked, because no vendor here is recommended.

## Answer to the open query: how much is backstop?

Run 2026-09-06 over the lake's `liquidations` table, `venue = hyperliquid`, `source =
hl_s3`, the full year:

| method | events | share of events | notional | share of notional |
|---|---:|---:|---:|---:|
| market | 5,569,709 | 98.9% | $35.3bn | 87.6% |
| backstop | 64,173 | 1.1% | $4.98bn | 12.4% |

Inside the minutes that matter, the 16,958 coin-minutes with at least $250k liquidated
(the episode floor), backstop is 14.1% of the notional and only 3.4% of those minutes
contain any backstop fill at all. The free vault subscription would see roughly one cascade
in thirty. It is not a trigger. The node is the only complete real-time source, and the
question is now a purchase decision, not a research one.
