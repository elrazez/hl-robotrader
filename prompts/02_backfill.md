# Prompt 02: Phase 2 historical ingestion

Owner notes: this is the phase that spends money. Before running it you need an AWS
account, an IAM user with read access to the two Hyperliquid buckets (CLAUDE.md section
7 item 7), a billing alarm at $10, and the keys in `.env`. Budget: about $16 for 6
months of fills, $33 for 12. Also 200 to 400 GB of free disk during the pull. Read
"Getting 6 to 12 months of history" in `docs/DATA_SOURCES.md` first.

---

You are working in the liqfade repo. Read `CLAUDE.md` in full, then `docs/DATA_SOURCES.md`
including the section "Getting 6 to 12 months of history", and `docs/ROADMAP.md` Phase 2.

This session is **Phase 2: historical ingestion**. Build the download jobs under
`liqfade/data/ingest/` and the `liqfade backfill` CLI. Do NOT build features, strategy,
backtest or anything else. Phase 1 may or may not be done; this phase only depends on
`liqfade/data/schemas.py` and `liqfade/data/symbols.py`. If those two files do not exist
yet, create them exactly as specified in `prompts/01_recorder.md` deliverable 1, commit
them separately first, and note that Phase 1 must reuse them.

## Hard money rules. These override everything else.

- Every S3 job runs in estimate mode first: list the objects, sum the bytes, print
  `N objects, X GiB, estimated $Y at $0.09/GB`. If the estimate exceeds $5 the job
  refuses to proceed unless the `--yes` flag was passed. Never bypass this.
- All S3 requests set `RequestPayer="requester"`. A missing payer header fails loudly.
- Every completed object is recorded in a manifest (`data/ingest_state/{job}.json`,
  atomic writes) with key, size and row counts. Re-running a job skips completed keys.
  A crash mid-pull loses at most one object.
- Pull newest month first, then walk backwards. If disk free falls under 30 GB, stop,
  print what was fetched, and exit 0 with a warning.
- Raw downloads go to `data/raw/{job}/` and are deleted per object as soon as its rows
  are parsed and written to Parquet, unless `--keep-raw` is passed.

## Decisions already made

- New dependencies allowed for this phase only: `boto3` (S3 with requester-pays) and
  `lz4` (the archive files are LZ4 frames). Add both to the `core` group with major
  version pins and say why in the commit message. Nothing else.
- Output goes into the SAME canonical tables and partition layout the recorder uses
  (`docs/DATA_SOURCES.md`), with `rx_ms = 0` marking historical rows and `source` set
  to `hl_s3`, `tardis`, `hl_api`, or `hydromancer`. Historical and recorded data must
  be indistinguishable to a reader except by those two columns.
- Fills parsing (`node_fills_by_block`): envelope is `local_time`, `block_time`,
  `block_number`, `events[]` of `[user_address, fill]`. Fills come in maker+taker
  pairs; dedup key is `(block_time, coin, tid)`. For the `liquidations` table, take
  only fills carrying a `liquidation` object, dedup by that key, and determine the
  liquidated side from the record whose user equals `liquidation.liquidatedUser`: if
  that record's `side` is `"A"` (sell), longs were liquidated (`side="long"`), if
  `"B"`, shorts (`side="short"`). `method` from `liquidation.method`, `notional_usd =
  px * sz`, `complete = true`. VERIFY this side rule on the first day pulled by
  checking that liquidation direction correlates with price moving down for long
  liquidations; print the check result; if it comes out inverted, flip the rule and
  record the correction in `docs/DATA_SOURCES.md`.
- The same fills also populate the `trades` table (deduped by tid, aggressor side from
  `crossed` and `side`), so one pull fills two tables.
- `hl_archive_l2.py`: `s3://hyperliquid-archive/market_data/{YYYYMMDD}/{H}/l2Book/
  {COIN}.lz4`, only for coins passed via `--coins` (no default; requiring an explicit
  list prevents an accidental whole-market pull), top 20 levels into the `l2` table.
