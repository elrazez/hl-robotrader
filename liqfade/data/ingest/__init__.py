"""Historical ingestion jobs. One module per source, all driven by `liqfade backfill`.

- `hl_s3`: requester-pays `node_fills_by_block`, the exact Hyperliquid liquidation and
  trade history, plus the lossless `fills_full` archive and the `rederive` job that
  rebuilds the canonical tables from it at zero cost.
- `hl_archive_l2`: requester-pays hourly L2 snapshots from `hyperliquid-archive`.
- `hl_candles`: 1m candles from the free public `candleSnapshot` endpoint.
- `tardis_free`: the free first-of-month CSVs for Binance, Bybit, OKX and Hyperliquid.
- `hydromancer`: a verification job that compares one day against `hl_s3`.

Money rules live in `common.py` and are not optional: every S3 job estimates before it
pulls, refuses over $5 without `--yes`, records every completed object in a manifest so a
crash costs at most one object, and stops when free disk runs low. See the "Hard money
rules" section of prompts/02_backfill.md and "Getting 6 to 12 months of history" in
docs/DATA_SOURCES.md.
"""
