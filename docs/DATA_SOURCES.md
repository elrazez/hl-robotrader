# Data sources

Researched 2026-08-30. Budget constraint: under $50/month total. Everything below is free or requester-pays unless marked.

## Summary table

| Need | Source | Cost | Real-time | History | Quality notes |
|---|---|---|---|---|---|
| HL trades, book, BBO, candles, OI, funding, mark | Hyperliquid WebSocket + Info API | Free | Yes | Candles up to 5000 per request; trades not queryable far back | Complete, canonical |
| HL positions and liquidation prices (public, per address) | Hyperliquid Info API `clearinghouseState`, polled | Free | Polling | None: the endpoint answers for now, never for a past block | Every position of any address, `liquidationPx` included; coverage is only the addresses we track |
| HL liquidations (historical) | `s3://hl-mainnet-node-data/node_fills_by_block` (requester pays) | ~$0.09/GB egress; fills ~0.8-1.0 GiB/day all coins | No | Older prefixes `node_fills`, `node_trades` in a different format | Every fill; liquidations carry a `liquidation` object |
| HL liquidations (historical, alt) | Hydromancer Reservoir S3 (requester pays) | Egress only | No | HL crypto perps from Aug 2025, 1s candles, fills, liquidations, ADLs | Free forever per vendor; verify schema before relying on it |
| HL L2 book snapshots (historical) | `s3://hyperliquid-archive/market_data/{YYYYMMDD}/{H}/l2Book/{COIN}.lz4` | Egress only | No | Since 2023 | Snapshots, not incremental |
| HL liquidations (real-time) | Not available on the free public feed. Paid: Quicknode streams, Hydromancer, Dwellir gRPC, 0xArchive, PurrData | Paid | Yes | n/a | See "HL real-time liquidation gap" below |
| Cross-exchange liquidations (real-time) | Binance `!forceOrder@arr`, Bybit `allLiquidation.{symbol}`, OKX `liquidation-orders` | Free | Yes | Only what we record ourselves | Binance and OKX throttle to 1 event/sec/symbol; Bybit publishes all since 2025-02-25 |
| Cross-exchange liquidations (historical, sampled) | Tardis.dev free CSVs for the 1st day of each month, no API key | Free | No | Bybit since 2020-12-18, Binance/OKX similar | 12 sampled days/year per exchange. Validation set, not a training set |
| Cross-exchange OI, funding, predicted funding, liq history at coarse intervals | Coinalyze API (free, 40 calls/min) | Free | Polling | Intraday granularities keep only ~1500-2000 points; daily is long | Good for live OI/funding context, not for scalp history |
| Aggregated liquidation history at fine intervals | Coinglass | Hobbyist $29 serves only 4h+ intervals; 1-min needs $299 tier and gives 6 days | n/a | n/a | Rejected for this project |

## Strategy for building the backtest dataset on this budget

1. **Record everything from day one.** The recorder is Phase 1 and runs 24/7 on the VPS before any strategy work. Every week of recording is a week of clean, complete, self-owned data with true receive timestamps. This becomes the primary backtest set within 4 to 8 weeks.
2. **Pull HL history from S3** for the 6 to 12 months before recording started: fills (for exact liquidations), trades, hourly L2 snapshots for a handful of coins. Estimate egress with the `hyperliquid-data` PyPI tool (`hl-data cost ...`) before pulling. Target under $15 total for the first pull. Pull only the universe coins, not all coins, where the layout allows.
3. **Use Tardis free days** (1st of each month) for Binance, Bybit, OKX and Hyperliquid to sanity-check that cross-exchange liquidation behaviour in the recorded window matches earlier months. Do not train thresholds on these; use them to reject overfit.
4. **Coinalyze** for live cross-exchange OI and funding context, polled every 60s for the universe, and its daily liquidation history for regime context.

Consequence for research: the earliest phase of research (Phase 3) leans on HL-native liquidations from S3. Cross-exchange features become testable once the recorder has a few weeks of data. Plan the feature set so HL-native features carry v0 and cross-exchange features are an additive v1.

## HL real-time liquidation gap

The public `trades` subscription has fields `coin, side, px, sz, hash, time, tid, users[buyer, seller]`. It does not flag liquidations. Fills with a `liquidation` object (`liquidatedUser`, `markPx`, `method: market|backstop`) exist only in node data (S3) and paid streams.

Approach for v0:

- Real-time "forced flow" proxy on HL is derived, not observed: bursts of aggressive taker volume against the direction of the move, combined with `activeAssetCtx.openInterest` drops and mark-vs-mid divergence. Calibrate this proxy against exact S3 liquidations in research and report its precision/recall.
- Backstop liquidations can be detected exactly in real time if one of `users` equals the HLP liquidator vault address. Record the address once it is confirmed (probe on mainnet, do not guess).
- Cross-exchange feeds (Bybit complete, Binance/OKX throttled) are the primary observed liquidation signal live. Cascades are cross-venue events, so this is acceptable for v0.
- Define an `LiquidationFeed` interface in `data/` so a paid HL stream can be plugged in later without touching strategy code.

## Endpoint details

### Hyperliquid

