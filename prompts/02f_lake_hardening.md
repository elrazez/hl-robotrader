# Prompt 02f: lake hardening, the ingestion fixes a code review found

Owner notes: a full-codebase review on 2026-09-04 (both axes: repo standards, and the
Phase 0 to 2 spec) turned up a set of ingestion and lake problems that are real but not
urgent enough to interrupt 02d. The seven that could lose money, leak a secret or destroy
recorded data were fixed the same day in `main` (see `git log` around that date). What is
left is here. **Run this in a fresh chat only after 02d has committed and both of its
runs, the `fills_full` compaction and the year rederive, have finished.** Several of these
touch the same files those jobs re-import on every restart. Drive mounted via
anylinuxfs/ntfs3, `.env` loaded through `bash -c 'set -a && source .env && set +a && ...'`.

---

You are working in the liqfade repo. Read `CLAUDE.md` in full, then `docs/DATA_SOURCES.md`
("Canonical Parquet schemas", "Counting a trade once", "Counting a liquidation once",
"HL S3", "Tardis free days", "Order to do it in") and the 2026-09-04 entries of
`docs/DECISIONS.md`, in particular "macOS writes an AppleDouble sidecar", "The Tardis side
tables are not compactable", "Hyperliquid keeps ~3 days of 1m candles", "The resume rule
reads a stamp in the Parquet footer" and "Tardis's Hyperliquid trades are dropped". Then
read `liqfade/data/ingest/common.py`, `hl_s3.py`, `hl_candles.py`, `hydromancer.py`,
`tardis_free.py`, `liqfade/data/derive/candles.py`, `liqfade/recorder/writer.py`
(`lake_files`, `compact_day`, `_hour_of`, `DAY_SUFFIX`), `liqfade/panel/app.py`
(`liquidations_by_venue`) and `scripts/research/data_quality.py`.

Phase: end of Phase 2, after 02d and before `02e_quality_gate.md`. Definition of done:
no reader of the lake can trip over a sidecar, no table can hold the same bar or trade
twice, every destructive command asks first, and the two ingestion paths that load a whole
file into Python are streamed.

## Decisions already made

- **Every commit here is independent.** One concern per commit with its test, `make lint
  test` green before each, in the order below. If one turns out to be bigger than it looks,
  commit the rest and report that one rather than batching.
- **`lake_files()` is the only way to list lake files.** It lives in
  `liqfade/recorder/writer.py` and filters hidden files, which is what keeps the macOS
  AppleDouble sidecars out. A bare `*.parquet` glob anywhere that reads the lake is a bug,
  not a style choice. Staging directories under `tempfile.mkdtemp` are APFS and are not
  the lake, so their globs can stay.
- **`hl_api` and `derived` bars must not coexist for a day.** `candleSnapshot` keeps about
  three days, the derivation covers the year, and both write `candles_1m` into the same
  partition. The derivation owns any day it writes; the API job is for the last few days
  and for validation only.
- **A file name is not a freshness signal.** 02d already learned this: the resume rule
  reads a stamp in the Parquet footer. Derived candles get the same treatment.
- **Only item 16 spends money, and cents.** The fifteen commits touch data already on
  disk (the candle validation day is a Tardis free day). Item 16 pulls Hyperliquid's
  daily asset-context files, a few MB a day, behind the same estimate and $5 gate as
  every S3 job, with a fresh IAM key that lives on this laptop only.
- **The dict functions in `hl_s3.py` stay the reference.** If a change would make
  `derive_trades_frame` and `derive_trades` disagree, the change is wrong.

## Do

Fourteen commits, then a cleanup commit.

1. **Stop `candles_1m` holding every overlap day's bars twice.** `hl_candles.run` writes
   `hyperliquid_HH.parquet` (`source=hl_api`) into the same partition
   `derive/candles.py` writes `hyperliquid_day.parquet` (`source=derived`) into, with no
   dedup, so a reader that does not filter `source` double counts every bar on exactly the
   days the DoD wants validated. It also makes that partition permanently uncompactable,
   because `_hour_of("hyperliquid_day.parquet")` raises. Make `derive candles` delete any
   `hl_api` rows for a day it writes, and make `backfill candles` skip (with a logged
   reason) any day that already has a derived day file. Tests: a partition holding both
   ends up with one bar per `(coin, ts_ms)`; the API job refuses a day the derivation owns;
   `compact_day` on a candles partition no longer raises.
2. **Derived candles carry the `trades` stamp, and a rederive invalidates them.**
   `derive/candles.py` skips on `output.exists() and not force`, which is the same "the
   name says nothing" mistake `_partition_is_current` exists to avoid: a day whose `trades`
   was rederived keeps its old, wrongly ordered bars unless the operator remembers
   `--force`. Stamp the bars' footer with the source `trades` stamp (reuse the
   `REDERIVE_STAMP` mechanism) and compare it on the next run. Test: rederive a day, run
   `derive candles` without `--force`, assert the bars were rebuilt.