- `hl_candles.py`: public `candleSnapshot`, interval `1m`, paginated 5000 per call,
  walking backwards until the API returns nothing older. Sleep 250ms between calls to
  stay far inside the rate weight. No AWS involved. Free.
- `tardis_free.py`: day-01 CSVs for `binance-futures`, `bybit`, `okex-swap`,
  `hyperliquid`; data types `liquidations` and `trades`; map to canonical schemas via
  `symbols.py`; `rx_ms=0`, `complete` per venue as in the recorder. Skip months where
  the URL 404s (the file legitimately may not exist) and report which.
- `hydromancer.py`: verification job, not a default source. `--check-day YYYY-MM-DD`
  pulls that day from BOTH Hydromancer and `hl_s3`, compares liquidation event count
  and total notional per coin, and writes `reports/data_quality/hydromancer_check.md`
  with the diff. Only if the owner later passes `--use-hydromancer` to the fills job
  does it become the source. Bucket details: discover from Hydromancer's public docs at
  runtime; if the bucket layout cannot be determined without an account, write the job
  as a stub that prints what the owner must look up, and say so in the summary.
- Data quality report: `scripts/research/data_quality.py` scans the lake and writes
  `reports/data_quality/counts.md`: rows per table per day, days with zero rows
  flagged, liquidation count per day plotted as a sparkline in text. Runnable any time.
- CLI: `liqfade backfill <job>` with jobs `s3-fills`, `l2`, `candles`, `tardis`,
  `hydromancer-check`, flags `--start YYYY-MM-DD --end YYYY-MM-DD --coins BTC,ETH
  [--estimate-only] [--yes] [--keep-raw]`. `--estimate-only` prints the cost and
  exits 0. Dates are inclusive, UTC.

## Deliverables

1. `liqfade/data/ingest/` with `common.py` (manifest, S3 client factory, estimate
   logic, disk guard), `hl_s3.py`, `hl_archive_l2.py`, `hl_candles.py`,
   `tardis_free.py`, `hydromancer.py`.
2. `liqfade backfill` CLI wired as above; the old placeholder removed.
3. `scripts/research/data_quality.py`.
4. Tests, all offline, no boto3 network calls (inject a fake client): estimate math
   and the $5 gate; manifest resume skips done keys; fills envelope parsing from a
   small handcrafted lz4 fixture committed under `tests/fixtures/ingest/` (a few KB);
   liquidation dedup counts a maker+taker pair once; the side rule; trades dedup;
   candle pagination walks backwards and stops; Tardis CSV mapping for each of the
   four exchanges from embedded sample rows; disk guard stops the loop; l2 file path
   construction.
5. `.env.example` already lists the AWS variables; verify, do not duplicate.
6. Append to `docs/DECISIONS.md`: boto3 and lz4 added and why; the liquidation side
   rule and how it was verified.

## How to work

- Print a short plan and wait for OK.
- Commit order: schemas/symbols if missing; common + estimate + manifest; hl_s3;
  candles + l2; tardis + hydromancer-check; quality script. `make lint test` green
  before each.
- You have no AWS credentials in this session. Build everything against the fake
  client, run `--estimate-only` code paths in tests, and end with the exact commands
  the owner runs, in order, including the verification day and the full pull.
- No em dashes anywhere.

## Definition of done

`make lint test` green, zero skips. `liqfade backfill s3-fills --start 2026-08-01 --end
2026-08-02 --coins BTC --estimate-only` runs and prints an estimate once the owner has
keys. The owner-run acceptance (not blocking this prompt): one verification day pulled
and side-rule check printed; then 6 to 12 months of fills, candles for the same window,
l2 for the chosen coins; `data_quality.py` shows no unexplained zero-row days; total
egress logged under budget in `docs/DECISIONS.md`.

End with the owner checklist: AWS IAM setup, billing alarm, the run order from
`docs/DATA_SOURCES.md` ("Order to do it in"), expected cost and disk, and how long the
pull will roughly take.
