# Data sources

Researched 2026-08-30. Budget constraint: under $50/month total. Everything below is free or requester-pays unless marked.

## Summary table

| Need | Source | Cost | Real-time | History | Quality notes |
|---|---|---|---|---|---|
| HL trades, book, BBO, candles, OI, funding, mark | Hyperliquid WebSocket + Info API | Free | Yes | Candles up to 5000 per request; trades not queryable far back | Complete, canonical |
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
- Subscriptions used: `trades` (per coin), `l2Book` (per coin, pushed on block, at least 0.5s apart), `bbo` (per coin), `candle` (per coin, `1m`), `activeAssetCtx` (per coin: markPx, oraclePx, funding, openInterest, dayNtlVlm, premium), `allMids`.
- Info calls used: `metaAndAssetCtxs` (universe selection: dayNtlVlm, openInterest, maxLeverage, szDecimals), `candleSnapshot` (backfill, max 5000 candles per call), `l2Book`, `clearinghouseState`, `openOrders`, `userFills`, `userFillsByTime`, `fundingHistory`.
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

### HL S3

- Buckets are requester-pays: always send `RequestPayer=requester`. IAM needs `s3:GetObject` and `s3:ListBucket` on `arn:aws:s3:::hyperliquid-archive`, `arn:aws:s3:::hl-mainnet-node-data` and their `/*` resources.
- Files are LZ4-compressed JSON or JSONL. `node_fills_by_block` records use an envelope with `local_time`, `block_time`, `block_number`, `events[]` where each event is `[user_address, fill]`.
- Fill schema (from node data): `coin, px, sz, side (B|A), time, startPosition, dir, closedPnl, hash, oid, crossed, fee, tid, feeToken, cloid?, builder?, builderFee?, twapId?, liquidation?{liquidatedUser, markPx, method}`.
- Fills come in maker and taker pairs. Count a liquidation once, keyed by `(block_time, coin, tid)`.

## Canonical Parquet schemas

All tables carry `ts_ms` (exchange time), `rx_ms` (local receive time, 0 for historical pulls), `source` (string enum), `coin` (HL symbol, e.g. `BTC`) and for cross-exchange rows `venue_symbol` (e.g. `BTCUSDT`).

- `liquidations`: `venue, coin, venue_symbol, ts_ms, rx_ms, side (long|short liquidated), px, sz, notional_usd, method (market|backstop|unknown), complete (bool: venue publishes all events)`
- `trades`: `venue, coin, ts_ms, rx_ms, px, sz, side (buy|sell aggressor), tid`
- `bbo`: `coin, ts_ms, rx_ms, bid_px, bid_sz, ask_px, ask_sz`
- `l2`: `coin, ts_ms, rx_ms, level, bid_px, bid_sz, ask_px, ask_sz` (top 20 levels)
- `asset_ctx`: `coin, ts_ms, rx_ms, mark_px, oracle_px, mid_px, funding, open_interest, day_ntl_vlm, premium`
- `candles_1m`: `coin, ts_ms, o, h, l, c, v, n`
- `xchg_ctx` (Coinalyze): `venue, coin, ts_ms, rx_ms, open_interest, funding, predicted_funding`

Partition: `data/parquet/{table}/date=YYYY-MM-DD/{venue}_{hour}.parquet`. Hourly flush from the recorder, nightly compaction into one file per table per day.

Symbol mapping lives in `data/symbols.py` (`BTC` <-> `BTCUSDT` on Binance/Bybit, `BTC-USDT-SWAP` on OKX, Coinalyze suffix codes). Unknown symbols are recorded under their venue symbol with `coin = null` rather than dropped.