3. **Deterministic merged file names.** `merge_staged` names every file
   `{stem}_{uuid4().hex[:6]}.parquet`. Two consequences, both latent rather than observed:
   `sorted(lake_files(...))` does not return an hour's batches in write order
   (`..._07_1_9f0e` sorts before `..._07_ab12`), and the whole 02d repair rests on file
   order being the exchange's order; and a crash between `merge_staged` and
   `manifest.record` makes a re-run append a second copy of the same object's rows under
   different names, undetectably. Derive the name from the S3 key plus a zero-padded batch
   index so it is stable and sorts in write order, and so a redo overwrites instead of
   appending. Test: files from a multi-batch hour sort in batch order; running the same
   object twice leaves one copy.
4. **`lake_files()` in the three readers that bypass it.**
   `scripts/research/data_quality.py:37`, `liqfade/panel/app.py` (`liquidations_by_venue`,
   which globs `date=*/*.parquet`) and `liqfade/data/ingest/hydromancer.py:176`. The
   data quality report is the Phase 2 DoD gate and today it raises on the owner's drive
   the moment it meets a `._x.parquet`. Test: a partition containing a sidecar file is read
   correctly by all three.
5. **The one-off Tardis Hyperliquid delete.** 02d drops `source == "tardis" AND venue ==
   "hyperliquid"` rows only from partitions the rederive rewrites. A day with no
   `fills_full` returns `"empty"` and keeps its Tardis Hyperliquid rows, and 2026-09-01,
   the day the candle validation uses, is exactly that case. Write
   `liqfade backfill drop-tardis-hl --start --end [--dry-run]` (or a script under
   `scripts/research/` if that is smaller) that sweeps every `trades` and `liquidations`
   day, reports counts before and after per day, and refuses to run without `--yes`.
   Report the totals in `docs/DECISIONS.md`.
6. **Move the Tardis side-table guard into `compact_day`.** DECISIONS says `liqfade
   compact` "leaves them out of the default set and refuses them by name rather than
   failing halfway through a range", but that guard lives only in `cmd_compact`, so
   `compact_day(root, date)` with `tables=None` walks every directory and
   `liqfade record --compact-only --date ...`, the command `docs/DATA_SOURCES.md` tells the
   owner to run, raises on any of the twelve Tardis days. Filter to `table_name in SCHEMAS`
   inside `compact_day` so the rule has one home. Test: `compact_day` with `tables=None`
   over a lake containing `tardis_book_snapshot_5` skips it and compacts the rest.
7. **`--dry-run` and a row-count check on `compact` and `rederive`.** Both unlink lake
   files across an arbitrary date range with no estimate and no confirmation, so a mistyped
   `--data-dir` or range rewrites a year of a 227 GiB pull unattended. CLAUDE.md rule 8
   says ask before deleting recorded data. Add `--dry-run` printing what would be
   read, written and deleted per day; require `--yes` above a few days; and give
   `_assemble` the same "output rows equal the sum of input rows before anything is
   unlinked" check `compact_day` already has.
8. **`inspect` reads three columns, not the whole partition.** The parent-side inspection
   in `hl_s3.py` does `pq.read_table(...).to_pylist()` on a whole staged `trades`
   partition, which is the same "millions of rows as Python dicts" failure `BATCH_BYTES`
   was introduced to fix, moved into the parent process. Read only `coin`, `ts_ms`, `px`,
   or sample.
9. **Stream the Tardis CSVs.** `tardis_free.read_csv` does
   `gzip.decompress(payload).decode("utf-8")` and then `list(csv.DictReader(...))`; a
   Binance BTC trades day is gigabytes of Python dicts. Stream with `gzip.open` over the
   response, or use `pyarrow.csv` the way the side tables already do.
10. **Check the body before treating a 400 as coverage.** Every `400` is currently
    recorded as "done, 0 rows" and never retried. Only the documented
    `Data type 'x' is not supported` body means "this venue never carried it"; a
    throttle-shaped or malformed-URL 400 becomes permanent silent data loss. Match the
    body text; anything else fails the object. Test: both shapes.
11. **Log egress in dollars.** Both S3 jobs print GiB at the end of a run and nothing
    else; the "$25 for the year" figure lives only in `docs/DECISIONS.md`. Log
    `bytes / GIB * EGRESS_USD_PER_GB` as a structured `event=egress` line with the byte
    count, and print it in the run summary. The Phase 2 DoD says egress spend is logged.
12. **Delete the Hydromancer job.** `fetch_day` returns `None` and exits 2, so the
    comparison step `docs/DATA_SOURCES.md` describes was never executable and the pull
    went ahead unchecked. About 100 lines of `summarise`/`compare`/`render`/`write_report`
    sit behind it, fully tested and unreachable. Remove `liqfade/data/ingest/hydromancer.py`,
    its tests, its CLI wiring, and the step that references it in `docs/DATA_SOURCES.md`.
    If the owner would rather keep it, the alternative is to get the real bucket layout and
    make `fetch_day` work; ask before choosing that.
