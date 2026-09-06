# Decision log

Append-only. One entry per decision that a future reader would otherwise question. Format: date, decision, why, what would reverse it.

## 2026-08-30

**Custom engine over NautilusTrader / Hummingbot / freqtrade.** Edge is in custom cross-venue liquidation features that these frameworks do not model natively; Nautilus is in a v1 to v2 migration in 2026; a small custom codebase is easier for the owner to reason about. Reverse if execution quality (fill rate, reconciliation bugs) becomes the bottleneck; `strategy/` stays framework-agnostic to keep that door open.

**Coinglass rejected.** Hobbyist tier ($29) serves only 4h and coarser history; minute-level needs the $299 tier and gives 6 days. Not useful for scalp backtests. Reverse only if budget rises and the Standard tier's 90-day 15-minute history becomes relevant for a slower variant.

**Tardis subscription rejected, free first-of-month days used.** Subscriptions are far over budget. The free days give real cross-exchange liquidation samples for validation. Reverse never for v0.

**HL real-time liquidations via proxy plus cross-venue feeds.** The free public HL trades feed does not flag liquidations. Paid streams exist (Quicknode, Hydromancer, Dwellir, 0xArchive, PurrData). For v0 we use Bybit (complete), Binance and OKX (throttled) plus an HL-native forced-flow proxy calibrated against exact S3 liquidations. Reverse if research shows the proxy is poor and cross-venue feeds do not predict HL reversion; then budget a paid HL stream.

**Historical HL data from requester-pays S3 and Hydromancer Reservoir.** Both are egress-only cost. `hyperliquid-data` PyPI tool used for cost estimates. Reverse if egress exceeds $15 for the initial pull; then narrow the coin set.

**Hosting in Tokyo.** HL validators and API run in AWS ap-northeast-1; a distant region adds about 200ms. A small Tokyo VPS fits the budget. Reverse never.

**Cross margin by default, isolated as config option.** Simpler order flow and no margin transfers; strict exposure caps and stops prevent account-level liquidation. Reverse if the owner wants hard per-position loss isolation at the cost of capital efficiency.

**Sizing driven by equity fraction and liquidity caps, leverage as an output.** Owner instruction: capital scales and leverage does not matter, volume and size do.

**Backtest latency default 1.0s.** Independent Tokyo measurement in March 2026 showed about 0.9s median order-to-fill despite official 0.2s docs. Reverse when live measurements say otherwise.

## 2026-08-30 (Phase 0 scaffold)

**argparse over typer or click for the CLI.** The whole surface is ten subcommands with a
handful of flags. argparse is in the standard library, so the trading process carries one
fewer dependency and one fewer thing to audit. Reverse if the CLI grows nested command
groups or needs shell completion badly enough to pay for a dependency.

**Hand-rolled config layering over pydantic-settings.** `liqfade config show` and the
panel settings page both have to tell the owner where a value came from (default file,
environment file, overrides.yaml, env var). pydantic-settings resolves its sources
internally and does not expose that provenance. So the loader deep-merges the layers
itself, recording the source of every leaf, and hands one dict to a plain pydantic model.
Reverse if pydantic-settings ever exposes per-field source information.

**Config YAML at the repo root, models in the package.** `config/default.yaml` sits beside
`data/` and `reports/`, matching how CLAUDE.md refers to `config/overrides.yaml` and
`config/mainnet.yaml` as root-relative paths, and keeping the file the panel writes out of
the installed package. The pydantic models live in `liqfade/config/`.

**Newline-delimited JSON framing on the control socket.** Messages are a command name and
a few arguments, so framing efficiency is irrelevant, and `nc -U /srv/liqfade/data/
control.sock` stays usable when the loop is misbehaving. Length-prefixed framing would buy
nothing here. Reverse if the control plane ever carries bulk data such as full order books.

**No authentication on the control socket.** Access control is the file: mode 0600, owned
by the service user. Anything that can open it already runs as the user that holds the
agent key, so a token on the wire would protect nothing. Reverse if a lower privileged
process ever needs read-only access.

**bcrypt for the panel password, not argon2.** Named in CLAUDE.md, one small wheel with no
build step on the VPS, and the real boundary is the network: the panel binds loopback and
is reached over Tailscale or an SSH tunnel. Against a single high-entropy password behind a
private network, argon2's better memory hardness does not change the outcome. Reverse if
the panel is ever exposed more widely, which it should not be.

**Panel sessions signed with itsdangerous, secret generated per process by default.** A
restart invalidating sessions is the safe default; `LIQFADE_PANEL_SESSION_SECRET` opts into
persistence. Reverse never for v0.

**/api/state reads data/state.json, never the control socket.** A slow HTTP client must not
be able to make the trading loop wait. A snapshot older than `panel.state_stale_s` returns
503 rather than a warning field, because every number on that page is a trading number and
quietly showing stale positions is worse than an obvious outage.

**Python pinned to 3.12 via .python-version.** pyarrow ships no 3.14 wheels yet and uv
otherwise tries to build it from source. Reverse once pyarrow publishes 3.14 wheels.

**Integration tests deselected, not skipped.** `-m "not integration"` in addopts, so a
clean `make test` reports zero skipped tests, which is the Phase 0 definition of done.

**deploy.sh restarts only services whose unit file changed.** Restarting the trading loop
because the panel's unit was edited is a surprise nobody wants at 3am. Code-only changes
need an explicit `systemctl restart`, which the script prints.

**Tailscale installed but not brought up by the bootstrap.** `tailscale up` needs an
interactive auth URL, and a bootstrap script should not decide to open a tunnel unattended.
The script prints the command.

## 2026-08-30 (cross-exchange liquidation history survey)

