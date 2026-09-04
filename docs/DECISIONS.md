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
