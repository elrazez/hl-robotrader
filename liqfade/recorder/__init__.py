"""24/7 market data recorder.

Owns one async task per venue stream (Hyperliquid trades, bbo, l2Book, candle,
activeAssetCtx, allMids; Binance, Bybit and OKX liquidations; a Coinalyze poller),
reconnects with jittered backoff, buffers in memory and flushes hourly into the
canonical Parquet tables under data/parquet/. It also publishes per-stream lag and
gap metrics to the control plane. Governed by docs/DATA_SOURCES.md (schemas,
endpoints, partition layout) and built in Phase 1 of docs/ROADMAP.md. Empty until then.
"""
