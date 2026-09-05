# Prompt 02g: the review follow-ups, style and structure only

Owner notes: a two-axis review on 2026-09-05 of everything since `498ec51` (the 02f lake
hardening chat and the 02e quality gate chat, 45 commits) against `CLAUDE.md`,
`docs/DATA_SOURCES.md`, `prompts/02f_lake_hardening.md` and `prompts/02e_quality_gate.md`.

Two findings were correctness or data safety and were fixed the same day in `main`:

- `37ff5f5` every lake write reads its own footer back before it lands. `compact_day` and
  `hl_s3._assemble` had that check; `write_atomic`, which every other lake writer routes
  through, did not, so a truncated file on the owner's NTFS mount replaced a good one and
  the job that wrote it recorded a full row count in its manifest.
- `2692f7f` `_assemble` keeps a foreign row whose `source` is null. The keep filter was an
  AND where the documented rule is an OR, so a Binance row with no `source` was deleted by
  the job that rewrites a year of partitions.

Everything below is style or structure. Nothing here is losing data today. **Run it after
Phase 3 has told us which of these files still matter**, not before: several are in code
that Phase 3's calibration may rewrite anyway, and CLAUDE.md rule 7 forbids refactoring
unrelated code while doing a task.

---

## Hard, in the sense that a documented rule says otherwise

1. **`derive candles` deletes lake files with no confirmation.** CLAUDE.md rule 8 says ask
   before deleting recorded data. `liqfade/data/derive/candles.py:212` `drop_hl_api()`
   unlinks whole `hl_api` files as a side effect of every day `run()` writes, and
   `cli.py cmd_derive` has no `--yes` or `--dry-run`, while the same batch of commits gave
   `compact` and `rederive` exactly that gate (`cli.py:118 CONFIRM_ABOVE_DAYS`). The delete
   itself is what 02f item 1 asked for and is right; the missing gate is the inconsistency.
2. **The asset-ctxs low-disk stop is printed, not logged.** CLAUDE.md section 4: one JSON
   line per event, `event=` always present. `liqfade/data/ingest/hl_asset_ctxs.py:188`
   prints and breaks, where `common.parallel_pull:500` logs `backfill_low_disk` for the
   same case with the right reason ("a detached run's console scrollback is gone by the
   time anyone asks why the pull is short"). One line.
3. **Missing docstrings on new public functions.** CLAUDE.md section 4 wants units and
   edge cases. `liqfade/data/parse.py to_int` (its two siblings have them),
   `hl_asset_ctxs.py object_key`, `hl_candles.py day_of`.

## Spec gaps, partial rather than wrong

4. **02f item 9 is half done.** "Stream with `gzip.open` over the response."
   `tardis_free.read_csv:119` streams the decompression, but `fetch:238` returns
   `response.content`, so the whole compressed day is in memory first, and
   `write_side_table:203` still calls `gzip.decompress(payload)` whole. Peak is
   compressed-day plus rows, not streamed. It fits today; a bigger venue-day would not.
5. **02f item 1's compaction claim holds only for the `hl_api` case.** Any file that is not
   a `_day` file sitting beside `hyperliquid_day.parquet` still reaches `_hour_of`
   (`recorder/writer.py:140`) and raises. That is exactly what the recorder's own
   `candles_1m` and `asset_ctx` hourly files will be once it starts, so item 16's "named
   `hyperliquid_day.parquet` so the compaction leaves it alone" carries the same exposure.
   Decide before Phase 4 whether `compact_day` folds a day file in or refuses the partition
   by name.
6. **02e item 1 hardcodes the side-table file count.** `data_quality.py:47`
   `SIDE_TABLE_FILES = 16` is four venues by four coins. Any re-pull with a different
   `--coins` set flags every side-table day as a hole. Derive it from the coins actually
   present, or name the expectation in the report.

## Judgement calls

7. **`read_fills`'s assertion is stricter than the assumption it defends.**
   `derive/from_fills.py:109` raises on any range overlap between consecutive files, which
   also catches the legitimate case `staged_name`'s own docstring describes: a fill whose
   `time` falls into the neighbouring hour's partition. An uncompacted partition then
   cannot be rederived at all. It fails loud and compaction is the documented fix, so
   nothing is lost, but a path that worked is now closed. Either narrow the check to a
   genuinely shared millisecond, or say in the docstring that rederive requires a compacted
   partition.
8. **One rule, two modules, opposite directions.** "`candles_1m` must not hold `hl_api` and
   derived bars together" is enforced at `hl_candles.py:117 derived_days` (skip the day),
   at `candles.py:212 drop_hl_api` (delete the rows) and again as a per-row filter at
   `hl_candles.py:219`. Duplicated Code plus Shotgun Surgery: a change to the rule touches
   three places.
9. **`parse.py` left a fourth copy behind.** 02f item 15 folded the `_f`/`_i`/`_ms` helpers
   from three ingest modules into `liqfade/data/parse.py`; `recorder/streams/hl.py:64 _f`
   is behaviourally identical and was not folded in, and `parse.py`'s docstring does not
   mention it.
10. **The same five-line lake-file helper in five scripts.** `lake_files(partition_dir(...))`
    mapped to `list[str]` is rewritten in `data_quality.py:148`, `funding_check.py:59`,
    `oi_from_fills.py:103` and inline in `candle_validate.py:44` and
    `drop_tardis_hl.py:61`. One `partition_files()` beside `lake_files` would do.
11. **`hl_asset_ctxs.parse_day` collides with `common.parse_day`.** Same package, imported
    side by side, one returns a `pa.Table` from a `.csv.lz4` and the other a `date` from a
    string. Its `payload: Path | Any` annotation says nothing either (mypy does not cover
    `data/`).
12. **`_assemble` carries five function-local import blocks**, two of them re-importing
    names already at module level (`os` at `hl_s3.py:32`, `lake_files` at `:55`). The
    laziness was for import cost that the module already pays.
13. **`derive_liquidations_frame` still uses the XOR.** `from_fills.py:234`
    `(side == "A") == _has_victim`, where the dict reference falls through to `buy`. They
    agree on every input that exists (side is always `A` or `B`, and polars' nulls
    propagate to the same answer), and 02f item 19 named only the trades XOR, which was
    fixed. But the twins rule says the dict is the reference, so the two should read the
    same way.
14. **`derive candles` leaves a stale day file behind.** `candles.py:294`: a day whose
    `trades` partition has become empty prints "nothing written" and keeps whatever bars
    were there before. Rare, and arguably right, but it is not stated.

## Not findings, recorded so the next review does not raise them again

- `drop_doubled_bybit_eth.py` and `funding_check.py` are scope creep against both specs
  (02e item 3 asked only to classify holes; 02f item 16 asked only to validate before
  reporting). Both were owner-approved and are recorded in `docs/DECISIONS.md`, and both
  are worth keeping as re-runnable gates.
- `data_quality.py --footers-only` was not asked for and is the flag that makes the gate
  usable on a slow mount. Keep.
- The test suite runs in about 23 s of the 30 s budget in CLAUDE.md section 4. Not over,
  but the next 2,500 lines of tests will be.