**No purchase of Binance/Bybit/OKX liquidation history for v0.** Surveyed Tardis
(https://docs.tardis.dev/faq/billing-and-subscriptions, https://tardis.dev/,
https://docs.tardis.dev/historical-data-details/binance-futures,
https://docs.tardis.dev/historical-data-details/bybit), CryptoStruct
(https://cryptostruct.com/pricing, https://cryptostruct.com/exchanges,
https://cryptostruct.com/download), Hyblock (https://docs.hyblockcapital.com/liquidation),
Velo (https://docs.velo.xyz/api, https://docs.velo.xyz/api/data), Coinglass
(https://www.coinglass.com/pricing), Amberdata
(https://blog.amberdata.io/accessing-crypto-liquidations-data-for-research-and-visualization),
Binance's public archive (https://data.binance.vision/, S3 listing under
data/futures/um/daily/ and data/futures/cm/daily/liquidationSnapshot/), and the
Binance developer forum (https://dev.binance.vision/t/is-there-historical-data-for-forceorder-endpoint/8362).
Finding: Binance and OKX have only ever published throttled streams, so no vendor holds
anything more complete than what the recorder captures; Bybit is complete only since
2025-02-25. Tardis has no one-off purchases and starts at $700/month; CryptoStruct sells
per instrument-day at EUR 1 but only carries liquidation events in 2026+ files. Summary
table in docs/DATA_SOURCES.md. Reverse if research question 4 shows cross-venue signals
matter; Hyblock 1-minute aggregates or a small CryptoStruct order are the affordable
paths then.

## 2026-09-02 (Phase 2, historical ingestion)

**liqfade/data/ was never in git.** The `.gitignore` rule for the data lake was written
`data/` with no leading slash, so it matched `liqfade/data/` too and `schemas.py` and
`symbols.py` existed only on the owner's disk. A clean clone had no `liqfade.data`
package, so CI could not have been passing. The lake, reports and logs rules are now
anchored to the repo root. Lesson: an unanchored directory rule in `.gitignore` matches
at every depth.

**`source` added to every canonical table, correcting a Phase 1 deviation.**
`docs/DATA_SOURCES.md` has listed `source` on every table since it was written;
`schemas.py` shipped without it in Phase 1 and used `venue` alone. The two are not the
same thing: `venue` is the exchange an event happened on, `source` is the job that put
the row in the lake (`recorder`, `hl_s3`, `hl_api`, `hl_archive`, `tardis`,
`hydromancer`). Encoding provenance in the filename instead was considered and rejected,
because nightly compaction folds a day into one file per table and would erase it
permanently the first time a recorded day and a backfilled day overlapped, which is
exactly the case that matters: `liquidations` and `trades` have four possible producers,
`l2` and `candles_1m` have two. `source` is stamped by `rows_to_table(name, rows, source)`
as a required argument rather than read from each row dict, so one file is always one
job's output and no parser can write a row with no provenance. `rx_ms = 0` keeps its
separate meaning: no local receive time exists, so no-lookahead checks cannot use it.

**boto3 and lz4 added to the core dependency group.** boto3 is the only maintained client
that speaks requester-pays S3, and `RequestPayer` is not optional on either Hyperliquid
bucket; lz4 reads the archive frames, which are LZ4 rather than gzip. Both pinned to a
major version. boto3 is imported lazily inside `s3_client()`, so the CLI, the recorder and
every offline test never import it and a machine with no AWS credentials only fails at the
point it actually tries to spend.

**The liquidation side rule, and how it is checked.** A Hyperliquid liquidation fill
carries `liquidation.liquidatedUser`. The forced party is the record whose `user_address`
is that address, and its own `side` says what it was made to do: `A` (sell) means it held
a long, so `side = "long"`. Reading the counterparty's record instead inverts the answer,
which is why the rule names the address explicitly. `SideRuleCheck` verifies it on the
first day pulled by measuring the 60s price move into each liquidation: long liquidations
are forced selling, so the mean return before them must be below the mean before short
liquidations. The run prints `consistent`, `INVERTED` or `inconclusive`. Correcting an
inversion is flipping `LIQUIDATED_ON_SELL` and `LIQUIDATED_ON_BUY` in
`liqfade/data/ingest/hl_s3.py` and running `liqfade backfill rederive`, never a second
download. Verified against the committed fixture; the owner records the result of the real
first day here.

**node_fills_by_block only covers 2025-07-27 onward.** The `hourly/` prefix begins there.
Older history lives under the `node_fills` and `node_trades` prefixes in a different
format that this parser does not read, so a request reaching further back prints what it
is skipping and why rather than quietly returning less data than asked for. Writing that
parser is the work item if a backtest ever needs more than about 13 months.

**Logging resolves stdout at write time.** `setup_logging` used to hand `sys.stdout` to
structlog's PrintLoggerFactory and to a StreamHandler, both of which keep the object. Any
later replacement of the stream left every subsequent log line writing to a closed file.
Found when the new `liqfade backfill` CLI tests configured logging under pytest's capture
and broke every test that logged afterwards. A small proxy now looks up `sys.stdout` on
each write. Under systemd the stream never moves, so this changes nothing in production.

## 2026-09-01

**Backfill parallelized with local staging after a measured bottleneck.** 13 hours of
the sequential pull moved 38 GB with Python at 4% CPU while fuse-t's NFS layer for the
owner's NTFS drive burned half a core on small writes. `parallel_pull` in
ingest/common.py now runs N download-and-parse workers against a staging directory on
the internal SSD and one serialized mover streams finished Parquet onto the lake disk,
which suits a USB drive (good at sequential copies, bad at concurrent small writes).
Default `--workers 6` on the s3-fills and l2 jobs. Staged files get a random name
suffix so concurrent workers can never collide in a partition. Reverse to workers=1 by
flag if a venue ever throttles concurrent S3 GETs.

Amended same day: threads did not help. The drive bottleneck gone, the run pegged one
core at 100% (orjson parsing holds the GIL) and stayed at ~3 GiB/hour. Workers are now
processes (spawn), each with its own boto3 client; the parent alone inspects, merges and
records the manifest. Handlers are module-level functions because they pickle into the
workers; the fills side-rule check moved to a parent-side inspector reading the staged
Parquet, since processes cannot share the accumulator.

## 2026-09-04

**A year of fills is 227 GiB, not 170.** The earlier figure was an estimate carried
forward as if measured; listing all 8,736 hourly objects gave 227.0 GiB, $20.43. Recorded
so nobody plans a re-pull on the smaller number. Total spend for the year, with L2 for
four coins and the retries below, about $25.

**The fills parser writes in byte-bounded batches.** `write_one_file` used to load a
whole hour into memory before writing. The 2025-10-10 cascade hour is 342 MiB compressed
(median hour: 23 MiB) and expanded to tens of GB of Python objects, killing every worker
that touched it on a 16 GB box with no swap, whatever the worker count. Batches are now
capped at `BATCH_BYTES` (16 MiB of JSON), which is bytes rather than lines because a
cascade block carries hundreds of events. A batch boundary is a line boundary and a line
is one block, so trade pairs stay together. Each batch appends its own Parquet file to the
partition; compaction folds them. Peak memory on that hour: 3.4 GB across 4 workers.

**L2 listing fans out.** One LIST per hour prefix is 8,760 calls a year; from a distant
box at 0.4 to 1.5 s each that was 40 minutes before the first byte. `ThreadPoolExecutor`
over the prefixes, order preserved. About 5 minutes now.

**Tardis 400 is coverage, not failure.** Tardis answers `400 Data type 'liquidations' is
not supported for 'BTC'` for Hyperliquid liquidations, which it never carried. Treated
like 404: recorded as missing, run continues.

**Hyperliquid keeps ~3 days of 1m candles.** `candleSnapshot` returns nothing for `1m`,
`5m` or `15m` older than about 72 hours; `1h` and coarser go back a year. The year of
`candles_1m` is therefore derived from `trades`, which is every fill and so exact. The
`hl_candles` job stays for the last few days and as the validation reference.

**One access key per machine.** Never use the
same access key from two machines at once.
AWS's exposure detection reads "one key, a datacenter IP, a brand-new account doing
high-volume paid egress" as a stolen key. Rule from now on: one IAM user and key per
machine, and a key never leaves the machine it was created for. Recovery is a support
case stating the above plus MFA.

**The NTFS drive goes through anylinuxfs, not fuse-t.** See docs/DATA_SOURCES.md, Disk.
12x on throughput, 9x on per-file latency; the difference between a usable and an
unusable lake for backtests. fuse-t and ntfs-3g removed from the laptop.

**Backfilled days need compaction before research.** The design's nightly compaction
only ever ran for recorded days. The year of history landed as 24 to 144 files per table
per day (150,000 files); at 30 ms an open that is 25 minutes of file opens per full scan.
`liqfade compact` over the range is the next step, then `liqfade derive candles`, then the
data quality report, then Phase 3.

**Compaction streams by hour instead of loading the day.** `compact_day` used to
`concat_tables` a whole partition to sort it. `fills_full` on the 2025-10-10 cascade day
is several GB compressed, tens of GB in Arrow, on an 8 GB laptop. It now groups a
partition's files by the UTC hour in their name, and folds the hours in ascending order
into one `pyarrow.parquet.ParquetWriter`, one row group per hour, so peak memory is one
hour of one table rather than one day. Both writers partition a row by the UTC hour of
its timestamp, so hour-ordered output is globally ordered by `ts_ms` and an hour range
scan can skip row groups on the footer statistics. Rows are counted on the temp file
before `os.replace` and again on the final file after it, and the inputs are deleted only
if both match; any exception leaves every input in place and removes the temp file.

**The hour ordering check needs a tolerance, because rx_ms files and ts_ms sorts.**
The recorder buckets a row by `rx_ms` and compaction sorts by `ts_ms`, so an event that
happened at 10:59:59.9 and arrived at 11:00:00.1 sits in hour 11's file and legitimately
precedes the end of hour 10. A strict assertion would have failed the nightly compaction
on any busy feed. Overlap up to `ORDER_TOLERANCE_MS` (60s, far above any feed's latency
and far below a mis-parsed hour) is logged and allowed; beyond it compaction still
refuses to write. Backfilled rows all carry `rx_ms = 0` and therefore bucket on `ts_ms`,
so for the whole historical lake the overlap is exactly zero.

**macOS writes an AppleDouble sidecar next to every file our Python writes to the
drive.** Sequoia stamps `com.apple.provenance` on files created by the interpreter, NFS
cannot store an xattr, so the client writes a 4 KiB `._name.parquet` beside `name.parquet`.
The backfill left 39,760 of them (l2 has none; the fills, liquidations and trades pulls
have them). They match a `*.parquet` glob, so they broke compaction and they break
`duckdb.read_parquet('date=*/*.parquet')` outright with "No magic bytes found at end of
file". A shell redirect to the same drive creates none, so this recurs on every write our
code makes and cannot be fixed by deleting them once. Rule: hidden files are never lake
data. `recorder/writer.lake_files()` is the one place that filter lives and every reader
should go through it or pass an explicit file list.

**The Tardis side tables are not compactable and do not need to be.**
`tardis_book_snapshot_5` and `tardis_derivative_ticker` keep Tardis's own columns by
design, so they have no canonical schema and no `ts_ms` to sort on, and the job already
writes them one file per venue per day (96 files for the 12 free days). `liqfade compact`
leaves them out of the default set and refuses them by name rather than failing halfway
through a range.

**`fills_full` is compacted even though nothing reads it today.** It carries 19 fields
the derived tables drop: `block_time, block_number, local_time, user_address,
start_position, dir, closed_pnl, hash, oid, crossed, fee, fee_token, cloid, builder,
builder_fee, twap_id, liq_user, liq_mark_px, liq_method`. Those are the candidate
features for the questions the derived tables cannot answer: how much vulnerable
positioning is left in a cascade (`start_position` and `user_address` across the book of
liquidated accounts), and whether the flow around a cascade is forced closing or fresh
opening (`dir`, `closed_pnl`, `liq_method`). Leaving it as 22,795 files would make that
research start with an hour of file opens. It is compacted last and detached, because it
is 130 GB and blocks nothing.

**Measured: the compacted lake is 146x faster to open.** A full-year metadata scan of
`l2` (every file's footer, no data read) went from 49,152 files in 686.3s to 343 files in
4.7s, same 3,425,187,420 rows. The numbers Phase 3 actually cares about are the reads:
the whole year of `liquidations` (5,770,019 rows, $51.6bn notional) is 4.2s, and one
coin-month of `l2` (BTC, 2026-08, 9,639,220 rows over 20 levels) is 6.6s. Per-file open
latency on this drive is about 14 ms, so file count, not bytes, was the cost.

File counts, before and after: `liquidations` 15,715 -> 364, `trades` 27,421 -> 365,
`l2` 49,152 -> 343. Every one of those 1,072 day files was independently re-opened
afterwards, checked for a `PAR1` trailer and a readable footer: none truncated, no
partition left unfolded. That sweep matters because this mount has timed out mid-write
before and left a Parquet with no footer. `fills_full` (22,795 files) was started
detached after the rest finished and its result is appended here when it lands.

**It landed (appended 2026-09-05).** `fills_full` 22,795 files -> 365, 2,853,564,289
rows, 13:26:38 to 19:40:41 UTC on 2026-09-04, 6.2 hours. The worst partition is the
2025-10-10 cascade day, 348 files and 19,127,562 rows folded in 348s; the median day is
around 40 files and 30s. The mount timed out three times (`[Errno 60]`, twice opening an
input and once stat-ing the temp file mid-write, on 2025-10-12, 2025-12-29 and
2026-01-19); the driving loop retried each and all three days completed, which is the
behaviour `_compact_partition` is built for: an exception leaves every input in place and
removes the temp file, so a retry is a clean redo. Two numbers in the run's own summary
line are wrong for the same reason the rederive's were, and are not the ones above: it
printed 10,089 files and 1,920,241,471 rows, counting only from the last restart. The
per-day log lines cover all 365 days exactly once and are what the totals here come from.
2,853,564,289 fills against 1,428,662,127 derived `trades` rows is the maker/taker pairing
the derivation collapses, so the two agree.

**Compaction is row-exact; the manifest is not the row authority.** Every partition's
output footer is compared against the sum of its inputs' footers before a single input is
deleted, so the compacted totals are the pre-compaction totals by construction. `l2`
matches its pre-compaction footer sum exactly, 3,425,187,420 either side.

The derived tables hold more rows than the manifests record, and that is the manifest
being wrong rather than the lake:

| table | source | on disk | manifest |
|---|---|---|---|
| `liquidations` | `hl_s3` | 5,633,882 | 4,101,187 |
| `liquidations` | `tardis` | 136,137 | 136,137 |
| `trades` | `hl_s3` | 1,428,662,127 | 1,145,240,744 |
| `trades` | `tardis` | 376,007,141 | 375,833,273 |

The `hl_s3` keys for 2025-09-01 to 2025-11-13 were pulled on a rented box that was wiped
before its manifest came back, and were rebuilt locally with `trades: 0` and partial
counts, so for that window the lake is the source of truth and the manifest is only a
resume ledger. The ~375k `liquidations` rows that share
`(venue, coin, ts_ms, px, sz, side, method)` are not duplicates either: the table carries
no `tid`, and the liquidation engine fills many positions at one mark price in one block.
Tested where it can be tested, on `fills_full`, which does carry `tid`: zero exact
duplicates on every day checked except 10 rows in the 2025-11-13 handoff hour. So nothing
needs deduping, and 02c is not blocked. The one residual is Tardis `trades`, 173,868 rows
(0.05%) above a manifest that was never rebuilt; small enough to leave to 02d. See
`prompts/02e_quality_gate.md`, which settles all of this.

**The Tardis side tables kept only the last coin written.** `write_side_table` named its
output `{venue}_00.parquet` per table and day, but `run` calls it once per coin for the
same venue and day, so each coin overwrote the one before: 57.2M of 281.8M rows survived
in `tardis_book_snapshot_5` and 7.7M of 36.6M in `tardis_derivative_ticker`. The name now
carries the coin. `trades` and `liquidations` were never affected; they go through
`write_partitioned`, which picks a free file name. The two side tables are deleted and
re-pulled (384 files, ~6.1 GB, free).

**`fills_full` carries the exchange's event order, and compaction did not disturb it.**
The 02d repair rests entirely on that, so it was checked rather than assumed. Every fill
carries `startPosition`, the user's position before the fill, so consecutive fills of one
`(user_address, coin)` must chain: `start_position[n] == start_position[n-1] +
signed_sz[n-1]`. Reorder two fills of one sweep and both links break.
`scripts/research/fills_order_check.py` walks that chain in `ts_ms` order with file order
as the tie-break, the same stable sort the rederive uses, on the compacted cascade day and
two days still in hourly files:

| day | files | perp links | perp broken | spot links | spot broken |
|---|---|---|---|---|---|
| 2025-10-10 (compacted) | 1 | 17,368,159 | **0** | 1,458,723 | 679,774 |
| 2026-03-01 (hourly) | 41 | 6,319,572 | **0** | 286,573 | 157,503 |
| 2026-01-10 (hourly, quiet) | 33 | 2,379,722 | **0** | 180,466 | 86,759 |

Zero broken perp links in 26.1M, including on the day the compaction had already folded
from 200-odd hourly files into one. Only perps are evidence, and that took a wrong turn
first: the check counted 86,759 violations on 2026-01-10 before it separated the markets.
On a perp, `startPosition` is the position and nothing but a fill moves it. On a spot
market (`coin` is `@107` or `PURR/USDC`) it is the base token balance, which also moves
on deposits, transfers between the spot and perp wallets, and the fee when the fee is
taken in that token, none of which appear in this file. Measured: 78,467 of the 86,759
spot breaks are exactly the previous fill's `fee`, and 83,831 had a non-USDC `fee_token`.
So spot breaking says nothing about row order, and the script reports the two separately.

**The rederive is vectorised through polars twins, and the dict functions stay as the
reference.** `derive_trades` and `derive_liquidations` walk Python dicts one row at a
time, which is right for the S3 parser (one 16 MiB batch of freshly parsed JSON) and
wrong for a year: ~3bn fills through that path is six to ten hours. So
`liqfade/data/derive/from_fills.py` holds `derive_trades_frame` and
`derive_liquidations_frame`, which do the same work in polars and are never allowed to
disagree. The contract is the test, not the reading: they are asserted equal row for row
and in order on the fixture, on every maker/taker and victim/counterparty shape, and on
all 6,744,520 fills of 2026-03-01 (41 hourly files, compared file by file, then the whole
day checked against the concatenation of the files, which also proves no `(block_time,
coin, tid)` group ever spans two files). The dict functions were not touched; if the two
ever disagree the dict one is right.

**The resume rule reads a stamp in the Parquet footer, not the file name and not the
mtime.** The obvious rule, "skip a day whose `trades` and `liquidations` are already a
single `all_day.parquet` newer than its `fills_full` files", is wrong here in both halves,
and the first run of the year proved it by skipping every day. 02b's compaction already
wrote exactly that file name for all 365 days, out of the wrongly ordered rows this job
exists to replace, and wrote them after the fills they came from. So the name says
nothing. The mtime is worse than useless: the `fills_full` compaction runs alongside this
and rewrites a day's fills without changing one row of them, so any day it reached after
the rederive had already done it would be redone for nothing. `_assemble` therefore
stamps `liqfade_rederive=1` into the footer metadata and `_partition_is_current` reads
it. `--force` is for the one case the stamp cannot see, a genuine re-pull of `fills_full`.

**Tardis's Hyperliquid trades are dropped: `hl_s3` owns the venue.** Both sources carry
the same Hyperliquid fills under the same `tid`, so on the twelve free days a
`venue == "hyperliquid"` filter over `trades` counted every trade twice. `tardis_free` no
longer requests Hyperliquid `trades` or `liquidations` (its Hyperliquid side tables have
no other source and are still pulled), and the rederive deletes any it finds in a
partition it rewrites: 1,284,579 rows on 2026-03-01, against 3,371,156 of ours. The other
three venues in that partition are untouched, 46.8M rows. Same rule as counting a
liquidation once; see docs/DATA_SOURCES.md, "Counting a trade once".

**2026-03-01, before and after, against the Tardis reference.** Bars built from Tardis's
Hyperliquid trades, whose order is right, on the four coins it covers. "Before" is bars
from the lake's `hl_s3` rows alone, which is what the other 353 days of the year looked
like; the lake's own `candles_1m` for this day scored better than that only because the
dedup sometimes picked Tardis's copy of a trade.

| coin | minutes | `o` before | `o` after | `c` before | `c` after |
|---|---|---|---|---|---|
| BTC | 1,440 | 89.72% | 99.86% | 88.40% | 99.79% |
| ETH | 1,440 | 91.04% | 99.65% | 92.36% | 99.72% |
| HYPE | 1,440 | 80.90% | 99.65% | 85.76% | 99.72% |
| SOL | 1,440 | 88.12% | 99.65% | 89.24% | 99.79% |

The residual is not ordering, and that is the number that matters. Splitting the
disagreements by whether our trade count for the minute equals Tardis's: before, 1,238 of
1,270 disagreeing minutes had an **identical** `n`, which is a right trade set in a wrong
order. After, **zero** of the remaining 28 do; every one is a minute where Tardis itself
saw a different number of trades, its known 0.05% incompleteness. So on every minute where
the two sources agree on the trades, `o` and `c` now agree exactly. `h` and `l` were
already 99.7%+ either way, and `n` is unchanged at 99.0 to 99.2%.

**The year rederived, measured.** `liqfade backfill rederive --start 2025-08-31 --end
2026-08-30`, two workers, newest first, running the whole time alongside the `fills_full`
compaction going the other way. 365 days in 2.0 hours of wall clock, median 33s a day,
slowest 178s (2026-02-01, a Tardis day, where the `trades` partition also carries 46.8M
Binance, Bybit and OKX rows that have to be read and written back around ours). Peak RSS
per worker 2.6 GB on the 2025-10-10 cascade day, 19,127,562 fills, which is why the
default is two workers and not four on this 8 GB machine.

Counted from the lake afterwards rather than from the run's own summary, all 365 days:

| table | source | after | before (2026-09-04, pre-repair) |
|---|---|---|---|
| `trades` | `hl_s3` | 1,428,662,127 | 1,428,662,127 |
| `liquidations` | `hl_s3` | 5,633,882 | 5,633,882 |
| `trades` | `tardis` (Binance, OKX, Bybit) | 362,088,179 | 376,007,141 incl. Hyperliquid |
| `liquidations` | `tardis` | 136,137 | 136,137 |

Row for row identical on our two tables: the same trades and the same liquidations out of
the same fills, in a different order. The 13,918,962 rows that went are Tardis's copy of
Hyperliquid `trades`; its `liquidations` were untouched because Tardis never carried
Hyperliquid liquidations at all. Every one of the 730 partitions is a single
`all_day.parquet` carrying the rederive stamp.

**Five restarts in two hours, all of them the mount.** Two `OSError 60` (the open timed
out) and three polars panics wrapping `OSError 22`, on both the `fills_full` read and the
existing-partition read. Three things came out of that and are now in the code. A polars
`PanicException` derives from `BaseException`, not `Exception`, so it has to be named or
no handler ever sees it. It also cannot be pickled back out of a pool worker, so the
parent reported a `PicklingError` for `pyo3_runtime` and the real cause never reached the
log; the worker now re-raises as a `BackfillError` carrying the message. And what decides
whether a failed read is retried is the listing, not the exception: if the day's files are
all still there it is a real failure and is raised, and if they are not the compaction
folded the day underneath us and it is redone from the new listing.

The restarts also exposed a reporting bug worth writing down, because the lake looked
short when it was not. `pool.shutdown(cancel_futures=True)` cannot recall a day already
handed to the pool's call queue, so after a failure a few more days run to completion
while the consuming loop has stopped. Their work is written and stamped, but their row
counts were never reported: the first year run was 26 days short in its own accounting
over a complete lake. `unconsumed()` drains them now.

**The year of candles rebuilt.** `liqfade derive candles --start 2025-09-01 --end
2026-08-30 --force`, 364 days, 174,697,608 bars, 33.4 minutes. The bar count is identical
to 02c's, to the bar, and that is the expected result rather than a suspicious one:
reordering the trades inside a millisecond changes which trade is a minute's first and
last, never how many minutes a coin traded or how many trades fell in each. `o` and `c`
are what moved. Bars a day still grow with the exchange, 327,788 across 277 coins on
2025-09-01 to 649,986 across 578 on 2026-08-30.

**Hyperliquid's `tid` is a hash, not a sequence, and that was silently reordering
`trades`.** Two fills of one sweep carry 997296749534590 and 653061949558227 in that
order. `hl_s3.derive_trades` sorted its output by `(ts_ms, tid)`, so inside a millisecond
the exchange's event order was replaced by an arbitrary one, and any consumer asking
"what was the first trade of this minute" got the wrong answer. Found while validating
derived candles against `candleSnapshot`: bars built in `tid` order matched the API's
open on 76% of BTC minutes and its close on 91%, while the high matched 100% and `n` and
`v` were identical on the very rows that disagreed, which is a right trade set in a wrong
order. Bars built in file order match on 100%.

The sort was fixed the same day, so everything written since is right. The year that was
already in the lake was not: measured on 2026-03-01 against the same day's Tardis rows,
`o` and `c` disagreed on 8 to 19% of minutes by a median of 0.15 to 1 bps, p95 3 to 4
bps, max 11 bps. Small, but not negligible next to 4.5 bps of taker fee, and worst
exactly at the entries and exits a scalp backtest models. **That data has since been
repaired.** `fills_full` was never sorted and still carried the true order, so the fix
cost nothing but time: `liqfade backfill rederive` rebuilt every `hl_s3` row of `trades`
and `liquidations` from it in file order, and `liqfade derive candles --force` rebuilt
the bars. See the 02d entries above for the order proof, the before-and-after match
rates and the run itself. Lesson, and the reason this is written down rather than just
fixed: an exchange id is an identity, not an ordering, unless the exchange says
otherwise.

**`trades` holds every Hyperliquid trade twice on the twelve Tardis days.** `hl_s3` and
`tardis` both cover Hyperliquid, under the same `tid`, so a naive `venue == hyperliquid`
filter doubles `v` and `n` for BTC, ETH, SOL and HYPE on those days. The candle
derivation deduplicates on `(coin, tid)`, which is unique per coin-day and never null on
a Hyperliquid row. Tardis is also the slightly smaller set: 541,237 BTC trades against
`hl_s3`'s 541,557 on 2026-03-01, and every Tardis `tid` is present in `hl_s3`.

**Empty minutes are bars, not gaps.** `candleSnapshot` returns a real bar for a minute
with no trades: `v = 0`, `n = 0`, `o = h = l = c` at the previous close. Measured on
TNSR, 291 such bars in 360 minutes with no break in the `t` sequence. The derivation
replicates it, so a window of N bars means N minutes in the backtest exactly as it does
against the live `candle` stream; sparse bars would have made the same feature mean two
different things. The fill runs between a coin's first and last trade of the day rather
than across the whole UTC day, so each day file stays independently rebuildable with
`--force`; for any coin liquid enough to trade, the two rules agree. Cost of the choice:
about 185M bars for the year instead of the 92M a sparse table would hold, and they
compress well because the filled ones repeat one price.

**The candle validation used a free Tardis day, because the API window and the lake do
not overlap.** `candleSnapshot` keeps about 3 days and the backfilled `trades` end
2026-08-30, so there is no day with both, and `.env` currently holds no AWS keys to pull
more. 2026-09-01 is a Tardis free day, so its Hyperliquid trades are downloadable at no
cost and the API still had it. That is what the 100% `o`/`h`/`c` match is measured on.
Residual `l` at 99.6% and `n` at 99.4% is Tardis's own incompleteness (0.05% of trades),
not the derivation.

**The year of candles, measured.** `liqfade derive candles --start 2025-09-01 --end
2026-08-30`: 364 partitions, one `hyperliquid_day.parquet` each, 174,697,608 bars, 2.4 GB
zstd, 34.9 minutes of wall clock (median 5.3s a day, 21.2s on the Tardis days, which carry
four venues in the same `trades` partition). Every file re-opened afterwards and checked
against `CANDLES_1M`: none truncated, none with a stray second file. Bars a day grow with
the exchange: 327,788 across 277 coins on 2025-09-01, 508,319 across 393 on 2026-03-01,
649,986 across 578 on 2026-08-30. That is roughly double the 90M the plan guessed,
because empty minutes are bars here rather than gaps; they cost little on disk since a
filled bar repeats one price. Dedup verified on a Tardis day: 2025-10-01 BTC has 679,311
trade rows, 339,656 after `(coin, tid)`, and the day's bars sum to exactly 339,656 trades
and 30,800.3529 coins.

**The derivation validated a second way, against the live `candle` stream.** The Tardis
check cannot separate a derivation bug from a source gap, so a 45 minute recorder capture
was compared against Hyperliquid's own bars for the same minutes: 899 minutes over 20
coins. On the 854 minutes where our trade count equals the exchange's, meaning we caught
the whole minute, `o/h/l/c` is exact on 851 (99.65%) and `v` matches to within a float
ULP on all 854. On the 45 minutes where the counts differ, which is the laptop's
WebSocket dropping frames (the run logged `stream_gap` throughout), `o/h/l/c` agrees on
38%. The three residual complete minutes are all fast ones and differ by a single tick in
one of `o`, `l` or `c`, with identical `n` and identical `v`; the BTC case is a trade at
79067 that our feed carries 25 seconds into the minute and the exchange's bar does not
count, so it is not a boundary or ordering effect. Left unexplained at 0.35% rather than
guessed at.

**The Tardis side-table re-pull finished:** `tardis_book_snapshot_5` 281,773,008 rows and
`tardis_derivative_ticker` 36,619,381, both matching the pre-bug expectation, so no coin
is missing now. The old one-coin-per-day files were deleted the same day; verified
2026-09-05, zero `{venue}_00.parquet` remain against 384 `{venue}_{coin}_00.parquet`.

## 2026-09-05 (lake hardening)

**The Tardis Hyperliquid sweep found nothing, which is the answer.**
`scripts/research/drop_tardis_hl.py` walks every `trades` and `liquidations` day for
`source = tardis AND venue = hyperliquid`, reports before and after per day, and refuses
to touch anything without `--yes`. Over 2025-09-01 to 2026-09-04, all 365 partitions of
each table: **0 rows found, nothing dropped.** The year rederive reached every day it
covers, so it had already dropped all 13,918,962 of them; spot-checked on 2026-03-01,
which now holds 21,291,201 Binance, 17,131,835 OKX and 8,337,259 Bybit `tardis` rows
around 3,371,156 `hl_s3` Hyperliquid rows and no `tardis` Hyperliquid row at all.

The case the prompt named, 2026-09-01, is not in the lake: the backfill ends 2026-08-30,
and the candle validation pulled that day's Tardis trades ad hoc rather than through the
lake. The script stays for the day a partition with no `fills_full` does land, which the
rederive returns `"empty"` for and therefore never cleans.

**The year of asset contexts, measured.** `liqfade backfill asset-ctxs --start 2025-09-01
--end 2026-08-30`: **363 day files, 116,884,578 rows, 3.76 GiB, $0.34** of egress. A full
day is 1440 minutes per coin, 334,080 rows at 232 coins. Every file was re-opened
afterwards and checked for a readable footer against the canonical schema: none truncated,
none with a stray second file.

364 days were requested and one is not there, and it is not worth filling from another
source: **2026-07-08 has no object in the bucket.** July 2026 lists 30 files, and
2026-07-07 and 2026-07-09 are both present at 10.5 and 10.3 MB. It is Hyperliquid's hole,
and a row invented for it would carry a `source` that is not true. `asset_ctx` is
therefore 363 days, and `scripts/research/data_quality.py` reports exactly two zero-row
days for it: that one, and 2025-08-31, which is outside the requested range and appears
only because `fills_full` and `trades` start a day earlier.

Several days are short rather than absent, because the archive's own file is truncated:
2026-08-27 ends 14:39 UTC at 155,440 rows and 2026-08-30 ends 12:03 at 119,248, and in
the 2026-08-24 to 2026-08-30 week alone four of seven days are clipped at one end. That is
the source, not the pull, and 02e is told to pass on them.

**Validated two ways, and both found bugs in the comparison before they found anything in
the data.** Funding against the Info API's `fundingHistory` over 2026-08-24 to 2026-08-30,
and open interest and mark against the twelve `tardis_derivative_ticker` days:

| coin | funding hours | within 2e-5 at HH:59 | worst funding diff | OI median rel | OI p95 rel | mark median rel |
|---|---:|---:|---:|---:|---:|---:|
| BTC | 139 of 168 | 100.00% | 5.78e-07 | 0.000% | 0.002% | 0.0000% |
| ETH | 139 of 168 | 100.00% | 3.2e-07 | 0.000% | 0.001% | 0.0000% |
| HYPE | 139 of 168 | 100.00% | 1.18e-06 | 0.000% | 0.001% | 0.0000% |
| SOL | n/a | n/a | n/a | 0.000% | 0.001% | 0.0000% |

The 29 unmatched funding hours are the truncated archive days above. The mark comparison
is an ASOF join with a median tick age of 0.8 to 1.4s, and matches exactly on 68 to 76% of
samples: the archive samples on the minute boundary and Tardis's nearest tick is about a
second earlier, so the residual is a second of price movement, which is why the median
relative difference is 0.0000% rather than the exact-match rate being 100%.

**Open interest from `fills_full` proves the archive exactly, and the exchange counts both
sides.** `scripts/research/oi_from_fills.py` sums `abs(start_position + signed_sz) -
abs(start_position)` over every fill in a minute, which is the exact change in open
interest across all accounts. Against the archive's own differenced series:

| day | coin | minutes | correlation | sides | worst abs diff | biggest move |
|---|---|---:|---:|---:|---:|---:|
| 2025-10-10 (cascade) | BTC | 1,439 | 1.000000 | 2.000000 | 2.0e-10 | 7,454.44 |
| 2025-10-10 | ETH | 1,439 | 1.000000 | 2.000000 | 7.7e-09 | 168,835.25 |
| 2025-10-10 | HYPE | 1,439 | 1.000000 | 2.000000 | 2.3e-07 | 7,757,751.74 |
| 2025-10-10 | SOL | 1,439 | 1.000000 | 2.000000 | 1.8e-08 | 1,991,461.36 |
| 2026-01-10 (quiet) | BTC | 1,439 | 1.000000 | 2.000000 | 1.0e-10 | 193.60 |
| 2026-01-10 | ETH | 1,439 | 1.000000 | 2.000000 | 7.2e-10 | 6,521.08 |
| 2026-01-10 | HYPE | 1,439 | 1.000000 | 2.000000 | 5.2e-08 | 178,329.16 |
| 2026-01-10 | SOL | 1,436 | 1.000000 | 2.000000 | 1.0e-08 | 79,213.98 |

Correlation 1 and a residual at float noise on both a cascade day and a quiet one. `sides`
is the archive's change over the long-only sum, and it is a gate rather than a note: 2.0 is
measured on every run and a run that finds anything else exits non-zero, because a
one-sided redefinition would still correlate 1.0 and simply halve every number.

**Three comparisons, three ways to be confidently wrong.** Worth writing down because none
of them would have failed a smoke test, and each produced a number a research note would
have accepted. The archive's `OI(M+1) - OI(M)` is the change *during* minute M, so
labelling it M+1 paired every minute with its neighbour and gave correlation 0.08 to 0.32.
Aligned, it still moved exactly twice as far, because `openInterest` is both sides.
And Tardis's mark appeared to disagree by 0.56% on HYPE, which turned out to be
`read_parquet` over files with per-coin inferred types silently truncating 31.215 to 31.
The rule that caught all three: check the residual against what it must be if the two
sides describe the same quantity, rather than accepting "broadly agrees".

**The drive dropped mid-pull, and the NTFS damage is not all repaired.** The USB volume
re-enumerated from `/dev/disk5` to `/dev/disk6` while the pull was writing, leaving a stale
NFS handle, a read-only remount, and after `ntfsfix` two damaged directory entries:
`parquet/asset_ctx/date=2026-02-15` and `ingest_state/s3-asset-ctxs.json`. On both, `stat`
raises EINVAL or ESTALE, and **`rm` and `os.replace` report success and do nothing**, while
an O_TRUNC write lands and reads back. Two consequences worth knowing before the next run:
the manifest silently recorded nothing for all 167 days of the resumed pull, so it was
rebuilt from the lake's own footers plus one S3 listing; and a `date=*` glob dies on the
partition, which is why `scripts/research/data_quality.py` now reports an unopenable
partition as a hole instead of crashing.

chkdsk on Windows cleared it, and **the repair itself cost four more day files**:
2026-02-16 to 2026-02-19 were cross-linked with the manifest, which is how Parquet bytes
got into its head, and resolving a cross-link means deleting one side. Those four
partitions came back holding only the crashed run's `.hyperliquid_day.parquet.60209.tmp`
and no real file. Caught by re-sweeping every footer after the repair rather than by
trusting the earlier sweep, which had been true when it ran; re-pulled for nothing, since
the archive charges by the byte and four days is 44 MB.

Two lessons, both now habits in the scripts here. A write to this mount is not believed
until it reads back: the manifest rebuild verifies by re-reading, and the one that did not
failed silently and cost 35 retry attempts. And a verification sweep is only true for the
moment it ran, so it is re-run after anything touches the volume, including a repair meant
to help.

**The panel's liquidation chart is uPlot, not `lightweight-charts`.** CLAUDE.md section 4
names `lightweight-charts` for price, liquidation and trade-marker charts and uPlot for
equity and latency series. The panel's 24h liquidation chart is stacked hourly bars per
venue, which is a time series and not a price chart: it has no candles, no crosshair on a
price scale and no trade markers. `lightweight-charts` has no stacked bar series, so
drawing it there would mean a histogram series per venue with the stacking done by hand
anyway, plus a second charting library in the bundle for one chart. uPlot already has to
be there for the equity and latency series. Reverse when the panel gets its per-coin price
chart with liquidation and trade markers, which is what section 4's rule is actually
about: that one is `lightweight-charts`, and this chart can stay where it is.

**The candle validation is a script now, and it reproduces.**
`scripts/research/candle_validate.py` builds our bars either from the lake's
`candles_1m` or, for a day the lake does not cover, from Tardis's free first-of-month
Hyperliquid trades, and compares them against `candleSnapshot` or against the recorder's
own `candle` stream. It writes `reports/data_quality/candle_validation_{day}_{ref}.md`.
Re-run on 2026-09-05, four days after the day it validates, so `candleSnapshot` still
held 1,059 of the day's 1,440 BTC minutes rather than all of them:

| coin | minutes | o | h | l | c | v | n | o/h/l/c where n agrees |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| BTC | 1,059 | 100.00% | 100.00% | 99.53% | 100.00% | 99.34% | 99.34% | 99.71% of 1,052 |
| ETH | 1,056 | 100.00% | 100.00% | 99.91% | 100.00% | 99.62% | 99.62% | 99.90% of 1,052 |
| HYPE | 918 | 100.00% | 100.00% | 99.78% | 99.89% | 99.24% | 99.24% | 99.78% of 911 |
| SOL | 1,047 | 100.00% | 99.90% | 99.33% | 100.00% | 99.62% | 99.62% | 99.42% of 1,043 |

Same answer as the prose entry above: `o` and `c` exact, and the residual sits in `l`,
`v` and `n`, which move together because they are the three a missed trade changes.
The last column is the one that says something about the derivation: it is measured only
on minutes where both sides counted the same trades, and it is 99.4 to 99.9%.

The reason the reference is Tardis and not the lake is unchanged and now enforced by the
script's own error message: `candleSnapshot` keeps about three days of `1m` bars and the
lake's `trades` end 2026-08-30, so there is no day both hold. The moment the recorder
starts there will be, and `--ours lake` is that run.

## 2026-09-05 (the Phase 2 data quality gate)

**The gate report covers the whole lake now.** `scripts/research/data_quality.py` grew
three sections and a rule. Per table per day it now reports files as well as rows, and
flags any partition not holding exactly one file after compaction, with the two
uncompacted Tardis side tables exempted at 16 (four venues by four coins). A "days
present / days expected" line per table puts the coverage next to the row count, because
on `asset_ctx` the row count alone is misleading in both directions. `liquidations` and
`trades` are split by `venue` and `source`, since both carry four venues from two pulls
and a day full of Binance rows with every Hyperliquid row gone would otherwise read as
healthy. And the side rule now runs over the whole year, month by month, as an ASOF join
rather than the per-file Python loop in `SideRuleCheck`; the script exits non-zero if any
month inverts. `--footers-only` skips the two sections that read data.

**The final table, 2025-08-31 to 2026-08-30, 365 calendar days in the span.**

| table | days | rows | files | source of the shortfall |
|---|---:|---:|---:|---|
| `fills_full` | 365 | 2,853,564,289 | 365 | complete |
| `trades` | 365 | 1,790,750,306 | 365 | complete |
| `l2` | 343 | 3,425,187,420 | 343 | 21 days the archive has no files for |
| `candles_1m` | 364 | 174,697,608 | 364 | equals `trades` days, less 2025-08-31 |
| `liquidations` | 364 | 5,770,019 | 365 | 2025-08-31 has trades but no liquidation |
| `asset_ctx` | 363 | 116,884,578 | 363 | 2026-07-08 is not in the bucket |
| `tardis_book_snapshot_5` | 12 | 281,773,008 | 192 | free first-of-month days only |
| `tardis_derivative_ticker` | 12 | 36,619,381 | 192 | free first-of-month days only |

Every one of those numbers matches what 02b, 02c, 02d and 02f recorded, independently
recounted from the footers. `trades` splits 1,428,662,127 `hl_s3` and 362,088,179
`tardis`; `liquidations` 5,633,882 and 136,137. No partition anywhere in the lake holds
the wrong number of files.

**The 365th day is three rows.** `fills_full` and `trades` cover 365 days rather than 364
because the first hourly object of 2025-09-01 carries six fills stamped 2025-08-31, which
derive to three trades. `liquidations` and `candles_1m` are 364 because none of those six
is a liquidation and three trades of one coin are not a bar worth writing.

**`l2` is 343 days, and the 21 missing ones come in three blocks.** 2025-10-17 to 10-22
(6), 2025-11-18 to 11-25 (8), and 2026-07-08 plus 2026-07-12 to 07-17 (7). Contiguous
blocks, not a scatter, which is what a source outage looks like and not what a failed
pull looks like. 2026-07-08 is missing from `asset_ctx` too, from a different bucket and a
different job, which is as close to proof as this gets that the date itself is the hole.
Not re-verified against S3: `.env` holds no AWS keys any more and the answer would not
change what we do.

**The `l2` snapshot cadence drops tenfold in June 2026, and this is the one finding that
changes Phase 3 and 4.** Not a hole: every day present holds all four coins across all 24
hours. What changed is how often the archive publishes a snapshot. Through 2026-06-14 it
is about 155,000 snapshots per coin per day, one every 0.56s. Then it steps down over a
week, and from 2026-06-21 to the end of the range it is about 16,085 per coin per day,
one every 5.4s:

| span | rows/day | snapshots per coin per day | gap between snapshots |
|---|---:|---:|---:|
| 2025-09-01 to 2026-06-14 | ~12.5M | ~156,000 | 0.55s |
| 2026-06-15 | 6,373,500 | 79,669 | 1.1s |
| 2026-06-16 to 06-19 | ~3.12M | ~38,985 | 2.2s |
| 2026-06-20 | 1,975,740 | 24,697 | 3.5s |
| 2026-06-21 to 2026-08-30 | ~1.25M | ~15,500 | 5.6s |

The consequence is a trap for the Phase 4 DoD, which asks for a held-out last 25%. The
last 25% of this range starts in June 2026, which is exactly where the book resolution
falls off a cliff. A fill model tuned on a 0.55s book and validated on a 5.6s one will
report worse fills out of sample and the cause will look like the strategy. Whatever the
split is, it has to be stated in book-cadence terms as well as in dates, or the L2-derived
features have to be built at a cadence both halves can serve. Recorded here rather than
solved, because the choice belongs to the research.

**`asset_ctx` is 363 days, and the one absent day is 2026-07-08, confirmed.** July 2026
lists 30 files, and 2026-07-07 and 2026-07-09 both hold a full 332,640 rows (231 coins by
1440 minutes). Not filled from another source: a row there would carry a `source` that is
not true.

Five days are short rather than absent, three more than 02f named, and all five are the
archive's own truncation rather than a stopped pull. The test is the object size against
the rows it produced: the median across the year is 34.5 bytes per row, and all five sit
at 30 to 32, so the `.lz4` is proportionally small and there is nothing in it we failed to
read. Each starts at 00:00 and stops early, with every coin present for every minute up to
the cut:

| day | rows | ends (UTC) | coins | bytes/row |
|---|---:|---|---:|---:|
| 2026-05-30 | 96,140 | 06:57 | 230 | 32.4 |
| 2026-08-08 | 160,544 | 11:31 | 232 | 31.2 |
| 2026-08-09 | 240,120 | 17:14 | 232 | 30.2 |
| 2026-08-27 | 155,440 | 11:09 | 232 | 32.2 |
| 2026-08-30 | 119,248 | 08:33 | 232 | 30.6 |

The 02f entry gives 14:39 and 12:03 for the last two. Same instants: those were read in
local time. Everything in this repo is UTC, which is why the table
says so explicitly.

**The "under 90% of the median" rule is the wrong rule for `asset_ctx`, and the report no
longer leans on it.** It flags 21 days, and 16 of them are complete: the whole of early
September 2025 sits at exactly 296,640 rows, which is 206 coins by 1440 minutes, and the
year's median of 329,760 is 229 coins. The table grew with the exchange, from 206 coins to
232. The rule that actually separates a short day from a small one is whether the row
count divides by 1440: a complete day does, a truncated day does not. That is why the
"days present / days expected" line matters here more than the row count, and it is what
the report now leads with.

**The manifest and the lake reconcile, and every disagreement has a name.** Over the 290
days whose manifest was not rebuilt on the wiped rented box:

- `liquidations` disagrees on exactly nine days, every one a Tardis free first-of-month,
  by exactly +105,705 rows in total. That is precisely the Tardis liquidation rows the
  `s3-fills` manifest never claimed to count: Tardis's own manifest records 136,137 for
  the year, of which 30,432 fall on the three month-firsts inside the rebuilt window. On
  all 281 other days the lake equals the manifest to the row.
- `fills_full` disagrees on most days by two to a few hundred rows, always an even number,
  and the disagreements cancel between adjacent days. Net over the 290 days: **-4 rows**
  in 2.4 billion. The manifest counts rows per hourly object and the lake buckets a row by
  the UTC day of its `ts_ms`; a block straddling midnight puts a few fills on the other
  side, and fills come in maker/taker pairs, which is the even numbers.
- All 8,736 hourly objects are present, 24 for each of the 364 days. There is no missing
  hour in the year.

The 22 days whose `fills_full` count is under 40% of the median are 18 Saturdays and
Sundays plus Christmas Eve, Christmas, New Year's Eve and New Year's Day, each with all
24 hourly objects. Volume seasonality, not a hole.

**One real defect, and it is 173,868 rows of Bybit ETH.** 02b left a residual it could not
place: Tardis `trades` in the lake were 173,868 rows (0.05%) above the manifest. It is one
file. Binance and OKX match their manifests exactly. Bybit is over by 173,868, all of it
on 2026-01-01, all of it ETH: the lake holds 657,605 rows where the manifest records
483,737, and 657,605 - 173,868 = 483,737 exactly.

Located by the hour. Grouping the day's Bybit ETH rows on `(ts_ms, px, sz, side)`, hours
00:00 to 11:59 UTC have 140,841 groups and **not one of them has an odd count**, while
hours 12 to 23, and BTC on both halves of the same day, are odd on 85 to 90% of groups as
real trades are. Hours 00 to 11 hold 347,736 rows, exactly twice 173,868. So the first
half of that file was written twice: a first attempt died after hour 11 without recording
a manifest entry, the retry wrote all 24 hours, and compaction folded both into
`all_day.parquet`. The manifest's 483,737 is the true count.

**Repaired the same day, on the owner's explicit approval under rule 8.**
`scripts/research/drop_doubled_bybit_eth.py` keeps the first half of every group in that
slice in file order and touches nothing else; the day went from 11,823,695 rows to
11,649,827. Three guards, because this deletes recorded data: an odd group anywhere in
the slice aborts the run, since "keep half" is undefined on an odd count and a wrong
parity would mean the diagnosis is wrong (all 140,841 were even); the write is refused
unless it drops exactly 173,868 rows and leaves exactly the manifest's 483,737; and the
rewrite is atomic with the file re-opened and counted afterwards. The filter is applied
to the pyarrow table rather than a polars frame so the schema survives, and the partition
still carries its `liqfade_rederive` stamp, so the resume rule will not redo the day.

The check that matters is the parity flipping the right way: hours 00 to 11 were 0% odd
groups before and are **88.4%** after, against 87.8% in the untouched hours 12 to 23. The
repaired half is now indistinguishable from the half that was always right. All four
Bybit coins match the manifest to the row, and the lake's Tardis `trades` total is now
exactly the Tardis manifest's 361,914,311, which closes 02b's residual to zero. The
year's `trades` total is **1,790,576,438**, not the 1,790,750,306 in the table above; the
repair is recorded in an appendix to `reports/data_quality/counts.md` rather than by
regenerating it, so the report stays the record of what the gate actually found.

Blast radius was small and known before the fix ran: `candles_1m` filters
`venue == "hyperliquid"` before it does anything, so no bar moved, and Tardis is the
cross-venue validation set rather than a training set.

Chose to repair rather than re-pull. The re-pull would have been free and is the more
obvious fix, but it rewrites rows in a partition that also holds 8.0M Binance, OKX and
Hyperliquid rows and carries a stamp that a naive rewrite would drop, so it has more ways
to go wrong than deleting a copy that is provably a copy. Reverse if a second doubled
slice ever turns up: at that point the general fix belongs in `tardis_free`, which should
be recording its manifest entry before the write rather than after it.

The wider lesson is the one the year keeps teaching: **`tid` is null on Tardis Bybit
rows**, so `(coin, tid)` cannot deduplicate them, and the duplicate-key heuristic that
finds nothing real on `liquidations` finds nothing real here either. What found it was
comparing the lake against the manifest per venue and per day, which is why the gate
report now splits by both.

**The side rule holds in all twelve months.** Mean signed price move in the 60s into each
liquidation, Hyperliquid rows only so that the liquidations and the price series come from
one venue. Long liquidations are forced selling and the price into them is falling in
every single month, by 68 to 423 bps, against short liquidations rising by 65 to 161:

| month | long n | long mean bps | short n | short mean bps |
|---|---:|---:|---:|---:|
| 2025-09 | 298,728 | -143.16 | 98,284 | +124.30 |
| 2025-10 | 729,986 | -423.13 | 123,253 | +109.08 |
| 2025-11 | 489,810 | -113.72 | 162,108 | +154.58 |
| 2025-12 | 237,121 | -74.92 | 91,214 | +152.88 |
| 2026-01 | 315,594 | -127.96 | 108,140 | +75.93 |
| 2026-02 | 279,724 | -68.14 | 125,588 | +66.61 |
| 2026-03 | 240,401 | -76.79 | 132,533 | +64.97 |
| 2026-04 | 127,925 | -110.15 | 149,261 | +70.59 |
| 2026-05 | 205,904 | -85.83 | 145,105 | +67.69 |
| 2026-06 | 454,241 | -68.12 | 170,171 | +87.36 |
| 2026-07 | 294,624 | -116.10 | 167,310 | +136.44 |
| 2026-08 | 208,049 | -180.21 | 266,197 | +161.17 |

The two extremes are the two facts about the year: October 2025 at -423 bps is the
2025-10-10 cascade, 394,452 liquidations in a day against a median of 11,005, and August
2026 is the only month where short liquidations outnumber long ones. Neither is a data
question, and the sign never turns.

**Tardis rows land on 5 days that are not a first of the month, and it is 40 rows.**
`trades` shows Binance on 14 days, OKX on 13 and Bybit on 16 rather than 12 each. The
extras are 2025-09-30 (28 rows), 2025-11-30 (1), 2026-01-31 (1), 2026-02-28 (1) and
2026-07-31 (9): trades stamped in the last milliseconds before midnight that Tardis
includes in its day-01 file, bucketed into the previous day's partition by `ts_ms`. The
same effect as the six fills on 2025-08-31. `liquidations` shows a clean 12 days for each
of the three venues, no Hyperliquid row from Tardis anywhere in either table, and the
`tardis` manifest records the 48 Hyperliquid liquidation requests as answered with no
rows, which is the 400 that Tardis never carried them.

**Every `candles_1m` file is newer than the `trades` file it was built from**, on all 364
days. The bars are the ones rebuilt after the 02d rederive, not the ones before it.

**Phase 2 is done.** No unexplained hole. One explained defect, 173,868 rows in one venue
on one day of the validation set, with a known fix. One property of the data that the
research has to plan around, the June 2026 book cadence.

## 2026-09-05 (Phase 3 features)

**The grid row at `t` covers `[t - step, t)`, strictly before t.** The no-lookahead rule is
enforced once, in `features/grid.py`, rather than re-checked in nine feature modules: a
value read at t was knowable at t, so every feature built on the grid inherits the
guarantee. A trade stamped exactly on the second belongs to the next bar. Reverse never;
the property tests in `tests/test_features_*.py` pin it.

**`mid` prefers the bbo midpoint and falls back to the last trade price.** The historical
year has no bbo at all, so for the backfilled window `mid` is the last trade price and
carries no spread. That is accepted only because the cost model subtracts an estimated
effective spread per side (`reports/research/PREREGISTRATION.md`). Reverse when the
recorder has enough bbo to rerun on quotes.

**The extreme of an episode is searched to `end_ts + join_gap_s`, not to `end_ts`.** The
silence is what ended the episode, and the price impact of the last liquidation lands in
the seconds after it. Searching only to `end_ts` would record `anchor == extreme` for every
single-event episode and score a zero move. Reverse if a re-cut of the episode definition
is logged first, per the preregistration's two-re-cut limit.

**The MAD in `features/zscore.py` is the streaming form.** `MAD_t = rolling_median(abs(v -
rolling_median(v)))`, two rolling medians, rather than the exact per-window median of
deviations from that window's own median. Exact MAD needs a median of medians per window,
which does not vectorize: a year of one coin at 1-minute samples is 525,600 windows of
1,440 points, minutes per coin against the phase's one-minute-per-coin-year budget. Over a
slowly moving median the two agree closely. Reverse if a research result ever turns on the
difference.

**The z-score scales the MAD by 1.4826.** Without it a threshold of 3 means three MADs,
which is about two sigma on normal data, and every threshold in
`docs/STRATEGY_AND_RISK.md` is written as a z. Reverse never.

**Sums over a partial window are reported; volatility over a partial window is null.** A
sum of what happened is still what happened, but an understated `rv` is a denominator that
inflates everything divided by it, including the stop distance. So `rv_{w}s` is null until
its window is full and liquidation and CVD sums are not.

**Phase 3 features run in a few hours over a year, not the budgeted twenty minutes.**
Measured 2026-09-05 on a synthetic year of one coin at 1s resolution: `build_grid` 50s,
`liq_intensity` 14s, `robust_z` 63s, the four flow functions 149s, 275s in all against the
60s per coin-year the prompt asks for. Nothing loops over rows. The cost is that a year at
1s is 31.5 million rows, and every rolling op carrying `.over("coin")` re-partitions and
re-materialises the frame: measured standalone, `.over("coin")` costs about 4x on a rolling
sum and turns a free `shift` into 1.75s. Accepted because the run happens once, the pipeline
chunks by coin-month so memory is never the constraint, and `build_episodes` caches its
output so the other seven scripts do not repeat it. Reverse by folding the four flow
functions into a single pass over a single sort, roughly a 3x win, at the cost of merging
four independently tested seams into one.

## 2026-09-05 (Phase 3 research, defects found on the first real run)

**The research scripts never applied the configured universe, and the first year of
results is void because of it.** `config/default.yaml` carries `universe.min_day_ntl_vlm_usd`
(20M), `min_open_interest_usd` (5M), `min_listing_days` (14) and `exclude`, and CLAUDE.md
says the universe is "high market-cap, liquid HL perps, selected dynamically by volume and
open interest". `scripts/research/_lib.py` read none of it and studied every coin the
liquidation table mentions.

Logged **before** the fix is run, per the preregistration's anti-tinkering rule, stating
what had already been seen. This is recorded as a **defect, not a re-cut of the episode
definition**: the specification always said the universe was filtered, so neither of the
two permitted re-cuts is consumed. Owner's decision, 2026-09-05.

What had already been seen on the unfiltered year (2025-09-01 to 2026-08-30):

- 279,307 episodes detected, 5,149 qualifying, 3,262 clusters.
- Q1 headline +84.3 bps net-stress at +15 min, 95% CI [+1.0, +195.8], median +28.3. A mean
  three times its median with a lower bound of one basis point.
- Q8 DECISION yes on `k_stop = none`, no stop at all, beating hold-to-horizon by 0.2 bps
  (+84.5 against +84.3). Every cell's interval spans zero.
- Capacity-weighted edge reported as $130,342,102 a year, which is its own proof that
  something upstream was wrong.
- **1,496 of the 5,149 qualifying episodes (29%) were builder-deployed HIP-3 markets**:
  `cash:AMZN`, `cash:GOOGL`, `cash:USA500`, `flx:GOLD`, `flx:OIL`, `flx:SILVER`, `xyz:TSM`,
  `xyz:MU`, and `hyna:BTC`/`hyna:ETH` duplicating the majors on third-party DEXs. All 1,496
  have no `asset_ctx` row at all, so their net-stress was null: they contributed nothing to
  the mean while inflating the episode and cluster counts that the preregistered sample bar
  is read against.
- Effective spread p99 4,805 bps and max 18,333 bps; gross PnL max +36,212 bps. Broken mids
  on illiquid tape, not trades.

The fix: one universe rule, applied at episode detection in `build_episodes`, reusing
`liqfade/recorder/universe.py:select_universe` rather than writing a second copy of it. A
coin is in the universe for a UTC day when the recorder's own rule says so, evaluated on the
last `asset_ctx` sample strictly before that day began. A coin with no `asset_ctx` row is
out, not null. A coin whose name contains a colon is out: those are builder-deployed HIP-3
markets and are not the venue this strategy trades. `min_listing_days` is measured from the
coin's first `asset_ctx` sample in the lake, so a coin listed before the lake starts reads as
old as the lake and no older.

**The top-N-by-volume ranking is evaluated once a UTC day, not once a minute.** It is a
cross-sectional rank over 300 coins, and half a million minutes of it does not compute in a
research run; the recorder itself refreshes it hourly and `day_ntl_vlm` is a rolling 24h
figure that barely moves within a day. Every input to it is trailing: the snapshot precedes
the day it selects for. The per-coin floors are also checked at the episode's own trailing
minute sample, so an episode in a coin that fell through the floor mid-day is still excluded.

**Phase 3 research runs peak at 2.23 GB (Q1) and 2.57 GB (Q8) over a year, inside the 3 GB
budget on an 8 GB machine.** Amended 2026-09-05: the figures accepted earlier that day were
3.0 and 3.3 GB, measured before the universe filter below. Filtering the universe cut the
episode count from 5,149 to 2,722 and brought both runs inside the budget on their own. A
month runs at 1.6 GB.
The change that would clear the year, materialising a dense core plus narrow islands around
the five horizon timestamps instead of a dense 66 minutes, saves about a gigabyte and was
measured to move the March headline from +38.5 to +37.9 bps: `mid` is a forward-filled last
trade price, an island cannot forward fill from a trade outside itself, and a row dropped for
having no price lets the horizon's asof reach a price from after the horizon. Reverted. A
smaller footprint is not worth a lookahead. Reverse only with a design that keeps the
forward fill correct across the gaps.

## 2026-09-05 (Phase 3, decided after seeing the filtered year)

Everything in this section was **decided after the numbers were seen**, which the
preregistration requires to be said out loud. None of it moves a bar that a result was then
read against; the Q8 change removes a clause that no result could ever have satisfied.

**Q8's "beats hold-to-horizon" clause was a drafting error, and stop rule 2 governs the stop
decision.** The preregistration said a cell must *beat* hold-to-horizon. A stop is insurance:
on a sample selected for reverting, it cannot beat no-stop in expectation, because its only
effect is to leave some reversion on the table in exchange for bounding the losses that do
not revert. No grid could ever have satisfied that clause, so the question could only ever
have answered no, which is not a measurement. Stop rule 2 is the condition the project
actually cares about and is what Q8 now reads: is there a cell that is positive, does not
stop out of half its trades, and sits in a positive neighbourhood rather than alone.

On the filtered year all twelve cells satisfy it. **Q8's decision is yes with a stop:
`k_stop = 1`, `k_target = 0.8` keeps +80.6 of the +84.6 bps that holding to the horizon
returns, at a 15% stop-out rate.** Owner's decision, 2026-09-05. Hold-to-horizon stays in the
report as the reference number, not as a bar.

**A report may not name a stop rule whose condition is unmet.** Q8 printed "Stop rule 2,
unharvestable edge" on an outcome where twelve of twelve cells met stop rule 2's condition,
because the citation was prose in the verdict text and nothing connected it to the rule.
`scripts/research/_lib.py:decision` now refuses a reason mentioning a stop rule, takes the
rule by number, writes the citation from the preregistration's own wording, and refuses to
cite one on anything but a `no`. Q3 carried the same defect for stop rule 5 and now names it
only when precision is genuinely under 0.5.

**Phase 4 sizes on the isolated-coin and top-5-removed numbers, not on the pooled headline.**
Q1's pooled +84.6 bps is +26.7 in the isolated-coin bucket and +34.0 with the top five
clusters removed. The difference is cluster upside, and a cluster is one bet: a market-wide
sweep liquidates dozens of coins inside one minute and the strategy would have been in all of
them at once. **The risk layer must therefore cap exposure per cluster**, not only per coin
and in total, and Phase 4 sizes on 27 to 34 bps rather than on 85. Written into
`reports/research/SUMMARY.md` beside the Q1 row.

**30 bps becomes a pre-trade spread guard in Phase 4, and is not applied in Q1.** Inside the
filtered universe the used effective spread is p50 4.1, p95 26.3, p99 137.5, max 5,107 bps,
and the whole tail is in major and minor: the core tier's own p99 is 22.9 with a max of 322.
Q1 reports a "used spread <= 30 bps" robustness row alongside the others rather than capping,
so the effect of the guard is visible instead of assumed. The 30 comes from the core tier's
p99 rounded up, which is the level at which a spread stops looking like a liquid perp's. It
belongs in `filters.max_spread_bps` in Phase 4, evaluated pre-trade on live book data, not in
the research sample.

## 2026-09-05 (Phase 3 config writeback)

Applied to `config/default.yaml` after the owner read `reports/research/SUMMARY.md`. Every
change below names the report it came from, the sample it was measured on and the value it
replaced. Nothing outside the preregistration's writeback list was touched.

**`intent.k_target` 0.5 -> 0.8 and `intent.k_stop` 1.5 -> 1.0.** From Q8's winning grid cell
over 2,722 episodes in 1,758 clusters, taken only because every neighbouring cell is also
positive, which is the condition the preregistration attaches to this writeback. The cell
keeps +80.6 of the +84.6 bps that holding to the horizon returns, at a 15% stop-out rate.
`reports/research/q8_path.md`. Reverse if a Phase 4 backtest on the held-out last 25%
disagrees.

**`intent.time_stop_s` unchanged at 900.** Q8's grid used a 15 minute time stop throughout
and tested no alternative, so the data did not speak to it and the preregistration forbids
moving a value the data did not choose.

**`cascade.proxy_min` unchanged at 4.0.** Q3's F1 curve is flat at 0.01 across every
preregistered threshold (2, 3, 4, 6), so there is no minimum to read off it. The number
stays where it was rather than being moved to a value the data did not produce.

**`filters.max_spread_bps` 3/6/12 by tier -> 30 flat.** Two findings, not one. Q6 measured
the tiered caps removing 51.9% of the sample to remove 36.3% of the loss tail, a
concentration of 0.70x against a 2x bar: as a signal filter they only shrank the book.
Separately, the effective spread inside the filtered universe is p50 4.1, p95 26.3, p99
137.5 bps, and the whole tail sits in major and minor while the core tier's own p99 is 22.9
with a max of 322. 30 is that core p99 rounded up. It is a **sanity guard, not a filter**:
above it a quote has stopped looking like a liquid perp's. Q1 reports the slice it would have
removed (`used spread <= 30 bps`, +32.2 bps on 2,602 of 2,722 episodes) rather than applying
it, so the effect is visible instead of assumed.

**`filters.skip_when_btc_cascading` true -> false.** Q6: removes 26.7% of the sample to
remove 33.3% of the loss tail, a concentration of 1.25x, under the 2x bar. The market-wide
bucket is also where the pooled edge lives (+160.4 bps against +26.7 isolated), so skipping
it skips the upside as well as the risk. What handles sweeps instead is the per-cluster
exposure cap, below.

**`filters.funding_extreme_bps_8h` 30.0 -> null, and the field is now nullable.** Q6: the
gate removes 0.6% of the sample and 0.0% of the loss tail. `null` means the gate is not
applied; the alternative, a threshold set so high it can never fire, is a config that lies
about what runs.

**Not written back, and why.** `exhaustion.*` keeps its values although Q2 found a flat 120s
delay beats the rule on every month: replacing the rule with a delay is a Phase 4 strategy
decision and would need a new config key, which is outside the writeback list this phase
agreed to. `features.levels.*` keeps its values although Q7 found in-zone episodes revert
*worse* than out-of-zone (+36.1 against +146.3 at 15 bps): the code stays built, tested and
unused. `filters.min_edge_multiple`, `sizing.risk_frac` and every gate in
`docs/STRATEGY_AND_RISK.md` section 5 are untouched, as the preregistration requires:
research measures the edge, it does not set risk limits.

**Phase 4 must add a per-cluster exposure cap.** Not a writeback, because no such key
exists yet. Q1's pooled +84.6 bps is +26.7 in the isolated-coin bucket and +34.0 with the top
five clusters removed; the difference is cluster upside, and a cluster is one bet, since a
market-wide sweep liquidates dozens of coins inside one minute and the strategy would be in
all of them at once. Sizing on the pooled number would size one bet as if it were fifty.
`sizing/` and `risk/` need a cap on concurrent exposure within a five minute window across
all coins on the same side, and Phase 4 sizes on 27 to 34 bps rather than on 85.

## 2026-09-06 (Phase 4 entry, owner override of stop rule 5)

**The owner overrides stop rule 5 and builds Phase 4 now.** Phase 3 parked the project on
that rule: the edge is real on exact S3 liquidations (Q1 +84.6 bps pooled, +26.7 isolated;
Q8 harvestable with `k_stop=1`, `k_target=0.8`) but the live bot cannot see those
liquidations, the HL-native proxy reaches precision 0.01, and Q4 cannot be answered until
the recorder has sixty days on 2026-11-04. The rule's instruction is to park rather than
build blind. The owner accepts that reasoning and overrides it anyway, on this basis: the
engine is the same engine whichever trigger eventually feeds it, and building it now means
the day a live-visible trigger passes its bar the answer is a rerun rather than a week of
work. Nothing about the science changed; the decision is about build order.

**What that override does and does not license.**

- The historical year is backtested on the archive's **exact** liquidations, and every
  number that comes out of it is **a ceiling, not a forecast**. It is what the strategy
  would have made with a trigger nobody can see in real time.
- **No live trading path is built in this phase.** `execution/` gets `rounding.py` and
  nothing else; `live/` and `alerts/` stay empty.
- **Phase 5 stays blocked** until a live-visible trigger passes its bar: Q3's re-cut at
  thresholds above the preregistered range, or Q4 rerun at sixty recorder days. Neither
  can happen before 2026-11-04.

**The trigger becomes a config switch, `cascade.trigger`.** Three values, one detector, one
engine, so the ceiling run and the eventual live run differ by one config key rather than
by a code path.

- `exact`: the lake's `liquidations` table, source `hl_s3`. Backtest only, and the default
  for the year run. `Settings` refuses it on mainnet outright and
  `live_trading_block_reason()` names it, so the Phase 5 loop cannot start on it by either
  route. That refusal is written now rather than when Phase 5 is built, because the person
  who would forget it is the person writing Phase 5.
- `proxy`: `hl_forced_proxy_60s` over `cascade.proxy_min`. Q3 measured precision 0.01 at the
  preregistered thresholds and a re-cut is outstanding. Shipped, default off.
- `crossvenue`: aggregated `liq_z_60s` over the recorded Bybit and OKX feeds, weighted by
  `venues.*.liq_weight`. What Phase 5 runs if Q4 passes. On the historical year it has data
  on the twelve Tardis days only, so every report naming this trigger prints the days it
  actually had data for.

Every report's first page names the trigger it ran on. A report that does not is a report
whose numbers cannot be read.

**Nothing research said no to is deleted.** Q2, Q5, Q6 and Q7 all returned `no`, and the
temptation is to strip the code they rejected. Every one of those rules stays as code behind
a config switch defaulted off, so the owner can turn one on in a backtest config and read
the cost rather than re-litigate the decision from memory. `exhaustion.mode` defaults to
`fixed_delay` at 120s because Q2 says the flat delay carries more PnL than the rule, and the
rule code stays under `exhaustion.mode: rule`. `zone_proximity` ships disabled because Q7
found in-zone *worse*; `features/levels.py` stays. The all-rules-off, `fixed_delay`
configuration is the golden baseline the tests pin.

The partial-liquidation rhythm (Q5) is the exception: it never reached the spec as a rule,
so there is nothing to switch off and none is added.

**Phase 4 sizes on 30 bps and adds a per-cluster exposure cap.** Already argued in the
2026-09-05 entries; recorded here as the thing this phase implements.
`risk.cluster_window_s` (300s) and `risk.max_cluster_exposure_frac` (0.25 of equity) cap the
gross notional entered across all coins on one side inside one window, because a sweep that
liquidates forty coins in a minute is one bet and the pooled +84.6 bps is what sizing it as
forty would assume.

**Reverse this entry** if the owner decides the ceiling numbers are being read as forecasts,
or if Q3's re-cut and Q4 both fail on 2026-11-04, in which case the engine built here has no
trigger and the momentum variant needs its own Phase 3.


## 2026-09-06: the backtester streams the lake and replays only where the cascades are

**`load_inputs` is gone. Nothing in `backtest/` reads a date range into one frame.** The
year run on four coins was killed by the OS twice on an 8 GB machine, and the reason was
not the strategy: `load_inputs` handed one `scan_parquet` every file in the window. The
sizes are worth writing down because the docstrings had them wrong. `l2` is not hourly
snapshots on four coins, it is a snapshot roughly every 0.55s, twenty levels a side, 12.6
million rows a day; a year is over four billion rows and a single month needs about 50 GB.
`load_day` takes one partition and there is deliberately no range version of it, so the
mistake cannot be made again by accident.

**A day's feature grid is built per coin, not per day.** Forty-two coins on a 1s grid is
3.6 million rows and about a gigabyte of float columns before any transform makes a copy.
Every function in `features/` is already `.over("coin")` or `by="coin"`, so building one
coin at a time changes no number and lets each coin's grid go before the next is read. What
survives the day is the few thousand rows a cascade could involve.

**Each day is built with the previous day in front of it, and cached.** `rv_300s`, the CVD
slope and `traded_usd_300s` reach back minutes; `robust_z` reaches back a full 24 hours and
`with_ctx`'s mark-divergence percentile exactly 24 hours, so one previous day is enough and
two would be waste. That is the same previous-day carry `scripts/research/q3_proxy.py`
uses. The result goes to `data/cache/features/{hash}/date=.../grid.parquet`, documented in
`docs/DATA_SOURCES.md`. The run's first day is never cached, because it has no previous day
inside the window and its grid is therefore a function of where the run started rather than
of the date.

**Raw tape is replayed only inside cascade windows.** A window opens
`backtest.window_warmup_s` (300s) before a candidate and closes
`exhaustion.max_age_s + intent.time_stop_s + backtest.window_margin_s` (900 + 900 + 60s)
after it. That end is an upper bound on the last thing the cascade can cause: the latest
tick it can signal on is `max_age_s`, the position that signal opens is flattened by the
time stop at the latest, and the margin covers the order latency and the tick the exit
lands on. Outside a window the portfolio is flat and no order is working, so the tape there
holds nothing a decision could have used. **The engine asserts that at every window end
rather than assuming it**, because it is the one way this optimisation could silently
change a result, and the report prints the share of the clock actually replayed so a run
creeping back towards 100% is visible as a number rather than as a wall clock that quietly
got worse.

`active_mask` now matches each row to the last candidate at or before it instead of
counting a rolling window of rows. Same answer, and it is the form that can take the
previous day's candidates as a carry, which a cascade starting at 23:59 needs.

**Proof.** `trades.parquet` is byte-identical to the engine this replaces, on the golden
fixture and on `2025-10-09` to `2025-10-11` of the real lake for `BTC,ETH,SOL,HYPE`; the
digest and the command are pinned in `tests/test_backtest_streaming.py`.

Measured on the owner's machine, `/usr/bin/time -l`, nothing else running, with
`features.z_robust: false` so that the runs have fills to be identical about.

The three reference days, `2025-10-09` to `2025-10-11`:

| run | wall | peak RSS |
| --- | --- | --- |
| four coins, the engine this replaces | 8.6 min | 2.39 GB (3.14 GB on a re-run) |
| four coins, streaming | 37 s | 1.59 GB |
| four coins, streaming, `--no-cache`, with `tail_only` | 37 s | **1.44 GB** |
| the 42-coin research universe, `--no-cache`, with `tail_only` | 46 s | **1.31 GB** |

The year, `2025-09-01` to `2026-08-30`, four coins:

| run | wall | peak RSS |
| --- | --- | --- |
| cold cache | 60.7 min | 2.64 GB |
| warm cache | 32.0 min | 1.96 GB |

**The universe costs almost nothing.** Ten times the coins is 23% more wall clock and
*less* memory, 1.31 GB against 1.44 GB. Both halves of that are the per-coin day loop
doing what it was built for: the working set is one coin-day whatever the universe size,
and `l2` (the largest table in the lake by an order of magnitude) exists for four coins
only, so the thirty-eight extra coins bring trades and nothing else. Four of the
forty-two had no tape at all in that window and simply have no grid, which is the right
answer rather than an error. Three days is the honest window for this: peak RSS here is
bounded per coin-day, so a year reaches the same working set.

The year replayed **10.4% of the tape in 1,376 windows**, skipping 28,193,589 simulated
seconds; the three reference days replay 14.1% on four coins and 29.7% on the universe,
which is what a busy stretch looks like. The warm run saves 28.7 minutes, 47% of the wall
clock, and writes a `trades.parquet` byte-identical to the cold run's (`703d0a95…`), which
is a second proof of the cache on top of the unit tests: a year of features read back off
disk produces the same sixty-four trades as a year of features built from the tape. A year
of four coins costs 6.8 GB of cache, about 1.7 GB per coin-year.

**What is still over budget.** The year's cold run was 2.64 GB against the 2 GB the task
asked for, and its warm run 1.96 GB. `tail_only` takes 0.15 GB off the per-day working set
and the year has not been re-measured with it, so the honest statement is that the working
set is comfortably under 2 GB and a cold year is not: the difference between the 1.59 GB
three-day peak and the 2.64 GB year peak is allocator high-water mark accumulated over 364
days of differently sized frames, not a bigger working set. Every run after the first is a
warm one.

**Two things found along the way that were not this change's to fix.**

Under the shipped `features.z_robust: true` the liquidation z-score was null on every row
of real data, so the `exact` trigger never fired and a backtest over the lake found
nothing.
`liq_long_usd_60s` is zero in 82% of grid seconds, so its 24h rolling median is zero and
the median absolute deviation of a mostly-zero series is also zero; `robust_z` correctly
refuses to divide by a zero scale and returns null, and the detector correctly refuses to
trade on null. Every part behaves as documented and the composition finds nothing: a full
run over 2025-10-09 to 2025-10-11, which carries the busiest cascade day of the year for
these four coins, produced zero intents and zero trades, and every `liq_*_z` on BTC's grid
over that window is null. With `features.z_robust: false` the same window gives 246
candidate rows and seven trades. The reference run above is therefore taken with the
non-robust z, so that it has fills to be identical about, and it stays that way: it pins
the engine, and re-taking it under a scale that has since changed would confuse the two
questions.

**Fixed in `b673bc8`**, by the 04c work running in parallel with this one, with a fallback
to the mean absolute deviation about the same median where the MAD is zero. That was the
right shape of fix: a scale that is defined for a series that is zero most of the time,
rather than a lower threshold. What a `liq_z_min` of 3 means on that scale is a
measurement, not three sigma, and 04c owns it.

`Settings.kill_switch_present` fails closed on any `OSError` from `os.stat`, which is
right, and the lake's USB drive produced one during an eight-minute run. The risk engine
recorded a `kill_switch` block and that run lost an entry it should have taken. Nothing to
change in the gate: a run whose funnel shows a `kill_switch` block with no KILL file on
disk has hit a drive hiccup and is not a reference.

## 2026-09-06: 04c, a discretionary confluence made objective, and counted rather than ANDed

**What this phase encodes.** A trader runs this setup by hand and takes tens of trades a
week. The liquidation is the signal; everything else exists to say "the bottom (or top) is
in" objectively instead of by eye. Five conditions became switches, and one deliberate
non-condition stayed out.

- `min_cascade_notional`, already shipped, is the liquidation itself and is the only rule
  the run config marks `required`.
- `oi_flush`: open interest down by `drop_min_frac` over the rv window, so positions were
  forced out rather than price merely moving, and optionally still up by `build_min_frac`
  over the build window, which is the crowding that was there to flush. The build half is
  measured to now rather than to the cascade start, so it is already net of the flush.
- `vwap_stretch`: mid at least `min_disp_bps` past a VWAP, on the side the cascade pushed
  it. `reference: day` is the session VWAP from 00:00 UTC, which is what a discretionary
  trader watches; `anchor` is the fixed pre-cascade window. There is no mode for the
  opposite sign. Fading when price is on the trend side of VWAP is a momentum rule and is
  out of scope for v0, so if the sign is wrong it is a sign flip and not a setting.
- `level_reclaim`: the swing failure and the three tap. The cascade extreme at least
  `sweep_min_bps` through a level that already existed, and mid `reclaim_min_bps` back on
  the near side of it now.
- `zone_proximity`, already shipped and off since Q7, is enabled by the run config as a
  vote rather than a veto.

**Order blocks are skipped on purpose.** They are in the same price-action primer as the
range, the swing point and the swing failure, and unlike those they have no definition two
readers would agree on. Encoding one means inventing a rule and then measuring the
invention. Nothing here is worse for their absence: the swept-and-reclaimed level is the
part of the idea that can be stated as arithmetic.

**Rules are required or optional, and the optional ones are counted.** An all-AND stack was
the wrong shape and would have been the expensive kind of wrong, because it produces a small
number that looks like a measurement. A trader's hand rate is tens of trades a week, and
the reason is that the corroborating conditions are corroboration: any one of them being
absent is normal. Every rule section gained `required`, defaulting true so nothing existing
moved, and `strategy.min_confluence` says how many of the enabled optional rules must pass.
Zero evaluates and reports them without ever blocking, which is how a rule earns its place
before it is allowed to cost a trade, and a config asking for more votes than there are
optional rules is refused rather than silently blocking everything. The report separates
"blocked by required rule X" from "blocked by the confluence count", which is nobody's fault
in particular, and prints each optional rule's pass rate. That pass rate is the evidence for
promoting a rule to required or dropping it, and it is the number to read first.

**The cascade trigger takes a USD floor and the z-score became optional.**
`cascade.liq_usd_min` is a per-tier mapping looked up exactly as `filters.max_spread_bps`
is; `cascade.liq_z_min` may be null; a validator refuses both null, which would admit every
row on the tape. The two ask different questions. A z says how unusual this flow is for this
coin, which is what travels across a universe whose notional spans two orders of magnitude.
A dollar floor says whether the liquidation was large enough to have moved price at all,
which is what a trader actually reads and which a z cannot say on a series that is quiet
most of the time. `candidate_expr` applies the most permissive tier, because it cannot see
which coin a row belongs to and a row it drops is a cascade that silently never happens.

**The robust z-score now has a scale, and the scale is not worth a threshold.** The defect
recorded on this date under the streaming change was that `features.z_robust: true` gave a
null z on every row of real data: `liq_long_usd_60s` is zero in 82% of grid seconds, so its
rolling median is zero and the median of the absolute deviations from it is zero too.
`robust_z` now falls back to the **mean** absolute deviation about the same median where the
MAD is zero, and an all-zero lookback still scores null.

Measured on the three reference days (2025-10-09 to 2025-10-11, BTC/ETH/SOL/HYPE) the fix
does what it was meant to and no more:

| scale | z defined | p99 of z on rows with liquidations | candidate rows at `liq_z_min` 3.0 |
|---|---|---|---|
| `z_robust: false` | 95.8% | 8.69 | 288 |
| `z_robust: true`, fixed | 95.8% | 1.8e11 | 316 |

The z exists again, so the `exact` trigger fires. But **`liq_z_min: 3.0` does not mean
anything on this scale.** The candidate count is 316 at 3.0, 292 at 20, 199 at 50, 110 at
100, and does not reach zero until 1e12: the mean absolute deviation collapses towards zero
whenever the trailing day is almost all zeros with one small burst, and the ratio explodes.
About 20 reproduces the old count (292 against 288) but does it by the flatness of the curve
rather than by selecting comparable events, so it is not a threshold and is not recorded as
one. `config/default.yaml` is left alone: the measurement justifies no value. The answer is
the notional floor, which is what `config/backtest/v1_confluence.yaml` uses, and the standing
question for Phase 5 is whether `z_robust` should default to false rather than to a scale
that is defined and meaningless.

**Two defects found on the way, both of which would have made this phase's numbers a
fiction.**

`LakeSource.levels` was never filled. On any run over the real lake `support_px` and
`resistance_px` were null on every row, so `zone_proximity` failed every candidate it saw.
It has been switched off since Q7, so nothing noticed, and `level_reclaim` reads the same
two columns. `LakeSource` now builds the level set once from `candles_1m`, the run plus a
fortnight in front of it, and only when a rule that reads levels is enabled, so a default
run still reads no candles at all.

The level join also took the most recently **formed** level of each kind rather than the
nearest one to price. For a rule asking whether the cascade traded through a level and came
back, that is a question about an arbitrary level, and the rule would have fired at close to
random. It now takes the nearest level below mid and the nearest above it, within
`features.levels.max_dist_bps`. That makes `level_reclaim` an existential test: its two
conditions together require the level to sit between the extreme and the current mid, and
the highest level under mid is the only candidate for that, so the rule passes exactly when
some level satisfies it. The cost is that it sees one level and not all of them, so where a
shallower level sits between the swept one and mid, that shallower one is what gets judged.

**Levels are those knowable at the start of the row's UTC day.** A fractal is confirmed
`fractal_n` bars after it prints, so an intraday swing becomes usable a quarter of an hour
after it forms, and honouring that exactly needs a lookup over both price and time on every
grid row. The day boundary buys one as-of join per day per side instead. It is a restriction
and never a lookahead: the day's start is earlier than every row it serves. A swing formed
this morning is not a level until tomorrow, which is close to what the rules are asking
about anyway, a level a trader would have drawn beforehand.

**What the year said, and why the number is not a measurement of the setup.** The run
(`config/backtest/v1_confluence.yaml`, 2025-09-01 to 2026-08-30, BTC/ETH/SOL/HYPE, 10,000
USD, `--lagged-ms 3000`) produced 776 candidates at the rule layer, 180 intents and 39
trades for **-1,083.41 USD**, and the 10% drawdown halt fired on **2025-11-21**, in month
three, after which the other nine months replayed halted. The 3s lagged rerun gave 39
trades and -1,137.20 USD, so nothing here is latency fragile; there was no positive
expectancy to lose.

**It is not a measurement of the setup, because the exit was behind the entry.** Of the 33
trades that exited at their target, **31 had the target on the wrong side of the entry**,
and the median hold on a target exit was **1.4 seconds**: the position opened and closed
immediately at a loss. An ETH short entered at 4369.2 and "hit its target" at 4394.5.

The cause is a real defect and it is not in this phase's code. `intent.target_px` is
`extreme + k_target * (anchor - extreme)`, a fixed point on the way back to the anchor,
and `Intent.edge_frac` measures the distance to it as `abs(target_px - entry_px)`. With
`exhaustion.mode: fixed_delay` the entry is 120s after the cascade starts and price is
usually still near the extreme, so the target is ahead and the absolute value is harmless.
With `exhaustion.mode: rule` the entry waits for the cascade to actually stop, and
`level_reclaim` waits further, until price is back above the level that was swept. By then
the reversion the target names has largely happened, the target is *behind* the entry, and
`abs()` reports the distance back to it as edge. `filters.min_edge_multiple` then passes an
intent whose expected move is negative by construction.

So the rule layer did its job and the exit undid it. **The fix is to make `edge_frac`
signed** so a target already passed is zero edge and `min_edge_multiple` drops the intent,
and only then is a run under `exhaustion.mode: rule` worth reading. Left unfixed here
because it is outside 04c and because a one line change to the intent maths deserves its
own commit, its own test and its own rerun rather than being folded into a phase whose
numbers it changes.

**What the rules themselves said**, over the 776 candidates that reached them:
`min_cascade_notional` (the only required rule) killed none, the confluence count killed 77,
and the pass rates were `vwap_stretch` 92.7%, `level_reclaim` 69.5%, `zone_proximity` 67.4%,
`oi_flush` 22.6%. `vwap_stretch` at 92.7% is close to not being a condition at all: a
cascade that has moved price by two volatilities is nearly always 30 bps from the session
VWAP, so at this threshold it votes yes almost always. That is what the pass rate table is
for, and it is the first thing to revisit. Peak RSS 2.30 GB, 78 minutes, 6.2% of the clock
replayed across 843 windows.

**Reverse this entry** if the confluence count turns out to be a way of admitting trades no
single rule would have justified, which is what the pass-rate table exists to make visible:
a rule that passes on almost everything is not corroboration, it is a rule that should be
dropped rather than counted.


## 2026-09-06: the target was behind the entry, and the four-coin year rerun

**Everything the 04c entry above reports as a result was measured with a broken exit.** The
fix and the rerun are here rather than folded into that entry, so the two sets of numbers
cannot be confused for each other.

**`Intent.edge_frac` was `abs(target - entry) / entry`.** The absolute value agreed with the
signed answer on every trade the fixed-delay entry produced, because that entry lands 120s
into a cascade with price still near the extreme and a target 80% of the way back is ahead
of it. Under `exhaustion.mode: rule`, and again with `level_reclaim`, the entry waits for
the bounce to be visible. By then price can be past the target, and `abs()` reported the
distance *back* to it as edge, so `filters.min_edge_multiple` passed intents whose expected
move was negative by construction. `edge_frac` is now signed in the trade's direction and a
non-positive edge is blocked as `target_behind`, its own reason, because a funnel filing
these under `edge_too_small` says the threshold is too high when what is wrong is the target.

**The reference digest moved, from seven trades to four, and that is the finding.** The
reference run of the streaming change carried the same defect: three of its seven trades
were targets already passed. The digest is re-pinned at
`ee18f6c2164c84e97e785439cecdd80f94d3099b98dcd5a01bd9979e93e10496` with the reason in the
test comment. A digest that had survived this change would have meant the fix did nothing.

**`intent.target_from` says what the fraction is a fraction of.** `extreme` is the spec's
formula and stays the default, so v0 and every Phase 3 number are untouched. `entry` gives
`target = entry + k_target * (anchor - entry)`, the same fraction of the reversion that is
*left*. The stop does not move in either mode: it belongs beyond the extreme, below the wick
that swept the level, wherever we got in. A late entry therefore has a nearer target and a
wider stop, and sizing divides the risk budget by the stop distance, so the same risk
fraction buys a smaller position. That is the real price of entering late, and it is now in
the numbers instead of hidden in a target nobody could reach.

**The backtest no longer reads the kill switch.** `Settings.kill_switch_present` fails
closed on any `OSError`, which is right for a live account and wrong for a replay: a USB
timeout on the lake's drive was recorded as a `kill_switch` block twice on this date, cost
the reference run an entry, and made two runs of one command disagree. `RiskEngine` takes
the check as an argument, defaulting to the real gate so the live path and the gate's own
tests are untouched, and the backtest passes one that never fires. A replay places no
orders; there is nothing for a kill file to stop.

**`v1_confluence.yaml` relaxes the account halts, on the owner's instruction.** Tuning runs
only, in that file alone: `max_dd_frac` and `max_daily_loss_frac` to 1.0 and the losing
streak cooldown to 0, so the whole tape replays instead of two months of trading and ten of
a halted account. The per-coin cooldowns and the cluster cap stay, because they shape which
trades a run takes rather than cutting the tape short. `config/default.yaml` keeps 0.10 and
0.03 and a test pins both halves.

**The rerun.** Same command, cache warm. The exit is a trade now rather than an accident:

| | before the fix | after |
| --- | --- | --- |
| trades | 39 | 51 |
| net | -1,083.41 USD | **-601.22 USD** |
| hit rate | 7.7% | **49.0%** |
| median hold on a target exit | **1.4 s** | **116.3 s** |
| target exits whose target was behind the entry | 31 of 33 | 3 of 26 |
| worst drawdown | 10.83%, halted in month three | 6.10%, nothing halted |

The 3s lagged rerun gives 51 trades and -558.72 USD, so nothing is latency fragile; there is
no positive expectancy to lose. `target_behind` drops **449** intents, which is the size of
the defect: those were trades before. The loss is now in the stops, 16 of them for -533.5
USD against -26.9 across all 26 target exits.

**Three trades are 38% of the loss and none of them is the strategy.** The three remaining
behind-target exits are entry slippage of 863 to 1,152 bps: the fill model filling far worse
than the resting price during a violent cascade, which puts the realised entry past a target
that was ahead when the intent was built. That is `backtest/fills.py` and the no-book path,
not the exit rule, and it is the next thing to look at rather than any threshold.

**`vwap_stretch` passes 92.7% of candidates and is barely a condition.** A cascade that has
moved price by two volatilities is nearly always 30 bps from the session VWAP. Left at 30
bps deliberately: the owner sets it after one clean run, and this is that run. `oi_flush` at
22.6% is the only one of the four that is discriminating much.

**The universe run's coin list, and a selection effect kept on purpose.** The run that
follows uses a universe fixed from `daily_universe` over 2025-10-09 to 2025-10-11 and
applied to the whole year, so September's coins are chosen by October's volume. Kept rather
than fixed, because comparability with the streaming change's memory numbers matters more
for a run whose question is cost. **The right answer is a universe that changes day by day**,
read from `daily_universe` with a one-day lag, which is how the live loop will pick coins
and what the backtest should do; it is a follow-up engine item and not this run.

**Reverse this entry** if signing `edge_frac` turns out to have been hiding a different bug
in the fill model rather than fixing one in the exit, which the 863 bps slippage above is
the reason to suspect.


## 2026-09-06: the lab is a client of the CLI, and Textual is how it draws

**`liqfade lab` spawns `liqfade backtest` rather than importing the engine.** The owner
wants to run backtests for weeks before going live without asking for a code change, which
means a tool that manages configs, launches runs and compares results. Making it a client
of the CLI and of the files the CLI writes buys two properties that an in-process runner
cannot have: a crash or a keystroke in the workbench cannot take a running replay down, and
closing the workbench leaves the replay going. It also means the lab has no privileged view
of a run: it sees the `--progress-file`, the stdout it captured and the report directory,
which is exactly what `liqfade watch` and the owner see. Reverse if a future feature needs
per-tick data the progress file does not carry, and even then prefer widening the progress
file to importing the engine.

**Textual, pinned to major 8, in a new optional `lab` extra.** It has to work on the laptop
and over SSH on the VPS with the same keystrokes, which rules out a web page and rules out
a curses hand-roll of tables, forms and modals. Optional because CLAUDE.md's rule that the
trading process never imports the panel applies to a TUI for the same reason; `make setup`
runs `uv sync --all-extras`, so the owner gets it anyway, and a box that installed the core
only keeps every other subcommand. `tests/test_cli.py` pins that importing `liqfade.cli`
does not import textual.

**The edit form is walked out of the `Settings` model, never listed.** A form that
enumerated knobs would go stale the first time a rule gained a threshold, and the owner
would tune a config the lab could not show. Walking `model_fields` means a field added to
`config/models.py` appears with its description and its validators, and saving goes through
the same `Settings.model_validate` a YAML file meets, so what the lab accepts is exactly
what the run will accept. `tests/test_lab_form.py` pins both the walk and the coverage of
the sections a backtest reads.

**One lake job at a time, enforced by pgrep as well as by the queue.** Two replays over the
year do not fit in 8 GB, and the run the lab must not collide with is usually one started
by hand in another terminal, which never took a lock the lab could see. `pgrep -f "liqfade
backtest"` sees it. When pgrep cannot be run at all the answer is "busy", because refusing
to start is the safe failure on a machine where a year run may already own the memory.
Reverse if the lab ever runs somewhere without pgrep, and then take a lock file **and**
keep the process check.

**Latency is an env override, not a new CLI flag.** `liqfade backtest` has no `--latency`
and adding one to serve the lab would put a second spelling of a config key into the CLI.
`LIQFADE_BACKTEST__LATENCY_MS` already works, goes through the same layering and the same
validation, and the Run screen shows it in the command it is about to run. The lab derives
the variable name from the loader's own prefix rather than spelling it, so it follows the
loader if that ever changes.

**`lab.json` beside each report, because `summary.json` names a config by hash.** The hash
is right for reproducing a run and useless for reading a list of thirty of them. The lab
writes the config file's path, name, notes and overrides beside the report when the run
ends, which is what the Results list shows and what the config diff compares. A run started
by hand has none, and Results says so rather than guessing. The lagged rerun's net is the
same shape of problem solved the same way round: `liqfade backtest` prints it, so it is
read back out of `run.log`. Reverse either of these by adding the fields to `summary.json`
if the panel ever wants them too, which would be a change to `backtest/report.py` and to
the golden report bytes.

**Nothing in the lab can write a loader layer or start live trading.** A save aimed at
`default`, `paper`, `testnet`, `mainnet` or `overrides` is refused by stem, so pointing the
lab at `config/` instead of `config/backtest/` cannot reach `config/mainnet.yaml`. The
Status tab reads `data/state.json` and never opens the control socket, so the workbench can
watch the box and not touch it.
