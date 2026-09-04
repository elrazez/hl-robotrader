# Prompt 02e: the data quality gate into Phase 3

Owner notes: last step before research. It answers one question: is there a hole in the
year that would silently bias the backtest? Run in a fresh chat after
`prompts/02d_rederive.md` (which itself follows 02b and 02c). Drive mounted via
anylinuxfs/ntfs3, `.env` loaded through `bash -c 'set -a && source .env && set +a && ...'`.

---

You are working in the liqfade repo. Read `CLAUDE.md` in full, then `docs/DATA_SOURCES.md`
("Counting a liquidation once", "Order to do it in", the Tardis row of the table) and the
2026-09-04 entries of `docs/DECISIONS.md`. Then read `scripts/research/data_quality.py`
and `tests/test_data_quality.py`.

Phase: end of Phase 2. Definition of done as stated in `docs/ROADMAP.md` Phase 2: counts
per day with no *unexplained* holes, written down.

## Do

1. Extend `scripts/research/data_quality.py` so the report covers every table now in the
   lake: `fills_full`, `liquidations`, `trades`, `l2`, `candles_1m`, `asset_ctx`,
   `tardis_book_snapshot_5`, `tardis_derivative_ticker`. Per table, per day: rows, files
   (should be 1 after compaction; flag anything else, except the two `tardis_*` side
   tables, which are not compacted and hold one file per venue per coin), and for `liquidations` and
   `trades` the rows per `venue` and per `source`. Keep the existing sparkline and the
   zero-row-day flagging; add a "days present / days expected" line per table. Keep the
   tests green and add one for the per-source breakdown.

2. Run it: `uv run python scripts/research/data_quality.py`. It writes
   `reports/data_quality/counts.md`.

3. Read the report and classify every hole. Known and expected, to be confirmed rather
   than investigated:
   - `l2`: 343 days, not 364. The Hyperliquid archive has no files for some days
     (November 2025 has 22, July 2026 has 24), and July and August 2026 are far smaller
     than other months. That is what the archive holds for BTC, ETH, SOL and HYPE.
   - `liquidations`: 364 days; `fills_full` and `trades`: 365 because the first hour of
     2025-09-01 carries a few rows stamped 2025-08-31.
   - Tardis rows exist on the first of each month only, for Binance, Bybit and OKX;
     Hyperliquid liquidations are absent from Tardis by design.
   - The two `tardis_*` side tables were re-pulled by 02c after the per-coin overwrite
     fix. Expect all four coins per venue on every free day and roughly 281.8M rows in
     `tardis_book_snapshot_5`, 36.6M in `tardis_derivative_ticker`. Fewer means the
     re-pull did not finish: check `~/tardis_repull.log` and rerun the same command
     (the manifest resumes) before calling it a hole.
   - `fills_full` was compacted last, detached (`~/fills_compact.log`, 22,795 files to
     365). If it is still running, say so and do not count its file counts as holes.
   - `candles_1m` days equal `trades` days.
   - `asset_ctx`: 364 days from Hyperliquid's archive (02f item 16), `source ==
     hl_archive`, one file per day, rows per day close to coins x samples at the cadence
     02f recorded in `docs/DATA_SOURCES.md`. Flag a day under 90% of the median.
   - `trades` holds no `venue == hyperliquid, source == tardis` rows after 02d; Tardis rows
     are Binance, Bybit and OKX only. Every `candles_1m` file is newer than its day's
     `trades` file (rebuilt with `--force` after the rederive).
   Anything else, in particular a Hyperliquid day with unusually few liquidations or
   trades, or a `fills_full` day whose row count is far below its neighbours, gets
   checked against the manifest in `data/ingest_state/s3-fills.json` (the `rows`
   recorded per object for that day) before it is called a hole.

   Four things already settled on 2026-09-04, so do not re-investigate them:
   - **Duplicates.** `liquidations` has no `tid`, so keying it on
     `(coin, ts_ms, px, sz, side, method)` finds ~375k "duplicates" on every day of the
     year. They are distinct fills: the liquidation engine fills many positions at one
     mark price in one block. The real test is on `fills_full`, which has `tid` and
     `user_address`: exact duplicates there were 0 on every day checked (cascade days,
     crash-restart days, the laptop/VPS handoff) except 10 rows on 2025-11-13, the
     handoff hour. `liquidations` rows equal the `tid`-deduped liquidation events in
     `fills_full` exactly. Drop those 10 rows if you like; nothing else needs deduping.
   - **The manifest.** The keys for 2025-09-01 to 2025-11-13 were pulled on a rented box
     whose manifest was not copied back before the box was wiped. They were rebuilt
     locally with `"rebuilt": true`, `size: 0`, `at_ms: 0`, `fills_full` rows from the
     hourly footers, `liquidations` rows from the compacted day files, and `trades: 0`.
     The manifest's job is resume (never re-buy the year); its row totals are not a
     source of truth for those keys. Compare against the lake, not the manifest, there.
   - **Order inside a millisecond.** `tid` is a hash. 02d rebuilt `trades` and
     `liquidations` from `fills_full` in file order and proved the order with the
     position chain; the bars were rebuilt after. Do not sort anything by `tid`.
   - **`._*` sidecars.** macOS writes an AppleDouble file beside every Parquet on this
     NFS-backed mount; they hold no data and break naive globs
     (`read_parquet('date=*/*.parquet')`). Every reader in this repo must skip names
     starting with `._`. Delete them freely; they come back on every write.

4. The side rule sanity check on the whole year: for each month, mean signed price move
   in the 60 s into long liquidations and into short liquidations, using the compacted
   tables. `liqfade/data/ingest/hl_s3.py` has `SideRuleCheck` for one day; reuse its
   logic, do not re-download. Long liquidations must follow falling prices every month.
   Put the twelve rows in the report.

5. Write the findings into `docs/DECISIONS.md` under a dated heading: the expected gaps
   above, any real gap found, the side rule table, and the final table of days and rows
   per table. Append the `fills_full` compaction result from `~/fills_compact.log` to the
   2026-09-04 compaction entry; 02b left it running. Update the Phase 2 DoD line in `docs/ROADMAP.md` to "done" with the date.

6. Owner checklist to print at the end:
   - Delete the leftover `~/Downloads/03_fills_2025-10.tar` (its contents are verified
     in the lake).
   - Wipe the rented VPS: `ssh old-box 'rm -rf ~/liqfade ~/.local/bin/uv
     ~/.local/share/uv ~/.cache/uv ~/dl ~/httptest'` and remove the provider firewall
     rule for port 8899. Nothing there is needed anymore.
   - In AWS, delete the access key of the read-only IAM user and consider closing the
     account; no further S3 pulls are planned.

Then the owner starts Phase 3 by pasting `prompts/03_research.md` into a fresh chat.
