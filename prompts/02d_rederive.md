# Prompt 02d: rederive `trades` and `liquidations` in the exchange's order

Owner notes: 02c found that `hl_s3.derive_trades` used to sort ties inside a millisecond
by `tid`, which is a hash, so the year of `trades` in the lake has an arbitrary order
inside each block and the derived candles' `o`/`c` are off on 8 to 19% of minutes. The
code is fixed; the lake is not. `fills_full` still holds the true order, so the repair
is local and free. Run in a fresh chat after `prompts/02c_candles.md`. Drive mounted via
anylinuxfs/ntfs3, `.env` loaded through `bash -c 'set -a && source .env && set +a && ...'`.
The `fills_full` compaction may still be running detached when you start
(`~/fills_compact.log`, retry loop, 365 days when done); the code work does not need it,
the year run does.

---

You are working in the liqfade repo. Read `CLAUDE.md` in full, then `docs/DATA_SOURCES.md`
("Counting a liquidation once", "Order to do it in") and the 2026-09-04 entries of
`docs/DECISIONS.md`, in particular "Hyperliquid's `tid` is a hash, not a sequence" and
"`trades` holds every Hyperliquid trade twice on the twelve Tardis days". Then read
`liqfade/data/ingest/hl_s3.py` (`rederive`, `derive_trades`, `derive_liquidations`,
`_batches`, `write_one_file`), `liqfade/data/ingest/common.py` (`replace_source_rows`,
`day_range`) and `liqfade/recorder/writer.py` (`lake_files`, `write_atomic`,
`compact_day`, `DAY_SUFFIX`).

Phase: end of Phase 2. Definition of done: every `trades` and `liquidations` row from
`hl_s3` rebuilt from `fills_full` in file order, one file per day, `candles_1m` rebuilt
from them, the order verified, all of it written down.

## Decisions already made

- Order inside a millisecond is `fills_full` file order. Nothing in this path sorts by
  `tid`, ever. The only sort is a stable sort on `ts_ms`.
- `rederive` streams. A day of `fills_full` is one file of up to ~13M rows after
  compaction; reading it into Python objects whole is the failure mode that killed the
  parser on 2025-10-10. Use `pq.ParquetFile(path).iter_batches(batch_size=200_000)`,
  derive per batch with the existing functions, write each batch's derived rows as an
  Arrow table to a staging directory on the SSD (`LIQFADE_STAGE_DIR` if set, else
  `data_dir/stage/rederive/`), and only then assemble the day: read the existing day
  file, drop its `source == "hl_s3"` rows, concatenate the staged batches in batch order,
  stable-sort by `ts_ms`, `validate_table`, `write_atomic` to
  `date=YYYY-MM-DD/all_day.parquet` (the compacted name), delete the other files in the
  partition and the staging files. Do the assembly in Arrow, not through
  `replace_source_rows`'s `list[dict]` path; change or replace that helper as needed.
- Inputs come from `lake_files()`; never a bare `*.parquet` glob (sidecars).
- Tardis's Hyperliquid trades are dropped from `trades` in the same pass: `hl_s3` is the
  one source of Hyperliquid trades and liquidations, the same rule as "Counting a
  liquidation once". Rows with `venue == "hyperliquid"` and `source == "tardis"` do not
  survive the rewrite, and `tardis_free.run` stops writing Hyperliquid `trades` and
  `liquidations` (keep pulling its Hyperliquid side tables). Write a "Counting a trade
  once" paragraph in `docs/DATA_SOURCES.md` next to the liquidation one.
- Days run in a process pool of 4, oldest first, one day per task, like the backfill
  workers. A day whose `trades` and `liquidations` files were both written after the
  day's `fills_full` file is skipped unless `--force`, so a crashed run resumes.
- Run detached in the same `until ... ; do sleep 30; done` loop the compaction uses; the
  mount times out an open every few hours.

## Do

1. Before touching the year, prove that `fills_full` still carries the exchange's order
   after compaction. On three days (2025-10-10, 2026-03-01, one quiet day), for each
   `(user_address, coin)`, walk the rows in file order: every fill's `start_position`
   must equal the previous fill's `start_position` plus its signed `sz` (`side == "B"`
   adds, `"A"` subtracts). Count violations with a duckdb window query; the interesting
   ones are pairs with the same `ts_ms`. Expect zero, or a handful explainable by
   settlement events. If it fails, stop and report; do not invent an ordering.
2. Rewrite `rederive` per the decisions; CLI `liqfade backfill rederive --start --end
   [--force] [--workers 4]`. Tests in `tests/test_ingest_hl_s3.py`: two fills in one
   millisecond with descending `tid`s come out in file order; a Tardis Hyperliquid row
   is dropped and a Binance Tardis row on the same day is kept; a batch boundary inside a
   millisecond does not reorder; the day file name is the compacted one; a second run
   skips. Memory test: monkeypatch the batch size to 1 and check the output equals a
   one-pass derivation.
3. Validate on 2026-03-01 before the year: rederive that day, derive its candles into a
   scratch `--data-dir`, and compare against the Tardis-derived reference 02c used. `o`
   and `c` must now match on 100% of the minutes where they disagreed before.
4. Wait for `~/fills_compact.log` to show 365 days (`grep -c '"event":"compacted"'`). Then
   run the year detached, oldest first. Report rows per table at the end and elapsed
   time; a Python-dict derivation over ~3bn fills is hours, not minutes.
5. `liqfade derive candles --start 2025-09-01 --end 2026-08-30 --force`, detached, after
   step 4. Same summary as 02c's year run.
6. `make lint test` green, mypy clean. `docs/DECISIONS.md`: a dated entry with the
   order proof from step 1, the 2026-03-01 before/after match rates, the year's row
   totals and timings. Update the `tid` paragraph so it no longer says the lake is
   wrong.

## Report back

The step 1 violation counts, the step 3 match table, the two year-run summaries, and
what changed in `docs/`. Next prompt: `prompts/02e_quality_gate.md`.
