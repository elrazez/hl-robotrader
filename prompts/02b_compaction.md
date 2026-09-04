# Prompt 02b: compact the backfilled year

Owner notes: the backfill left the lake as 24 to 144 Parquet files per table per day
(about 150,000 files). On the external drive that is ~30 ms per file open, so a full
scan is minutes of opening files before any data is read. This prompt makes the
existing nightly compaction safe over a whole year and runs it. Run it in a fresh chat
after the lake is complete. Data lives on the external drive at `LIQFADE_DATA_DIR` in
`.env`; the drive must be mounted through anylinuxfs with the `ntfs3` driver first
(see `docs/DATA_SOURCES.md`, Disk). Fish shell: wrap commands in
`bash -c 'set -a && source .env && set +a && ...'`.

---

You are working in the liqfade repo. Read `CLAUDE.md` in full, then `docs/DATA_SOURCES.md`
(Partition layout, and the Disk paragraph) and `docs/DECISIONS.md` for 2026-09-04. Then
read `liqfade/recorder/writer.py` (`compact_day`, `write_atomic`, `DAY_SUFFIX`,
`MULTI_VENUE_TABLES`) and the `--compact-only` branch of `cmd_record` in
`liqfade/cli.py`.

Phase: end of Phase 2 (`docs/ROADMAP.md`). Definition of done: every day of every table
in the lake is one file, the row counts are unchanged, the recorder's nightly path still
works, and a timed full-year scan of `l2` is reported before and after.

## What exists

`compact_day(root, date)` folds one day of every table into `{venue}_day.parquet` (or
`all_day.parquet` when a partition holds several venues or the table is in
`MULTI_VENUE_TABLES`). It deletes the hourly inputs only after the output exists and its
row count matches. It is idempotent. It has two problems for a year of history:

1. It concatenates the whole day in memory. `fills_full` on 2025-10-10 (the cascade day)
   is several GB of Parquet, which is tens of GB in Arrow. The owner's laptop has 8 GB.
2. It only runs for one date, from the recorder's CLI.

## Build

1. **Stream by hour.** Rewrite the body of `compact_day` so it never holds more than one
   hour of one table in memory:
   - Group the input files of a partition by the UTC hour parsed from the file name.
     Names are `{venue}_{HH}.parquet`, `{venue}_{HH}_{n}.parquet` and, from the
     backfill's staging mover, `{venue}_{HH}_{n}_{6hex}.parquet`; parse the hour as the
     second underscore field and treat anything unparseable as an error, not hour 0.
   - Iterate hours in ascending order. Per hour, read its files (`partitioning=None`),
     concatenate, sort by `ts_ms`, and write the result as one row group with a
     `pyarrow.parquet.ParquetWriter` opened on a temp path in the same directory.
     Because the writer partitions by UTC hour (`liqfade/recorder/buffer.py`), an hour's
     rows all fall inside that hour, so hour-ordered output is globally ordered by
     `ts_ms`. Assert it: the first `ts_ms` of hour h+1 must be >= the last of hour h;
     on violation raise rather than write a misordered file.
   - `validate_table` every hour's table before writing it.
   - After closing the writer, `os.replace` the temp file onto the final name, re-read
     the footer, and only if `num_rows` equals the sum of the inputs' footers delete the
     inputs. Any exception leaves the inputs untouched and removes the temp file.
   - Keep the signature `compact_day(root: Path, date: str) -> dict[str, int]` and the
     return value, so the recorder's nightly call and its tests are unchanged.
   - Keep the naming rule. Note that `liquidations` and `trades` now hold `hl_s3` rows
     and `tardis` rows for the same day, and `tardis` rows carry other venues; the
     existing rule already resolves that to `all_day.parquet`.

2. **A range command.** Add `liqfade compact --start YYYY-MM-DD --end YYYY-MM-DD
   [--tables a,b,c] [--data-dir PATH]`, oldest first. For each day print one line per
   table with rows and seconds, and a final summary of files before and after. A day
   that is already one file per table is skipped silently. Default tables: everything
   except `fills_full`. `--tables fills_full` compacts the archive too; it is not read
   by research, so it is opt-in and last. Leave `liqfade record --compact-only` as it
   is; it may call the same code.

3. **Tests** in `tests/test_recorder_writer.py` (extend) and `tests/test_cli.py`:
   a partition with `_n` and `_6hex` suffixed names across three hours compacts to one
   file whose rows equal the union and are sorted; a partition mixing `hl_s3` and
   `tardis` rows compacts to `all_day.parquet`; a second run is a no-op; an output row
   mismatch (monkeypatch the writer) keeps every input; the recorder's existing
   compaction tests still pass; the range command skips compacted days and honours
   `--tables`. Unit tests stay offline and under 30 s.

4. `make lint test` green, `mypy` clean on the touched modules.

## Run it

With the drive mounted and `.env` loaded, first time one full-year scan for the record:

```
uv run python -c "import time,glob,pyarrow.parquet as pq,os; L=os.environ['LIQFADE_DATA_DIR']+'/parquet/l2'; fs=glob.glob(L+'/date=*/*.parquet'); t=time.time(); n=sum(pq.read_metadata(f).num_rows for f in fs); print(len(fs),'files',n,'rows',round(time.time()-t,1),'s')"
```

Then, under `caffeinate -is`, in this order and reporting each:

```
uv run liqfade compact --start 2025-08-31 --end 2026-08-30 --tables liquidations
uv run liqfade compact --start 2025-08-31 --end 2026-08-30 --tables trades,l2,tardis_book_snapshot_5,tardis_derivative_ticker
uv run liqfade compact --start 2025-08-31 --end 2026-08-30 --tables fills_full
```

The last one is 130 GB read and written at about 60 MB/s: expect over an hour. It can
be interrupted and re-run; only days already folded are skipped. Re-run the scan timing
afterwards.

## Report back

File counts per table before and after (expected after: one per day: `l2` 343,
`liquidations` 364, `trades` 365, `fills_full` 365), total rows per table unchanged
(compare `total_rows()` of the manifests in `data/ingest_state/` against the compacted
footers for `fills_full`, `liquidations`, `trades`, `l2`), the two scan timings, and
anything that did not compact and why. Do not delete anything that fails verification.
Append a dated note to `docs/DECISIONS.md` with the streaming rewrite and the measured
scan speeds. Next prompt: `prompts/02c_candles.md`.
