# Prompt 02g: the review follow-ups, style and structure only

Owner notes: a two-axis review on 2026-09-05 of everything since `498ec51` (the 02f lake
hardening chat and the 02e quality gate chat, 45 commits) against `CLAUDE.md`,
`docs/DATA_SOURCES.md`, `prompts/02f_lake_hardening.md` and `prompts/02e_quality_gate.md`.

Three findings were fixed the same day in `main`:

- `37ff5f5` every lake write reads its own footer back before it lands. `compact_day` and
  `hl_s3._assemble` had that check; `write_atomic`, which every other lake writer routes
  through, did not, so a truncated file on the owner's NTFS mount replaced a good one and
  the job that wrote it recorded a full row count in its manifest.
- `2692f7f` `_assemble` keeps a foreign row whose `source` is null. The keep filter was an
  AND where the documented rule is an OR, so a Binance row with no `source` was deleted by
  the job that rewrites a year of partitions.
- `f8ade43` `derive candles` asks before it deletes. `drop_hl_api` unlinks the API's bars
  for every day the derivation writes, which is what 02f item 1 asked for, but it happened
  with no `--dry-run` and no confirmation over a long range, where `compact` and `backfill
  rederive` both got that gate in this phase. CLAUDE.md rule 8 does not exempt the third
  command that deletes recorded data.

Everything below is style or structure. Nothing here is losing data today. **Run it after
Phase 3 has told us which of these files still matter**, not before: several are in code
that Phase 3's calibration may rewrite anyway, and CLAUDE.md rule 7 forbids refactoring
unrelated code while doing a task.

---

## Hard, in the sense that a documented rule says otherwise

