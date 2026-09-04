# Prompt 02d: the data quality gate into Phase 3

Owner notes: last step before research. It answers one question: is there a hole in the
year that would silently bias the backtest? Run in a fresh chat after
`prompts/02b_compaction.md` and `prompts/02c_candles.md`. Drive mounted via
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
   lake: `fills_full`, `liquidations`, `trades`, `l2`, `candles_1m`,
   `tardis_book_snapshot_5`, `tardis_derivative_ticker`. Per table, per day: rows, files
   (should be 1 after compaction; flag anything else), and for `liquidations` and
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
   - `candles_1m` days equal `trades` days.
   Anything else, in particular a Hyperliquid day with unusually few liquidations or
   trades, or a `fills_full` day whose row count is far below its neighbours, gets
   checked against the manifest in `data/ingest_state/s3-fills.json` (the `rows`
   recorded per object for that day) before it is called a hole.

4. The side rule sanity check on the whole year: for each month, mean signed price move
   in the 60 s into long liquidations and into short liquidations, using the compacted
   tables. `liqfade/data/ingest/hl_s3.py` has `SideRuleCheck` for one day; reuse its
   logic, do not re-download. Long liquidations must follow falling prices every month.
   Put the twelve rows in the report.

5. Write the findings into `docs/DECISIONS.md` under a dated heading: the expected gaps
   above, any real gap found, the side rule table, and the final table of days and rows
   per table. Update the Phase 2 DoD line in `docs/ROADMAP.md` to "done" with the date.

6. Owner checklist to print at the end:
   - Delete the leftover `~/Downloads/03_fills_2025-10.tar` (its contents are verified
     in the lake).
   - Wipe the rented VPS: `ssh old-box 'rm -rf ~/liqfade ~/.local/bin/uv
     ~/.local/share/uv ~/.cache/uv ~/dl ~/httptest'` and remove the provider firewall
     rule for port 8899. Nothing there is needed anymore.
   - In AWS, delete the access key of the read-only IAM user and consider closing the
     account; no further S3 pulls are planned.

Then the owner starts Phase 3 by pasting `prompts/03_research.md` into a fresh chat.