- REST: `POST https://api.hyperliquid.xyz/info`, `POST https://api.hyperliquid.xyz/exchange`
- WS: `wss://api.hyperliquid.xyz/ws`; testnet `wss://api.hyperliquid-testnet.xyz/ws`
- Ping every 20s; server closes idle connections after 60s. Reconnect logic is mandatory; missed data is not replayed except via snapshot on resubscribe.
- Subscriptions used: `trades` (per coin), `l2Book` (per coin, pushed on block, at least 0.5s apart), `bbo` (per coin), `candle` (per coin, `1m`), `activeAssetCtx` (per coin: markPx, oraclePx, midPx, funding, openInterest, dayNtlVlm, premium, impactPxs), `allMids`.
- Info calls used: `metaAndAssetCtxs` (universe selection: dayNtlVlm, openInterest, maxLeverage, szDecimals), `candleSnapshot` (backfill, max 5000 candles per call), `l2Book`, `clearinghouseState`, `openOrders`, `userFills`, `userFillsByTime`, `fundingHistory`.
- **Asset-context archive** (`liqfade backfill asset-ctxs`). `s3://hyperliquid-archive/asset_ctxs/{YYYYMMDD}.csv.lz4`, requester-pays, one file per UTC day. Listed and one day pulled on 2026-09-04: coverage runs from **2023-05-20** through **two days before today**, about **11 MB compressed a day** in 2026, so the year 2025-09-01 to 2026-08-30 is **4.05 GB, roughly $0.36** of egress. Columns, in this order: `time` (ISO 8601 UTC), `coin`, `funding`, `open_interest`, `prev_day_px`, `day_ntl_vlm`, `premium`, `oracle_px`, `mark_px`, `mid_px`, `impact_bid_px`, `impact_ask_px`. **One row per coin per minute**, 1440 per coin per day, across 229 perps on 2026-03-01. Rows land in `asset_ctx` with `source = hl_archive` and `rx_ms = 0`; `prev_day_px` is the one column the canonical schema has no home for and is dropped (it is the previous day's close, recoverable from `candles_1m`). One file per day, `asset_ctx/date=YYYY-MM-DD/hyperliquid_day.parquet`, sorted by `ts_ms`, so the compaction leaves it alone. This is the only history of open interest, funding and the impact spread the lake has until the recorder has run for a while, and Phase 3's cascade gates read all three.
- **The asset-context archive has one hole in our year, and `asset_ctx` is therefore 363 days, not 364.** `asset_ctxs/20260708.csv.lz4` does not exist in the bucket: July 2026 lists 30 files, and 2026-07-07 and 2026-07-09 are both there at their normal 10.5 and 10.3 MB. It is Hyperliquid's gap, not our window and not a listing that paged short, and it is left empty rather than filled from another source, which would put a row in the lake that `source` could not honestly label. **A quality report over 2025-09-01 to 2026-08-30 should expect 363 `asset_ctx` day partitions and one missing day at 2026-07-08.** Two further days are short rather than absent, which is the same kind of gap one level down: 2026-08-27 has 155,440 rows and 2026-08-30 has 119,248, against a full day's 334,080 (232 coins x 1440 minutes). Coverage runs 2023-05-20 to three days back (measured 2026-09-05: the newest file was 2026-09-02).
- **`clearinghouseState` is public for any address, and that is where `hl_positions` comes from.** No auth, no key, no account: post `{"type": "clearinghouseState", "user": "0x..."}` to `/info` for anyone's wallet and every open position comes back with `coin, szi, entryPx, positionValue, marginUsed, maxLeverage, leverage {type, value}` and **`liquidationPx`**, the venue's own computed liquidation level (null when the position has none). It weighs **2** against the 1200/min per-IP budget (`docs/HYPERLIQUID.md`), so 200 addresses a minute is 400, one third of it, and the hourly universe refresh is the recorder's only other info traffic. **There is no history**: the endpoint answers for now and not for a past block, which is why the liquidation map can only be recorded forward and never backtested over the year in the lake. The addresses come from the `users[buyer, seller]` field the public `trades` feed already carries, ranked by cumulative traded notional in `<data_dir>/hl_addresses.json`. See `docs/research/external_bots_scan.md` section 1.
- Universe refresh: every 60 minutes from `metaAndAssetCtxs`. Recorder subscribes to the top N by `dayNtlVlm` plus a fixed core list (BTC, ETH, SOL, HYPE).

### Binance USDⓈ-M futures

- WS: `wss://fstream.binance.com/ws/!forceOrder@arr`
- Payload: `o.s` symbol, `o.S` side (SELL means a long was liquidated), `o.q` qty, `o.p` price, `o.ap` avg price, `o.T` trade time. One snapshot per symbol per 1000ms, the largest order only. Treat as a lower bound on count and notional.

### Bybit v5 linear

- WS: `wss://stream.bybit.com/v5/public/linear`
- Topic: `allLiquidation.{SYMBOL}` per symbol (subscribe in batches). Fields: `T` time, `s` symbol, `S` side (Sell means long liquidated), `v` size, `p` price. Complete since 2025-02-25, pushed every 500ms.

### OKX v5

- WS: `wss://ws.okx.com:8443/ws/v5/public`
- Channel: `liquidation-orders` with `instType: SWAP`. At most one update per second per contract. Same lower-bound caveat as Binance.

### Coinalyze

- Base: `https://api.coinalyze.net/v1/`, header or query `api_key`. 40 calls/min.
- Endpoints: `open-interest-history`, `funding-rate-history`, `predicted-funding-rate-history`, `liquidation-history`, `long-short-ratio-history`, `ohlcv-history`, plus current OI and funding. Symbols are exchange-specific with a suffix (fetch `future-markets` once and cache).

### Tardis free days

- `GET https://datasets.tardis.dev/v1/{exchange}/{dataType}/{year}/{month}/01/{symbol}.csv.gz` with no auth for day 01 of any month. Exchanges of interest: `binance-futures`, `bybit`, `okex-swap`, `hyperliquid`. Data types: `liquidations`, `trades`, `book_snapshot_5`, `derivative_ticker`.
- Hyperliquid `liquidations` and `trades` are **not** pulled from Tardis: `hl_s3` owns both, and Tardis's copy is the same fills under the same `tid`. See "Counting a trade once". Its Hyperliquid `book_snapshot_5` and `derivative_ticker` are pulled; nothing else carries them.
- **Read the two side tables with `union_by_name = true`, always.** They keep Tardis's own columns and declare no schema, so pyarrow infers types per file, and a coin whose prices happen to be whole numbers infers `int64` where every other coin infers `double`. Measured on 2026-03-01: `tardis_derivative_ticker.mark_price` is `BIGINT` for BTC and `DOUBLE` for ETH, HYPE and SOL. A plain `read_parquet([...])` across coins takes the first file's type and **silently truncates** 31.215 to 31. It reads as data rather than as an error, and it cost an hour chasing a phantom 0.5% disagreement against the asset-context archive before the cause was found. `union_by_name = true` promotes to `double` and is correct. Declaring the types in `write_side_table` instead would fix it at the source and needs a re-pull of 384 free files; worth doing if anything beyond validation ever reads these tables.

### HL S3

- Buckets are requester-pays: always send `RequestPayer=requester`. IAM needs `s3:GetObject` and `s3:ListBucket` on `arn:aws:s3:::hyperliquid-archive`, `arn:aws:s3:::hl-mainnet-node-data` and their `/*` resources.
- Files are LZ4-compressed JSON or JSONL. `node_fills_by_block` records use an envelope with `local_time`, `block_time`, `block_number`, `events[]` where each event is `[user_address, fill]`.
- Fill schema (from node data): `coin, px, sz, side (B|A), time, startPosition, dir, closedPnl, hash, oid, crossed, fee, tid, feeToken, cloid?, builder?, builderFee?, twapId?, liquidation?{liquidatedUser, markPx, method}`.
- Fills come in maker and taker pairs. Count a liquidation once, keyed by `(block_time, coin, tid)`.

## Canonical Parquet schemas

All tables carry `ts_ms` (exchange time), `rx_ms` (local receive time, 0 for historical pulls), `source` (string enum), `coin` (HL symbol, e.g. `BTC`) and for cross-exchange rows `venue_symbol` (e.g. `BTCUSDT`).

`source` names the job that produced the row: `recorder`, `hl_s3`, `hl_api`, `hl_archive`, `tardis`, `hydromancer`, `derived` (built from another table in this lake, no network). It is orthogonal to `venue`, which names the exchange the event happened on. It has to be a column rather than a filename convention, because nightly compaction folds every file of a day into one and a recorded day and a backfilled day for the same table land in the same file. `rx_ms = 0` still marks a historical row; `source` says which pull it came from.

- `liquidations`: `venue, coin, venue_symbol, ts_ms, rx_ms, source, side (long|short liquidated), px, sz, notional_usd, method (market|backstop|unknown), complete (bool: venue publishes all events)`
- `trades`: `venue, coin, ts_ms, rx_ms, source, px, sz, side (buy|sell aggressor), tid`
- `bbo`: `coin, ts_ms, rx_ms, source, bid_px, bid_sz, ask_px, ask_sz`
- `l2`: `coin, ts_ms, rx_ms, source, level, bid_px, bid_sz, ask_px, ask_sz` (top 20 levels)
- `asset_ctx`: `coin, ts_ms, rx_ms, source, mark_px, oracle_px, mid_px, funding, open_interest, day_ntl_vlm, premium, impact_bid_px, impact_ask_px`
- `candles_1m`: `coin, ts_ms, source, o, h, l, c, v, n`
- `xchg_ctx` (Coinalyze): `venue, coin, ts_ms, rx_ms, source, open_interest, funding, predicted_funding`
- `fills_full` (the pay-once archive, `hl_s3` only): `coin, ts_ms, rx_ms, source, block_time, block_number, local_time, user_address, px, sz, side (B|A raw), start_position, dir, closed_pnl, hash, oid, crossed, fee, fee_token, tid, cloid, builder, builder_fee, twap_id, liq_user, liq_mark_px, liq_method`. Every field of every Hyperliquid fill for every coin, no universe filter. `liquidations` and `trades` for `source = hl_s3` are derived from this table and can be rebuilt with `liqfade backfill rederive` at no cost. See "Pay once, keep everything" below.
- `hl_positions`: `coin, ts_ms, rx_ms, source, address, szi, entry_px, liquidation_px, position_value_usd, margin_used_usd, leverage, leverage_type, max_leverage`. One row per open position per poll of a tracked address. `szi` is signed as Hyperliquid sends it (negative short) and `liquidation_px` is null when the venue sends null. Single venue, so compaction folds a day to `hyperliquid_day.parquet`.

Partition: `data/parquet/{table}/date=YYYY-MM-DD/{venue}_{hour}.parquet`. Hourly flush from the recorder, nightly compaction into one file per table per day. A buffer that fills before the hour is out writes `{venue}_{hour}_{n}.parquet` alongside it rather than rewriting the hour file; readers glob the partition and compaction folds the day into one file, so nothing downstream needs to know.

## The backtest's feature cache

`data/cache/features/{feature_hash}/date=YYYY-MM-DD/grid.parquet`, written by
`liqfade backtest` and read by the next run. Derived, disposable and gitignored: deleting
the whole `cache/` directory costs wall time on the next run and nothing else.

One file per UTC day, holding every coin the run covered, at
`backtest.feature_grid_ms` resolution (1s by default) and carrying only the columns the
strategy actually reads. `feature_hash` is a sha256 over the config keys a feature column
depends on and nothing else: `features.*`, `cascade.*`, `venues.*`,
`backtest.feature_grid_ms`, `sizing.vol_window_s`, `exhaustion.mark_mid_pct_threshold`,
`exhaustion.max_age_s`, and one key per rule in `strategy.rules.LEVEL_RULES`, which are the
rules that make the grid join price levels at all. That is what makes a parameter sweep
affordable: changing a stop, a target, a sizing fraction or a risk limit reads the year
back and builds nothing, while changing a window length, a threshold, the grid step or the
trigger builds a new tree beside the old one. The list lives in `HASHED_KEYS` in
`liqfade/backtest/source.py`; a key the grid reads and the hash does not cover would
silently hand a run someone else's features, so it is pinned by a test in both directions
and **anything that teaches the grid to read a new config key has to be added there**.

Two things it deliberately does not do. **The run's first day is never cached**: every
other day is built with the previous day in front of it so its rolling windows and its 24h
z-score lookback are full, and the first day has no previous day inside the run window, so
its grid is a function of where the run started rather than of the date. **A cache written
for a narrower coin set is not read by a wider run**: the coin list is stamped in the
file's Parquet metadata, because a coin that never printed that day leaves no rows and
"covered and empty" cannot otherwise be told from "never asked about".

**What it costs on disk.** A year of four coins is 6.8 GB, so roughly 1.7 GB per coin per
year at the 1s default; the 42-coin research universe is therefore around 70 GB a year.
That is the price of a parameter sweep not rebuilding a year of features every time, and
it is why the directory is disposable rather than precious. `backtest.feature_grid_ms: 250`
multiplies it by four, as it does the wall time and the memory.

`liqfade backtest --no-cache` rebuilds every day and writes nothing.


## What the wire actually sends

Captured live on 2026-08-30 with `scripts/probe/ws_payloads.py`. Where this differs from the endpoint notes above, the wire wins and the parsers follow it.

- **HL `activeAssetCtx` carries no timestamp.** The payload is `{"channel":"activeAssetCtx","data":{"coin":..,"ctx":{..}}}` with no `time` field, so the `asset_ctx` table sets `ts_ms = rx_ms`. It is a slowly moving context snapshot rather than an event, so receive time is the honest answer, but any research that joins `asset_ctx` to trades on exchange time must know this.
- **`asset_ctx.funding` is the minute-sampled rate, and the applied hourly rate is the sample one minute before the hour.** `HH:59`, not `HH:00`: at the top of the hour the column has already reset to the next hour's rate, so reading `HH:00` misses the applied rate by up to 1.3e-4/hr. At `HH:59` the worst error over a week on BTC, ETH and HYPE is 1.4e-6/hr, under 0.4% of one 4.5e-4 taker fee, so the backtest cost model uses the column as-is with that offset and needs no separate funding table. Every offset from `-1m` to `-5m` passes the same 2e-5 bar; `-1m` is the tightest, and further back is the rate still converging. Gate: `scripts/research/funding_check.py`, measured 2026-09-05 against the Info API's `fundingHistory`.
- **HL `openInterest` counts both sides of the book, not one.** It is longs plus shorts in coin units, so it is twice the long exposure and twice what most exchanges publish. Measured 2026-09-05, not assumed: the `asset_ctxs` archive's minute-to-minute change divided by the long-exposure sum computed from every fill's `start_position` is 2.000000 at the 5th and 95th percentiles on BTC, ETH, SOL and HYPE, and once the factor is applied the two agree to float noise on all 1,439 minutes of 2026-08-28. Consequence: an account contributes `abs(position)`, a new long against a new short of size 2 raises `openInterest` by 4, and any feature comparing HL open interest to a Binance or Bybit figure has to halve one of them. See `scripts/research/oi_from_fills.py`.
- **HL `candle` sends one object, not a list**, and names the coin `s`: `{"t":..,"T":..,"s":"BTC","i":"1m","o":..,"c":..,"h":..,"l":..,"v":..,"n":..}`. The snapshot on subscribe is a list, so the parser accepts both.
- **HL `candle` pushes the in-progress bar repeatedly, and nothing in the payload says a bar has closed.** The only signal is `t` advancing. The recorder therefore holds one bar per coin and writes it when the next bar opens, so the lake gets exactly one row per `(coin, ts_ms)` and it is the closed bar. This matters because `candles_1m` has no `rx_ms` by design and compaction re-sorts by `ts_ms`: with several rows per bar, nothing downstream could tell which one was final, and the wrong pick has partial `c`, `v` and `n`. Two consequences to know: the newest bar per coin is written up to a minute late, and it is lost if the recorder stops before the next bar opens. Both are acceptable because `candles_1m` is also derivable from `trades` with `liqfade derive candles`, and an in-progress bar in the lake is worse than a missing one. A bar older than the one held is dropped, which is what stops the subscribe snapshot after a reconnect from rewriting bars already in the lake. After a *restart* there is no held bar to compare against, so the snapshot seeds the coin's open bar and emits nothing: folding it wrote every bar it carried a second time, and `candles_1m` has no `rx_ms` and no dedupe, so a day summed over `v` or `n` counted each restart twice. The price is that the bars from a downtime window come from `liqfade derive candles` rather than from the stream.
- **HL `trades` `side` is the aggressor**, `B` buy and `A` sell, confirmed by the shared counterparty address across a burst of `A` trades.
- **The `trades` subscribe answer is a snapshot of that coin's last ~30 trades, and every reconnect resubscribes.** Counted at the 22:42 UTC reconnect of 2026-09-05: about 30 rows per coin whatever the coin's rate, so how far back they reach is 30 trades at that rate, which is 356s on DOGE (3 prints a minute) and 17.7s on PONS (176 a minute). Do not read that `rx_ms - ts_ms` as latency; it is the whole of a "DOGE arrives minutes late" report. The recorder used to write them all again (2,336 duplicate `(coin, tid)` rows in that day) and now drops a trade whose id it has already emitted, keeping the ones that are genuinely new because the same replay is what fills the hole a reconnect leaves. A process restart starts with an empty memory, so a restart day still holds about 30 duplicate rows per coin; `count(*) - count(distinct (coin, tid))` over a day measures it. See `docs/DECISIONS.md`, 2026-09-06.
- **HL closes every WebSocket with `1000 (OK) Expired` after 167 to 181 minutes**, on a clock rather than on an error, seven measured on the box 2026-09-05 to 2026-09-06. Each close used to cost about 18s with nothing recorded. The recorder opens and subscribes a replacement at `venues.hyperliquid.ws_rotate_frac` of `ws_lifetime_s` and overlaps the two, so the changeover records everything; correct `ws_lifetime_s` if HL changes the interval.
- **HL `l2Book` pushes one snapshot per coin every 5.4 seconds, not every 0.5 seconds, and every coin lands within a few milliseconds of the rest.** Measured on the box over 2026-09-05, 18 coins: interval per coin p50 **5,390 ms**, p90 5,511 ms, p99 5,700 ms, max 19-20s, and the same three figures on all 18. The 0.5s in the endpoint notes above is the floor HL promises, and it is what the `hyperliquid-archive` L2 files actually deliver (0.55s), so **the live book is an order of magnitude coarser than the archive's**. It has also moved: 4s measured on 2026-08-30, 5.4s a week later, which is why `recorder.l2_gap_multiple` multiplies the cadence the stream is currently delivering at rather than a number written down here.

  **What this means for `backtest.stale_book_s`.** It is 1.0s, and at a 5.4s cadence *every* live snapshot is stale by that rule the moment it is read, so a paper run against recorded feeds prices every walked fill with `slip_stale_bps` added and the paper cost model no longer matches the historical one. Three ways out, in order of honesty: price scalp fills off `bbo` (p99 316 ms stream-wide, and 185 ms per coin) with `l2` as depth context, which is what `docs/STRATEGY_AND_RISK.md` already assumes; or raise `stale_book_s` above the measured cadence and accept that the staleness penalty then only catches an actually stalled book; or keep 1.0s and read the resulting `stale` share as "this is what trading on HL's public book costs", which is a real fact about the venue rather than a modelling artefact. **Do not raise it silently**: the same key prices the historical year, where the archive's 0.55s snapshots genuinely are fresh, and moving it changes both. Book state at scalp resolution therefore comes from `bbo` plus trades, with `l2` as depth context.

- **OKX swap sizes are in contracts, not coin units, so OKX `notional_usd` is null.** `sz` on `liquidation-orders` counts contracts of `ctVal` each, so `bkPx * sz` is out by up to 100x and is not USD. `notional_usd` means USD or it means null, so both OKX paths (`recorder/streams/okx.py` and `data/ingest/tardis_free.py`) write null. `px`, `sz` and `venue_symbol` are preserved intact, so joining `ctVal` from the instruments endpoint later is a backfill over the lake rather than a re-pull. Until that join exists, OKX contributes event counts but no notional, and cross-venue notional leans on `venues.okx.liq_weight` and the other three venues. OKX inverse (coin margined) swaps such as `BTC-USD-SWAP` are recorded with `coin = null` for the same reason: their size is in USD contracts and folding them into the coin would corrupt every cross-venue total.
- **Bybit `allLiquidation` `S` is confirmed**: `Sell` means a long was liquidated, `Buy` means a short was. Verified against live BTCUSDT and SOLUSDT events.
- **Binance `!forceOrder@arr` delivered nothing from a geo-blocked network.** The connection is accepted and stays open, but no message ever arrives, and neither does `btcusdt@aggTrade`, so this is a silent block rather than a quiet market or a parser bug. The parser is tested against the documented payload. **Confirm Binance liquidations actually flow once the recorder runs in Tokyo**; `liqfade status` showing `binance.liquidations` with `msgs/min = 0` for a busy hour means the block follows the VPS too, and then Binance is worth dropping from `venues` rather than pretending it is a feed.

Symbol mapping lives in `data/symbols.py` (`BTC` <-> `BTCUSDT` on Binance/Bybit, `BTC-USDT-SWAP` on OKX, Coinalyze suffix codes). Unknown symbols are recorded under their venue symbol with `coin = null` rather than dropped.

## Getting 6 to 12 months of history

Written 2026-08-30, so "12 months back" means roughly September 2025. This is the
shopping list for a backtest that covers that window. Phase 2 of `docs/ROADMAP.md` writes
the code that does the pulling; this section is what it will pull and what it will cost.

### The short version

| What | Where | 6 months | 12 months | Notes |
|---|---|---|---|---|
| Hyperliquid exact liquidations and trades | `s3://hl-mainnet-node-data/node_fills_by_block` | about $16 | about $33 | Requester pays. This is the important one |
| Hyperliquid liquidations, pre-parsed | Hydromancer Reservoir S3 | egress only, likely far less | same | Covers Aug 2025 onward. Check this first |
| Hyperliquid 1 minute candles | derived from our `trades` table (`liqfade derive candles`) | free | free | The API keeps only ~3 days of 1m bars; see "Candles and order book" |
| Hyperliquid order book snapshots | `s3://hyperliquid-archive` | varies | varies | Only pull a few coins, hourly. Whole-market is expensive |
| Cross-exchange liquidations | Nowhere affordable | see below | see below | This is the real gap |
| Cross-exchange open interest and funding | Coinalyze free tier | free | free | Daily granularity keeps long history, intraday does not |

Everything downloads to your laptop. None of it needs the server.

### The one thing you cannot buy

Binance, Bybit and OKX broadcast liquidations live and then throw them away. There is no
free historical archive, and the paid ones (Coinglass at $299 a month for minute-level,
and even then only 6 days) are out of budget and out of scope. See `docs/DECISIONS.md`.

What exists instead:

- **Tardis free days.** The 1st of each month, every exchange, no signup:
  `https://datasets.tardis.dev/v1/{exchange}/liquidations/{year}/{month}/01/{symbol}.csv.gz`.
  That is 12 days a year per exchange. Enough to check that your recorded data looks like
  earlier months. Not enough to fit anything on.
- **Your own recorder,** from the day you start it. This is the only way to get a
  continuous cross-exchange liquidation feed.

**What this means for your plan.** A 6 to 12 month backtest is possible right now, but it
can only use Hyperliquid's own liquidations, which come from the S3 fills. That is the
right place to start anyway: `docs/STRATEGY_AND_RISK.md` research question 1 is entirely
about Hyperliquid-native cascades, and question 4 (does a Bybit spike predict a
Hyperliquid bounce) is explicitly a later question. The plan already assumes Hyperliquid
carries v0 and cross-exchange features are an addition.

It also means the recorder is worth starting sooner than it feels. Every week it is not
running is a week of cross-exchange data that cannot be recovered at any price.

### Hyperliquid fills from S3, step by step

This is the main pull. Every fill on the exchange, and liquidations carry a `liquidation`
object naming the liquidated account, the mark price and whether it was a normal or
backstop liquidation.

**You need first:**

1. An AWS account.
2. An IAM user with `s3:GetObject` and `s3:ListBucket` on `arn:aws:s3:::hl-mainnet-node-data`
   and `arn:aws:s3:::hl-mainnet-node-data/*`, plus the same for `hyperliquid-archive` if
   you want order book snapshots.
3. **A billing alarm at $10.** Set this before the first byte moves. Requester-pays means
   every GB is on your card.
4. Those keys in `.env` as `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`.

**Estimate before pulling.** The `hyperliquid-data` package on PyPI has a cost command:

```bash
uvx --from hyperliquid-data hl-data cost --start 2026-02-01 --end 2026-08-01
```

Sanity check on the numbers: fills run 0.8 to 1.0 GiB a day compressed for the whole
market, and S3 egress is about $0.09 a GB. So roughly $0.08 a day, $16 for 6 months, $33
for 12. The roadmap's $15 target fits 6 months, not 12.

**You cannot filter by coin server-side.** The files are laid out by block, not by symbol,
so you pay for every coin's bytes and throw away what you do not want after the download.
Filtering to the universe happens locally.

**Disk.** Budget 200 GB free for a 6 month pull, 400 GB for 12, before the raw files are
turned into the smaller Parquet tables and deleted. The finished year is about 190 GB of
Parquet: 130 GB `fills_full`, 25 GB `l2`, 5 GB `trades`, 50 MB `liquidations`.

**An NTFS drive on macOS must not go through fuse-t.** Measured on the owner's 2.5" USB
drive: fuse-t/ntfs-3g wrote at 4.6 MB/s and cost 270 ms per file open, which makes a
year of hourly Parquet unusable (a full L2 scan was 3.7 hours of file opens). The same
drive through `anylinuxfs` with the Linux `ntfs3` driver does 62 MB/s writes, 81 MB/s
reads and 30 ms per open. Mount it with
`sudo anylinuxfs mount /dev/diskNs1 "/Volumes/<name>" -t ntfs3 --ignore-permissions`;
a volume left dirty by an interrupted write needs `ntfsfix -d /dev/vda` from
`anylinuxfs shell` first. Or format the drive exFAT and skip all of this.

**Pull in order, newest first.** If the money or the disk runs out, you still have the
most recent and most relevant months.

### Third-party sellers of cross-exchange liquidation history (surveyed 2026-08-30)

The question was whether anyone sells a more complete Binance/Bybit/OKX liquidation
history than the free first-of-month Tardis days. The answer has two parts.

**There is nothing more complete to buy for Binance and OKX.** Both exchanges have only
ever published a throttled public stream (one event per symbol per second, largest
order only). Every vendor's "historical liquidations" for those venues is a recording
of that same throttled stream; Tardis says so explicitly for Binance (`forceOrder`,
"only the latest liquidation per symbol in each 1000ms window", since 2020-01-07).
Bybit is complete only since 2025-02-25 (`allLiquidation`); before that it was
throttled too. The only complete liquidation records that exist anywhere are
Hyperliquid's own fills (our S3 pull) and Bybit since Feb 2025 (whoever recorded it).

**Continuous history of the throttled streams does exist, at these prices:**

| Vendor | What | Coverage | Price | Verdict |
|---|---|---|---|---|
| Tardis.dev | Per-event CSVs, all venues | Binance from 2020, Bybit from 2020-12 | Solo $700+/mo, no one-off purchases; quarterly billing unlocks 12 months back | Over budget |
| CryptoStruct | Per instrument-day files: tick trades, full L2, liquidations | Binance USDT-M from 2021-02, Bybit from 2023-07, OKX from 2021-10, Hyperliquid from 2024-04; liquidation events only in 2026+ files | EUR 1 per instrument-day, one-off, Parquet export; EUR 20/mo for 50 credits | Affordable for samples; full coverage of 10 coins x 3 venues x 8 months is ~EUR 7,000 |
| Hyblock Capital | 1-minute aggregated liquidation count and volume per side, 14 exchanges incl. Binance, Bybit, OKX, Hyperliquid | No documented lookback limit | Pricing not published on a fetchable page; API on paid tiers | Cheapest route to 1m cross-venue aggregates if Q4 ever needs them |
| Velo Data | 1-minute candles with exchange breakdown; Binance and Bybit only, no OKX | Yearly plan for full history | $199/mo | Over budget, missing OKX |
| Coinglass | Aggregated | 1-minute interval keeps only days of history on every tier under $699 | $29 to $699/mo | Rejected (see DECISIONS) |
| Amberdata / Kaiko, CoinAPI | Per-event (Amberdata) or aggregated metrics (CoinAPI) | Deep | Enterprise, not published | Out of scope |
| Binance data.binance.vision | Daily `liquidationSnapshot` zips | COIN-margined futures only, from 2023-06-25; USDT-M has none | Free | Marginal: a different, smaller market |
| Exchange REST | OKX `liquidation-orders` keeps 7 days; Binance `allForceOrders` removed; Bybit has none | n/a | Free | Useless for history |

**Decision:** do not buy cross-exchange liquidation history for v0. Hyperliquid's exact
fills carry the research; Coinalyze aggregates carry regime context; the recorder
carries cross-venue data forward from the day it starts. Revisit only if research
question 4 says cross-venue signals matter, and then Hyblock (aggregated) or a small
CryptoStruct order (per-event, 2026 only) are the affordable options. A CryptoStruct
order is also the cheapest way to get full-tick Hyperliquid L2 for a few coin-days if
the hourly S3 snapshots turn out too coarse for the fill model.

### Pay once, keep everything

Standing policy, decided 2026-08-30: the S3 download is bought exactly once. The
ingestion jobs therefore write a lossless `fills_full` table preserving every field of
every fill for every coin (user addresses, position sizes, closed PnL, fees, builder
and TWAP tags, the full liquidation object), with no universe filtering. The canonical
`liquidations` and `trades` tables are derived from it locally and can be rebuilt at
any time with `liqfade backfill rederive` at zero cost. This is what makes later ideas
(whale tracking, liquidation-cluster levels, ADL studies, anything not thought of yet)
free instead of a second egress bill. Budget roughly 1 GB per day of history for it.

### Hydromancer: surveyed, never used, and the job is gone

Hydromancer's Reservoir S3 bucket carries Hyperliquid perps from August 2025 with fills,
liquidations and auto-deleveraging already separated out, free forever per the vendor,
egress only. The plan was to pull one day from both sources, compare the liquidation
count and total notional, and use Hydromancer if they matched.

That never happened. The bucket layout is not published anywhere readable without a
Hydromancer account, so the `hydromancer-check` job's `fetch_day` was a stub that printed
what to look up and exited 2, and the whole S3 pull went ahead unchecked. The job was
removed on 2026-09-05: a comparison that cannot fetch either side is not a check. If a
future pull wants it, the work is the four things `LOOKUP_CHECKLIST` named (bucket name,
region and requester-pays, key layout, and their liquidation column names), and the
comparison and report are recoverable from git.

### Candles and order book

**1 minute candles are derived from our own `trades` table, not downloaded.** The public
`candleSnapshot` endpoint only retains about 3 days of `1m` (and `5m`, `15m`) history;
measured 2026-09-03: 72 hours back returns bars, 96 hours back returns nothing. Only
`1h`, `4h` and `1d` go back a year. The `hl_candles` job therefore writes nothing for a
historical range and is kept only for the last few days. `liqfade derive candles`
builds `candles_1m` from `trades` (every fill on Hyperliquid, so the bars are exact) and
is validated against the API on the days both exist. The live recorder still records
the `candle` stream directly.

Bars land as one file per day, `candles_1m/date=YYYY-MM-DD/hyperliquid_day.parquet`,
already named like a compacted partition so `liqfade compact` leaves them alone. `source`
is `derived`.

Three things about the bars, all measured against the live API on 2026-09-04 rather than
assumed:

- **`v` is coin units, not USD.** It is the plain sum of `sz`.
- **A minute with no trades is a real bar**, not a gap: the API returns `v = 0`, `n = 0`
  and `o = h = l = c` at the previous close (TNSR had 291 such bars in 360 minutes, with
  no break in the `t` sequence). The derivation replicates that, so a window of N bars
  spans N minutes in the backtest exactly as it does live. It fills between a coin's
  first and last trade of the day rather than the whole UTC day, which keeps each day
  file independently rebuildable; for any coin liquid enough to trade the two rules give
  the same thing.
- **A Hyperliquid `tid` is a hash, not a sequence.** Two fills of one sweep can carry
  997296749534590 and 653061949558227 in that order, so ordering a millisecond's trades
  by `tid` picks an arbitrary open and close. The order that matters is the row's
  position in the `trades` file, which is the exchange's own event order. Ordering by
  `tid` matched the API's open on 76% of BTC minutes; file order matches on 100%.

**The year in the lake was rebuilt on 2026-09-04 to fix exactly that.**
`hl_s3.derive_trades` sorted its output by `(ts_ms, tid)` until then, so for
`source = hl_s3` the row order inside a millisecond was arbitrary and the derived `o` and
`c` inherited it: measured on 2026-03-01 against the same day's Tardis rows, whose order
is right, `o` and `c` disagreed on 8 to 19% of minutes by a median of 0.15 to 1 bps and
up to 11 bps, while `h` and `l` agreed to within 0.2%. `fills_full` was never sorted and
still held the true order, so the repair cost nothing: `liqfade backfill rederive` over
the year rebuilt every `hl_s3` row of `trades` and `liquidations` from it in file order,
and `liqfade derive candles --force` rebuilt the bars from those. After the rebuild, on
every minute where our trade set and Tardis's agree, `o` and `c` agree exactly; the
residual disagreement is Tardis's own 0.05% incompleteness. The proof that `fills_full`
really carries the exchange's order, including after compaction, is
`scripts/research/fills_order_check.py`; see docs/DECISIONS.md, 2026-09-04.

Order book snapshots live in `s3://hyperliquid-archive` as one compressed file per coin
per hour, going back to 2023. Do not pull the whole market. Pick the handful of coins the
backtest actually trades and pull hourly snapshots. The backtester needs the book to
model what a taker order would really have paid.

**The archive's snapshot cadence is not constant across the year in the lake, and any
split of it has to account for that.** Through 2026-06-14 it publishes about 155,000
snapshots per coin per day, one every 0.55s. Over the following week it steps down, and
from 2026-06-21 to 2026-08-30 it is about 15,500 a day, one every 5.6s: a tenfold loss of
resolution across the last ten weeks of the range. No day or coin or hour is missing, so
nothing reads as a gap; only the spacing changed. The trap is that the held-out last 25%
of this range begins inside that transition, so a fill model fitted on a 0.55s book and
validated on a 5.6s one will report worse out-of-sample fills for a reason that has
nothing to do with the strategy. Measured 2026-09-05; see docs/DECISIONS.md.

### Counting a liquidation once

Fills arrive in maker and taker pairs, so the same liquidation shows up twice. Key on
`(block_time, coin, tid)` and count once. Getting this wrong doubles every liquidation
number and makes the strategy look far better than it is.

### Counting a trade once

The same trap one level up: two sources cover Hyperliquid and they overlap. `hl_s3`
derives `trades` from every fill on the exchange, and Tardis's free first-of-month days
carry Hyperliquid as well, the same trades under the same `tid`. On those twelve days a
naive `venue == "hyperliquid"` filter doubles `v` and `n` for BTC, ETH, SOL and HYPE.

The rule, decided 2026-09-04, is the liquidation rule again: **`hl_s3` is the only source
of Hyperliquid trades and liquidations.** `tardis_free` no longer requests either one for
Hyperliquid (it still pulls the Hyperliquid side tables, `book_snapshot_5` and
`derivative_ticker`, which nothing else carries), and `liqfade backfill rederive` drops
any Tardis Hyperliquid rows it finds in a partition it rewrites. Tardis was the smaller
set anyway: 541,237 BTC trades against `hl_s3`'s 541,557 on 2026-03-01, and every Tardis
`tid` is present in `hl_s3`.

Consumers still deduplicate on `(coin, tid)`, as `liqfade/data/derive/candles.py` does.
That costs nothing, and it is what will keep a recorded day and a backfilled day of the
same date from counting twice once the recorder and the backfill overlap.

### Order to do it in

1. Set the AWS billing alarm.
2. Pull one day. Confirm the file layout matches this document.
3. Pull 6 months, newest first, and check the daily counts for holes.
4. Pull hourly order book snapshots for the coins you will actually trade.
5. Cross-check against the Tardis free 1st-of-month days.
6. Compact every backfilled day (`liqfade record --compact-only --date ...`): the pull
   leaves 24 or more files per table per day and a year of that is 50,000 file opens
   per scan. Research reads the compacted lake.
7. Derive 1 minute candles from `trades` (`liqfade derive candles`).
8. Run `scripts/research/data_quality.py`. Only then start Phase 3 research.

**Actual spend, 2025-09-01 to 2026-08-30:** fills 227 GiB ($20.43), L2 for four coins
30 GiB ($2.70), plus a few dollars of retries: about $25.
The $15 target in the roadmap was for 6 months; a full year of fills is the 227 GiB.