1. **The asset-ctxs low-disk stop is printed, not logged.** CLAUDE.md section 4: one JSON
   line per event, `event=` always present. `liqfade/data/ingest/hl_asset_ctxs.py:188`
   prints and breaks, where `common.parallel_pull:500` logs `backfill_low_disk` for the
   same case with the right reason ("a detached run's console scrollback is gone by the
   time anyone asks why the pull is short"). One line.
2. **Missing docstrings on new public functions.** CLAUDE.md section 4 wants units and
   edge cases. `liqfade/data/parse.py to_int` (its two siblings have them),
   `hl_asset_ctxs.py object_key`, `hl_candles.py day_of`.

## Spec gaps, partial rather than wrong

3. **02f item 9 is half done.** "Stream with `gzip.open` over the response."
   `tardis_free.read_csv:119` streams the decompression, but `fetch:238` returns
   `response.content`, so the whole compressed day is in memory first, and
   `write_side_table:203` still calls `gzip.decompress(payload)` whole. Peak is
   compressed-day plus rows, not streamed. It fits today; a bigger venue-day would not.
4. **02f item 1's compaction claim holds only for the `hl_api` case.** Any file that is not
   a `_day` file sitting beside `hyperliquid_day.parquet` still reaches `_hour_of`
   (`recorder/writer.py:140`) and raises. That is exactly what the recorder's own
   `candles_1m` and `asset_ctx` hourly files will be once it starts, so item 16's "named
   `hyperliquid_day.parquet` so the compaction leaves it alone" carries the same exposure.
   Decide before Phase 4 whether `compact_day` folds a day file in or refuses the partition
   by name.
5. **02e item 1 hardcodes the side-table file count.** `data_quality.py:47`
   `SIDE_TABLE_FILES = 16` is four venues by four coins. Any re-pull with a different
   `--coins` set flags every side-table day as a hole. Derive it from the coins actually
   present, or name the expectation in the report.

## Judgement calls

6. **`read_fills`'s assertion is stricter than the assumption it defends.**
   `derive/from_fills.py:109` raises on any range overlap between consecutive files, which
   also catches the legitimate case `staged_name`'s own docstring describes: a fill whose
   `time` falls into the neighbouring hour's partition. An uncompacted partition then
   cannot be rederived at all. It fails loud and compaction is the documented fix, so
   nothing is lost, but a path that worked is now closed. Either narrow the check to a
   genuinely shared millisecond, or say in the docstring that rederive requires a compacted
   partition.
7. **One rule, two modules, opposite directions.** "`candles_1m` must not hold `hl_api` and
   derived bars together" is enforced at `hl_candles.py:117 derived_days` (skip the day),
   at `candles.py:212 drop_hl_api` (delete the rows) and again as a per-row filter at
   `hl_candles.py:219`. Duplicated Code plus Shotgun Surgery: a change to the rule touches
   three places.
8. **`parse.py` left a fourth copy behind.** 02f item 15 folded the `_f`/`_i`/`_ms` helpers
   from three ingest modules into `liqfade/data/parse.py`; `recorder/streams/hl.py:64 _f`
   is behaviourally identical and was not folded in, and `parse.py`'s docstring does not
   mention it.
9. **The same five-line lake-file helper in five scripts.** `lake_files(partition_dir(...))`
   mapped to `list[str]` is rewritten in `data_quality.py:148`, `funding_check.py:59`,
   `oi_from_fills.py:103` and inline in `candle_validate.py:44` and
   `drop_tardis_hl.py:61`. One `partition_files()` beside `lake_files` would do.
10. **`hl_asset_ctxs.parse_day` collides with `common.parse_day`.** Same package, imported
    side by side, one returns a `pa.Table` from a `.csv.lz4` and the other a `date` from a
    string. Its `payload: Path | Any` annotation says nothing either (mypy does not cover
    `data/`).
11. **`_assemble` carries five function-local import blocks**, two of them re-importing
    names already at module level (`os` at `hl_s3.py:32`, `lake_files` at `:55`). The
    laziness was for import cost that the module already pays.
12. **`derive_liquidations_frame` still uses the XOR.** `from_fills.py:234`
    `(side == "A") == _has_victim`, where the dict reference falls through to `buy`. They
    agree on every input that exists (side is always `A` or `B`, and polars' nulls
    propagate to the same answer), and 02f item 19 named only the trades XOR, which was
    fixed. But the twins rule says the dict is the reference, so the two should read the
    same way.
13. **`derive candles` leaves a stale day file behind.** `candles.py:294`: a day whose
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

---

# The 01b recorder-hardening review, 2026-09-05

A second two-axis review, of the 19 commits since `f8ade43` (the recorder hardening chat)
against `CLAUDE.md`, `docs/DATA_SOURCES.md` and `prompts/01b_recorder_hardening.md`. All
17 spec items landed; 14 of them cleanly. Three findings were fixed the same day in
`main`, one commit each:

- `0199bf9` a fresh process no longer alerts that every stream is disconnected. Dropping
  `and stat.last_msg_ms` (item 7) made the never-connected stream visible and also made
  every stream visible for the moment before its first connect returns. The health loop's
  first tick runs before any socket is up, so every restart posted nine Telegram messages,
  each with a different text, so the ten minute dedupe collapsed none of them.
- `967ddfe` a restart's candle snapshot seeds the open bar instead of being folded. The
  held-bar rule (item 5) makes the snapshot harmless across a *reconnect*, because a bar
  is held to compare against. Across a *restart* there is none, so every bar the snapshot
  carried was written again into a table with no `rx_ms` and no dedupe, and a day summed
  over `v` or `n` counted each restart twice.
- `ad0c95e` rows the quarantine file also refuses are counted lost, not saved.
  `quarantined_rows` was incremented before `quarantine_rows` ran, so the one path that
  actually loses data (item 17's full disk, which stops the JSONL append too) reported
  itself as a clean save behind a bare `quarantine_failed` line.

Everything below is style, structure or a resilience gap. Nothing here is losing data
today. Same rule as the 02g list above: run it when the surrounding code is being touched
anyway, not as a refactor of its own.

## Hard, in the sense that a documented rule says otherwise

14. **One quarantine, two log lines.** `recorder/writer.py:159` emits
    `log.error("rows_quarantined", ..., rows=, path=)` and `recorder/main.py` emits
    `log.warning("rows_quarantined", ..., detail=)` for the same event, at two severities
    with two field sets. CLAUDE.md section 4: one JSON line per event. Counting the lines
    double-counts, and a grep for the ERROR misses `detail`. Pick one site.
15. **The Tardis dropped-row count is printed, not logged.** `data/ingest/tardis_free.py`
    ends a run with `print(f"  {dropped} rows dropped: an unknown side token...")`. Same
    rule and same reason as item 1 above: an unknown side token is a venue schema change,
    which is exactly what you want in the journal rather than in scrollback that is gone.
16. **`ControlError`'s docstring is now wrong.** `control/protocol.py` says "Raised by the
    client when the socket is unreachable"; `ControlServer._take_lock` raises it too.
17. **`write_partitioned` gained a raise it does not document.** It routes through
    `Buffers.add`, which now raises `ValueError` on a row with no usable timestamp, and
    `buffer.py` deliberately lets the backfill jobs see it. The buffer says so; the public
    entry point every ingest job actually calls does not.

## Spec gaps, partial rather than wrong

18. **Item 8 is per-coin gaps for `bbo` only.** The item asked to key gap stats by
    `(venue, table, coin)` "for the HL continuous streams"; `PER_COIN_GAP_STREAMS` holds
    only `hyperliquid.bbo` (commit `61def00`, measured: all three counting per coin made
    one quiet minute produce twenty warnings). `trades` and `l2` carry `worst_coin` but
    count gaps at stream level, so the Phase 1 DoD "no gap over 5s on HL trades and BBO
    for the core coins" still is not evidenced per coin for trades. The reasoning is in
    the code; it is recorded here so it does not read as done later.

## Resilience gaps, worth deciding before the box runs for months

19. **The quarantine write is synchronous, inside the WebSocket receive loop.**
    `sink` -> `quarantine` -> `quarantine_rows` opens, appends and closes a file inline.
    One malformed row is nothing. A venue rename that strips the timestamp off every row
    makes it one blocking `open`/`write`/`close` per row on the event loop, plus one entry
    per row on `self._alerts` until the flush tick five seconds later drains it. The sink's
    own docstring says it must not block. Batch the quarantine, or bound the alert list.
20. **`state.json` grew by two 300-sample series and is still rewritten every second.**
    Item 14 put `ws_ping_samples` and `rest_samples` in `LatencyState`, so the snapshot the
    `StateWriter` serialises with `OPT_INDENT_2` and `fsync`s once a second is now tens of
    KB rather than a couple. `/api/state` strips them, which fixes the wire cost, not the
    disk cost. Either write the samples on a slower cadence into their own file, or drop
    the indent.
21. **A refused control socket is a silent restart loop.** `_take_lock` raising
    `ControlError` is right, but it happens before anything is running, so the recorder
    exits with a journal line and no Telegram alert, and `Restart=always` retries it every
    three seconds forever. The refusal should say so out loud once.

## Judgement calls

22. **`to_trades` decides on its map and then bypasses it.** `tardis_free.py` gates on
    `TRADE_SIDES.get(...)` and writes `row["side"]` raw. Identical today only because the
    map is `{"buy": "buy", "sell": "sell"}`. Same twins rule as item 12 above.
23. **Three parallel frozensets keyed on the same `venue.table` string.**
    `streams/base.py` `CONTINUOUS_STREAMS`, `PER_COIN_STREAMS` and `PER_COIN_GAP_STREAMS`
    are all consulted in `_stat` to build one `StreamStats`, and
    `PER_COIN_GAP_STREAMS` being a subset of `PER_COIN_STREAMS` is asserted in a test
    rather than made true by construction. One `dict[str, StreamKind]` collapses all three.
24. **Venue symbol and canonical coin, same `str`.** `WSStream._forget` passes venue
    symbols from `set_symbols` into `StreamStats.forget_coin`, while `record_message`
    populates `_coin_msg_ms` with canonical `row["coin"]`. Correct only because HL's symbol
    equals its coin and HL is the only per-coin venue. Nothing in the types says so.
25. **`""` means "the whole stream".** `StreamStats.check_silence` returns `list[str]` with
    an empty string for the stream-level case, which `health.py` then untranslates twice
    with `label or None`. A primitive standing in for a domain concept.
26. **`STARTUP_GRACE_S` is the one health threshold that is not configurable.** Its three
    siblings (`gap_alert_s`, `silence_alert_s`, `parse_error_alert_1h`) are config fields
    with unit comments; this one is a module constant, and it now gates two branches.
27. **`Health.check_streams` holds no state but thresholds.** It reads `check_silence`,
    `parse_errors_1h`, `silent_s`, `worst_coin`, `last_msg_ms`, `connected` and `name` off
    every `StreamStats`. Feature Envy: "what is wrong with this stream" wants to live on
    the stats object, leaving `Health` the timer and the dispatch.
28. **One unreadable HL frame counts as five parse errors.** `WSStream.handle` cannot
    attribute a message that will not parse to a table, so it counts it against every table
    of the venue. Deliberate and documented, but it means `parse_error_alert_1h: 100` fires
    on HL at twenty bad frames an hour, not a hundred. Either state that in the config
    comment or count it once against the venue.
29. **Coinalyze is rate-limited to death on the VPS.** Seen on the first live run,
    2026-09-05: `coinalyze.xchg_ctx` polls 1,243 markets in chunks of 20 every cycle, the
    free tier allows 40 requests a minute, and every cycle dies at the first `429`, so the
    stream never connects. Poll only the recorder's universe (the coins HL actually
    subscribes), spread the chunks across the interval, and back off on `429` instead of
    abandoning the cycle. Until then the cross-venue context table is empty.
30. **`with_ctx` computes the mark divergence percentile on the 1s grid though `asset_ctx`
    changes once a minute.** Measured 2026-09-05 on the Q3 year sweep: 15s per day, the
    single largest cost in the pass. `_mark_divergence` builds `mark_mid_bps` on every grid
    row and then filters to minute samples for the rolling quantile, so 86,400 rows a coin a
    day are produced to sample 1,440 of them. The threshold could be computed on the minute
    frame and joined onto the grid. **This is a speedup that must not change a number**: the
    per-tick `mark_mid_bps` comparison still has to be evaluated at 1s, and only the
    trailing p95 behind it moves. Any change here reruns Q3 and diffs the trigger counts
    before it is kept.
31. **`hl_forced_proxy` runs twice per day in Q3, 8s each.** The gated and ungated variants
    share every rolling sum and differ only in the final gate mask, so one pass could emit
    both columns. Measured 2026-09-05 on the same sweep. **Also a speedup that must not
    change a number**: the gate-lift table is the difference between the two, and the
    difference is the deliverable, so a shared pass has to be proved identical to two
    separate ones on a real day before it is kept.
