# Prompt 02c: derive the year of 1 minute candles from trades

Owner notes: Hyperliquid's public `candleSnapshot` keeps only about 3 days of 1 minute
bars (measured 2026-09-03: 72 hours back returns data, 96 hours back returns nothing),
so `liqfade backfill candles` wrote 0 rows for the year. We hold every fill on the
exchange in `trades`, so exact bars can be built locally for free. Run in a fresh chat
after `prompts/02b_compaction.md` (the derivation reads the compacted `trades` files;
it works on uncompacted ones too, just slower). Drive mounted via anylinuxfs/ntfs3,
`.env` loaded through `bash -c 'set -a && source .env && set +a && ...'`.

---

You are working in the liqfade repo. Read `CLAUDE.md` in full, then
`docs/DATA_SOURCES.md` (the schema list, "Candles and order book", the `candle` payload
note under Hyperliquid quirks) and the 2026-09-04 entries in `docs/DECISIONS.md`. Then
read `liqfade/data/schemas.py` (`CANDLES_1M`, `validate_table`, the `SOURCE_*`
constants), `liqfade/data/ingest/hl_candles.py` (`to_rows`, `fetch_page`, `walk_back`),
`liqfade/recorder/streams/hl.py` (`_candle`, how the live stream fills the same table)
and `liqfade/recorder/writer.py` (`write_atomic`, `DAY_SUFFIX`).

Phase: end of Phase 2. Definition of done: a `candles_1m` partition for every day of the
year with one file per day, built from `trades`, validated against the API on the days
the API still has, with the comparison written down.

## Build

1. `liqfade/data/derive/__init__.py` and `liqfade/data/derive/candles.py`, pure
   functions plus one runner:
   - `bars_from_trades(trades: polars.DataFrame) -> polars.DataFrame` for one coin-day.
     Input columns are the canonical `trades` schema. Bucket by
     `ts_ms // 60_000 * 60_000`. Per bucket: `o` = `px` of the earliest trade,
     `c` = `px` of the latest, `h` = max, `l` = min, `v` = sum of `sz` in coin units
     (this is what Hyperliquid's own `v` is; confirm against the API in step 4 and
     document if it is not), `n` = number of trades. Order ties inside a millisecond
     by `tid`. Output has exactly the `candles_1m` columns, `source` set to a new
     `SOURCE_DERIVED = "derived"` constant added to `schemas.py` next to the others.
   - Minutes with no trades: decide by measurement in step 4. If the API emits a bar
     with `v=0`, `n=0` and `o=h=l=c=` the previous close for empty minutes, replicate
     that so live and historical bars have the same shape; if it omits them, omit
     them. Write the answer in the module docstring and in `docs/DATA_SOURCES.md`.
   - Rows come only from `venue == "hyperliquid"`. `trades` also holds Tardis rows for
     Binance, Bybit and OKX on twelve days; those must not leak into Hyperliquid bars.
     Any `source` on the Hyperliquid rows is fine (`hl_s3`, `recorder`).
   - `run(data_dir, start, end, coins=None)`: for each day oldest first, read that
     day's `trades` partition with polars (`scan_parquet` on the directory glob,
     filter venue, optionally filter coins), derive per coin, concatenate, sort by
     `(coin, ts_ms)`, `validate_table`, and write one file
     `parquet/candles_1m/date=YYYY-MM-DD/hyperliquid_day.parquet` through
     `write_atomic`. A day whose output already exists is skipped unless `--force`.
     Print one line per day: coins, bars, seconds.

2. CLI: `liqfade derive candles --start --end [--coins BTC,ETH] [--force] [--data-dir]`.
   Add the `derive` group to `cli.py` beside `backfill`; keep `backfill candles` as it
   is (it is still right for the last three days and is the validation reference).

3. Tests in `tests/test_derive_candles.py`, offline, small hand-built trade frames:
   a minute with three trades gives the right ohlc, `v` and `n`; ties inside one
   millisecond order by `tid`; two coins do not mix; a Binance row on the same day is
   excluded; a day with no Hyperliquid trades writes nothing; the writer is atomic and
   a second run skips; `validate_table` passes on the output. Add the property test
   that `l <= min(o, c)` and `h >= max(o, c)` for every bar.

4. **Validate against the API before the year run.** With `.env` loaded:
   `uv run liqfade backfill candles --start <2 days ago> --end <yesterday> --coins BTC,ETH,SOL,HYPE`
   writes API bars with `source = hl_api`. Then derive the same two days into a scratch
   `--data-dir` and compare per coin per minute: count of bars, and for the minutes
   both have, exact equality of `o, h, l, c`, and `v`, `n` within floating tolerance.
   Report the match rate. Anything under 100% on `o/h/l/c` is a bug in the derivation
   or a documented quirk of the API (write which). Record the empty-minute behaviour
   found here and implement it before step 5.

5. Run the year: `caffeinate -is bash -c '... uv run liqfade derive candles --start 2025-09-01 --end 2026-08-30'`.
   Expect roughly 90 million bars across all coins. Report bars per day at the start,
   the middle and the end of the range, total size of `candles_1m`, and elapsed time.

6. `make lint test` green, `mypy` clean on the new module.

## Report back

The API comparison table (per coin: bars compared, ohlc match %, v/n match %), the
empty-minute decision, the year run summary, and a dated `docs/DECISIONS.md` entry.
Next prompt: `prompts/02d_quality_gate.md`.