13. **`scripts/research/candle_validate.py`.** The Phase 2 DoD says `candles_1m` is
    "validated against the API on the overlap days", and that validation exists only as
    prose in `docs/DECISIONS.md`. Write the script that reproduces those numbers: the
    Tardis free-day comparison (2026-09-01) and the live-`candle`-stream comparison, both
    writing a report under `reports/data_quality/`. It must be re-runnable and must print
    the same shape of table the DECISIONS entry holds.
14. **The uPlot line in `docs/DECISIONS.md`.** CLAUDE.md section 4 specifies
    `lightweight-charts` for liquidation charts and the panel's 24h chart uses uPlot. The
    in-file comment justifies it; section 6 requires the deviation to be logged. One dated
    paragraph.

15. **Cleanup, one commit.** In this area only: mirror the operator `print` warnings in
    `hl_s3.py` and `common.py` through `log.warning` so they survive into the journal
    (keep the prints for the console); fold the three near-identical `_f`/`_i`/`_ms`
    parse helpers in `hl_s3.py`, `hl_archive_l2.py` and `tardis_free.py` into one
    `liqfade/data/parse.py`; drop the `check=None` parameter from `write_one_file`, which
    no production caller passes; `round()` rather than `int()` in the
    `hl_s3` millisecond conversion; make `derive/candles.py`'s `unique(subset=["coin",
    "tid"])` explicit about null `tid` rows rather than collapsing them; and count the
    silent `continue` skips in `hl_archive_l2.py` so an archive format change cannot read
    as a thin coin-hour.

## Check while you are in there

The old one-coin-per-day Tardis side-table files (`{venue}_00.parquet`, against the
correct `{venue}_{coin}_00.parquet`) were deleted from the lake on 2026-09-04; confirm
zero remain. The `docs/DECISIONS.md` line still says they are outstanding, so correct it.

## Two backfills the research needs, after the fifteen commits

Phase 3 calibrates the live cascade detector, whose gates read open interest and
mark-versus-mid from `activeAssetCtx`. The lake has no `asset_ctx` history and the
recorder has not started, so without this the detector is calibrated on trade prints
alone. Hyperliquid's archive fixes that for under a dollar.

16. **`liqfade backfill asset-ctxs`.** The `hyperliquid-archive` bucket, requester-pays,
    holds a daily asset-context file per UTC day at `asset_ctxs/YYYYMMDD.csv.lz4` with
    per-coin open interest, mark price, oracle price, funding and premium, sampled at a
    fixed cadence. **Verify the layout before writing the job**: list the prefix, pull
    one day, print the header, the row count per coin and the cadence, and write those
    three facts into `docs/DATA_SOURCES.md` under Hyperliquid before anything else.
    Then the job, in the shape of `hl_archive_l2.py`: same S3 client and requester-pays
    header, `--estimate-only`, the $5 gate, manifest job `s3-asset-ctxs`, resume by key.
    Rows go into `asset_ctx` with `source = hl_archive`, `rx_ms = 0`, `ts_ms` from the
    file's timestamp column, every schema column the file provides and null for any it
    does not; a column the file has and the schema lacks is documented, not silently
    dropped. One file per day named `hyperliquid_day.parquet`, sorted by `ts_ms`, so the
    compaction leaves it alone. Run the year 2025-09-01 to 2026-08-30. Validate before
    reporting: funding against the Info API's `fundingHistory` on BTC, ETH and HYPE for
    one week (free; expect exact equality at the funding timestamps) and open interest
    and mark against the twelve `tardis_derivative_ticker` days (expect agreement within
    the sampling interval). Both tables go into `docs/DECISIONS.md`. Tests use a
    hand-built two-coin, three-row CSV through the same fake S3 the other jobs use.

17. **`scripts/research/oi_from_fills.py`.** `fills_full` carries `start_position`, `sz`
    and `side` on every fill, so the change in open interest per coin per minute is the
    sum of long exposure opened minus closed across all fills in that minute, exactly,
    block by block. Write it, with a test over five hand-built fills, and compare it
    with the archive's minute-to-minute OI change on 2025-10-10 and one quiet day.
    Agreement proves the archive series; the script stays as the sub-minute source if a
    Phase 3 gate ever needs one. Report the correlation and the largest disagreement.

Owner step before item 16, print it and stop until it is done: create an IAM user for
this laptop only, with `s3:GetObject` and `s3:ListBucket` on `hyperliquid-archive` and
nothing else, put its key in `.env`, and plan to delete it once the pull is verified.
One key per machine; AWS can flag one key used from
two places as stolen.

## Report back

The fourteen commit hashes plus the cleanup one, the before/after counts from the Tardis
Hyperliquid delete, confirmation that no stale side-table files remain, the candle
validation table the new script produces, the archive's cadence, row and dollar totals
with the two validation tables from item 16, item 17's agreement numbers, and what
changed in `docs/`. Next prompt:
`prompts/02e_quality_gate.md`.
