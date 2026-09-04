# Prompt 02d: rederive `trades` and `liquidations` in the exchange's order

Owner notes: 02c found that `hl_s3.derive_trades` used to sort ties inside a millisecond
by `tid`, which is a hash, so the year of `trades` in the lake has an arbitrary order
inside each block and the derived candles' `o`/`c` are off on 8 to 19% of minutes. The
code is fixed; the lake is not. `fills_full` still holds the true order, so the repair
is local and free. Run in a fresh chat after `prompts/02c_candles.md`. Drive mounted via
anylinuxfs/ntfs3, `.env` loaded through `bash -c 'set -a && source .env && set +a && ...'`.
The `fills_full` compaction is still running detached (`~/fills_compact.log`, retry loop,
oldest first). Nothing here waits for it: the rederive runs newest first alongside it.

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
- `rederive` is vectorised. The existing `derive_trades` and `derive_liquidations` work on
  Python dicts one row at a time; a year is ~3bn fills and that path is 6 to 10 hours.
  Write polars twins, `derive_trades_frame(fills: pl.DataFrame) -> pl.DataFrame` and
  `derive_liquidations_frame`, in `liqfade/data/derive/from_fills.py`, that read the
  compacted or hourly `fills_full` files of one day with `pl.read_parquet` and produce
  exactly the rows the dict functions produce, in exactly the same order. Order is the
  whole point of this prompt: `maintain_order=True` on every `group_by`, `sort` only on
  `ts_ms` and only with `maintain_order=True`, never on `tid`. The dict functions stay as
  they are; the parser keeps using them and they are the reference. The equality test is
  the contract: on the fixture and on one real day, frame == dict rows, same order.
- Assembling a day: read the existing `trades` (or `liquidations`) day partition through
  `lake_files()`, drop its `source == "hl_s3"` rows (and the Tardis Hyperliquid rows, see
  below), concatenate the derived frame, stable-sort by `ts_ms`, `validate_table`,
  `write_atomic` to `date=YYYY-MM-DD/all_day.parquet` (the compacted name), delete the
  other files in the partition. All Arrow/polars, no `list[dict]`; `replace_source_rows`
  can be replaced or left to the recorder path, whichever is smaller.
- Do not wait for the compaction. It walks oldest first; the rederive walks newest
  first, so they meet once around the middle of the year. A day is one task; take its
  `fills_full` file list at the start of the task, and if any file has vanished by the
  time it is read (the compaction folded that day and deleted the hourly inputs), list
  again and redo the day from the new listing. Nothing else can race: the compaction
  writes only `fills_full`, the rederive writes only `trades` and `liquidations`.
- Memory: one worker holds a whole day, up to ~13M fills x 30 columns on the cascade
  days. Default `--workers 2`; measure 2025-10-10 alone first and raise to 3 or 4 only if
  the peak leaves room.
- Inputs come from `lake_files()`; never a bare `*.parquet` glob (sidecars).
- A day the compaction has not reached yet is still many hourly files, and their names
  do not sort in write order (`hyperliquid_07_10_x` sorts before `hyperliquid_07_2_x`).
  That is fine: the parser batches whole lines and one line is one block, so all fills
  of one millisecond sit in one file. Stream `sorted(lake_files())`, stable-sort the
  assembled day on `ts_ms`, and ties keep file order. Do not wait for the compaction
  to reach a day before rederiving it.
- Tardis's Hyperliquid trades are dropped from `trades` in the same pass: `hl_s3` is the
  one source of Hyperliquid trades and liquidations, the same rule as "Counting a
  liquidation once". Rows with `venue == "hyperliquid"` and `source == "tardis"` do not
  survive the rewrite, and `tardis_free.run` stops writing Hyperliquid `trades` and
  `liquidations` (keep pulling its Hyperliquid side tables). Write a "Counting a trade
  once" paragraph in `docs/DATA_SOURCES.md` next to the liquidation one.
- Days run in a process pool, newest first, one day per task, like the backfill workers.
  A day whose `trades` and `liquidations` partitions are already a single `all_day.parquet`
  newer than every `fills_full` file of that day is skipped unless `--force`, so a crashed
  run resumes.
- Run detached in the same `until ... ; do sleep 30; done` loop the compaction uses; the
  mount times out an open every few hours and the resume rule makes a restart cheap.

## Do

1. Before touching the year, prove that `fills_full` still carries the exchange's order
   after compaction. On three days (2025-10-10, 2026-03-01, one quiet day), for each
   `(user_address, coin)`, walk the rows in `ts_ms` order with a stable sort (file order on a compacted day):
   every fill's `start_position`
   must equal the previous fill's `start_position` plus its signed `sz` (`side == "B"`
   adds, `"A"` subtracts). Count violations with a duckdb window query; the interesting
   ones are pairs with the same `ts_ms`. Expect zero, or a handful explainable by
   settlement events. If it fails, stop and report; do not invent an ordering.
2. Write the polars twins and rewrite `rederive` per the decisions; CLI `liqfade backfill
   rederive --start --end [--force] [--workers 2]`. Tests in `tests/test_derive_from_fills.py`
   and `tests/test_ingest_hl_s3.py`: the twins equal the dict functions row for row and in
   order on the fixture; two fills in one millisecond with descending `tid`s come out in
   file order; a Tardis Hyperliquid row is dropped and a Binance Tardis row on the same day
   is kept; hourly input files that do not sort by name still give the right order; the
   day file name is the compacted one; a second run skips; a vanished input file makes the
   day re-list and succeed. Then the same equality on one real day (2026-03-01, hourly
   files) before anything is written to the lake.
3. Validate on 2026-03-01 before the year: rederive that day, derive its candles into a
   scratch `--data-dir`, and compare against the Tardis-derived reference 02c used. `o`
   and `c` must now match on 100% of the minutes where they disagreed before.
4. Run the year detached, newest first, while the compaction keeps going. Expect about 2
   hours: the days still in hourly files pay ~200 file opens each on this mount, the
   compacted ones are one read. Report rows per table and elapsed time at the end.
5. `liqfade derive candles --start 2025-09-01 --end 2026-08-30 --force`, detached, after
   step 4. Same summary as 02c's year run.
6. `make lint test` green, mypy clean. `docs/DECISIONS.md`: a dated entry with the
   order proof from step 1, the 2026-03-01 before/after match rates, the year's row
   totals and timings. Update the `tid` paragraph so it no longer says the lake is
   wrong.

## Report back

The step 1 violation counts, the step 3 match table, the two year-run summaries, and
what changed in `docs/`. Next prompt: `prompts/02e_quality_gate.md`.
