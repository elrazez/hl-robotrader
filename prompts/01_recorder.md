# Prompt 01: Phase 1 recorder and Panel v1

Owner notes: runs fine on the laptop for development; its real home is a VPS or any
machine that stays on (see `docs/DEPLOY_VPS.md`, recorder-only section). No exchange
keys needed. Telegram alerts need `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALERT_CHAT_ID` in
`.env`; without them the recorder still runs and logs alerts instead of sending them.

---

You are working in the liqfade repo. Read `CLAUDE.md` in full, then `docs/DATA_SOURCES.md`
(schemas, endpoints, partition layout) and `docs/ROADMAP.md` Phase 1. Follow them exactly.

This session is **Phase 1: the recorder**. Build `liqfade/recorder/`, the canonical
Parquet schemas it writes, a minimal outbound-only Telegram alert function, `liqfade
record`, and Panel v1. Do NOT build ingestion (Phase 2), features, strategy, backtest,
execution, or the live loop. Do not modify `strategy/`, `sizing/`, `risk/`,
`execution/`, `backtest/`, `live/` beyond leaving them as they are.

## What already exists and must be used, not rebuilt

- `liqfade/config/`: pydantic models with `extra="forbid"` plus YAML files at repo root.
  Adding a config key means adding the field to `models.py` AND the commented default to
  `config/default.yaml` together, or validation fails. The `recorder`, `venues` and
  `universe` sections already exist with the values you need.
- `liqfade/control/`: `ControlServer`, `ControlBackend` protocol, `StateWriter`,
  `StateSnapshot` with a `feeds: list[FeedState]` field shaped exactly for this phase.
- `liqfade/runtime.py`: `run_main(coro)` for the process entry. `liqfade/log.py`:
  `setup_logging(proc="recorder", env=...)` then `get_logger()`.
- `liqfade/cli.py`: replace the `record` placeholder with the real command. Keep the
  argparse structure; add flags only to the `record` subparser.
- Tests use `pytest` with asyncio auto mode; the `sock_dir` fixture in
  `tests/conftest.py` exists for anything binding a unix socket.

## Decisions already made. Follow these, do not re-open them.

- One process, one asyncio task per stream, supervised by a small task group that
  restarts a crashed task with the same backoff as a reconnect.
- Own `websockets` client for every venue. No SDK websockets.
- Every parsed message gets `rx_ms = int(time.time() * 1000)` stamped at receive, before
  any queueing. `ts_ms` is the exchange timestamp from the payload.
- Buffers are plain lists of dicts per (table, venue), flushed to Parquet when the hour
  rolls over OR the buffer hits `recorder.ring_buffer_rows`, whichever first, and on
  shutdown. Parquet writes go through pyarrow with zstd compression, written to a temp
  file then `os.replace`, same pattern as `control/state.py`.
- Partition layout, file naming, and the seven table schemas are EXACTLY as specified in
  `docs/DATA_SOURCES.md` "Canonical Parquet schemas". Column order as listed there.
  Schemas live in `liqfade/data/schemas.py` as pyarrow schema constants with a
  `validate_table(name, table)` helper. The recorder and (later) ingestion and backtest
  all import from there. This file is the contract of the whole project; get it right.
- Symbol mapping lives in `liqfade/data/symbols.py`: HL coin <-> `BTCUSDT` (Binance,
  Bybit), `BTC-USDT-SWAP` (OKX), Coinalyze suffix symbols. Functions
  `to_venue_symbol(coin, venue)` and `to_coin(venue_symbol, venue) -> str | None`.
  Unknown venue symbols map to None and the row is still recorded with `coin=None`.
- Universe: `metaAndAssetCtxs` every `universe.refresh_min`, select top
  `top_n_by_volume` by `dayNtlVlm` meeting the volume and OI floors, union
  `core_coins`, minus `exclude`. On change: subscribe new coins, unsubscribe dropped
  ones, log `event=universe_change` with both lists. Cross-venue streams follow the same
  universe through the symbol map.
- Reconnects: exponential backoff from `reconnect_backoff_min_s` to
  `reconnect_backoff_max_s` with `reconnect_jitter_frac` uniform jitter, counted per
  stream, reset after 60s of stable connection.
- Gap accounting: a silence longer than `recorder.gap_alert_s` on a stream that should
  be continuous (HL trades, bbo, l2Book; Bybit liq) increments that stream's gap
  counter. Binance/OKX liquidation streams are event-driven and sparse: for those,
  silence is only counted against the WS connection being alive (ping/pong), never as a
  data gap.
- Alerts: `liqfade/alerts/telegram.py` with one function
  `async def send_alert(text: str) -> bool` using httpx against the raw Bot API,
  reading `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALERT_CHAT_ID` from env at call time,
  timeout from `telegram.send_timeout_s`, returns False and logs on any failure, never
  raises. A module-level dedupe: the same alert text at most once per 10 minutes.
  This is the ONLY thing in `alerts/` this phase. The command bot is Phase 5.
- Alert triggers: any stream silent past `recorder.silence_alert_s`; disk usage of the
  data volume past `recorder.disk_alert_pct` (checked every 5 min via
  `shutil.disk_usage`); presence of `data/ALERT_DISK` (written by the VPS cron).
- Control plane: implement `RecorderBackend`. `status` and `get_metrics` return real
  feed data. `pause`/`resume`/`flat` raise `RuntimeError("not supported by recorder")`
  which the server turns into an error response. `kill` triggers graceful shutdown
  (flush buffers, then stop). `reload_config` re-resolves config and applies universe
  and threshold changes at the next refresh tick. Run `ControlServer` and `StateWriter`
  (interval from `control.state_interval_s`) inside the recorder process; `proc` is
  `"recorder"`.
- WS ping RTT: HL responds to `{"method":"ping"}`; measure send-to-pong and keep a
  rolling window of 300 samples; p50/p95 go into `LatencyState.ws_ping_p50/p95`. REST
  RTT: time the `metaAndAssetCtxs` call; goes into `rest_p50/p95`.
- Nightly compaction at `recorder.compaction_hour_utc`: rewrite yesterday's
  `date=YYYY-MM-DD` partition into one file per table named `{venue}_day.parquet`
  (or one `all_day.parquet` per table where venues share a table), delete the hourly
  files only after the compacted file is written and row counts match. Runs inside the
  recorder process as a scheduled task; also exposed as `liqfade record --compact-only
  --date YYYY-MM-DD` for manual runs.
- `liqfade record` flags: `--minutes N` (bounded run, for fixture capture and tests),
  `--data-dir PATH` (overrides `paths.data_dir`), `--compact-only --date D`.
- Venue payload parsing is EXACTLY per the endpoint details in `docs/DATA_SOURCES.md`:
  Binance `o.S == "SELL"` means longs liquidated; Bybit `S == "Sell"` means longs
  liquidated; OKX per its schema; notional_usd = px * sz; `complete` flag from the
  venue config. HL `trades` rows also feed the `trades` table with side from the
  aggressor.

## Deliverables

1. `liqfade/data/schemas.py` and `liqfade/data/symbols.py` as specified above.
2. `liqfade/recorder/` with: `buffer.py` (per-table buffers + flush), `writer.py`
   (atomic Parquet write + compaction), `streams/` package with `base.py` (connect,
   backoff, ping, gap and lag accounting in one reusable class) and one module per
   venue (`hl.py`, `binance.py`, `bybit.py`, `okx.py`) each containing a parser
   function `parse(msg: bytes, rx_ms: int) -> list[tuple[str, dict]]` (table name,
   row) that is pure and unit-testable, `coinalyze.py` (60s poller writing `xchg_ctx`),
   `universe.py`, `health.py` (gap/lag/disk checks and alert dispatch), `backend.py`
   (RecorderBackend), `main.py` (wires everything, entry for the CLI).
3. `liqfade/alerts/telegram.py` as specified.
4. CLI `record` implemented; `status` already works and must now show real feeds.
5. Panel v1: backend endpoints `GET /api/recorder/liquidations?hours=24` (per-venue
   hourly liquidation notional from Parquet via duckdb, session-gated) and
   `GET /api/recorder/disk` (bytes used/free of the data dir). Frontend: extend the
   existing page with a disk usage row, the universe list (already rendered), and a
   liquidations-by-venue bar chart. Use `uplot` for the chart; add it to
   `panel/frontend` dependencies, rebuild the bundle with `make frontend`, commit the
   bundle. If Node is unavailable, write the source, skip the build, say so at the end.
6. Fixture: after tests pass, run `uv run liqfade record --minutes 5 --data-dir
   /tmp/fixture_capture` yourself, then copy at most 2 MB of the result (BTC rows only
   is fine) into `tests/fixtures/parquet/` preserving the partition layout, and commit
   it. Unit tests that need real data read this fixture. If the network is unavailable
   in your environment, generate the fixture synthetically through the real writer code
   path instead, and say so in the summary.
7. Tests, all offline: per-venue parser tests using verbatim example payloads embedded
   as bytes in the test file; symbol map round trips; buffer flush on row count and
   hour roll; writer atomicity (concurrent reader loop, same technique as
   `test_state_writer_never_leaves_a_partial_file`); compaction preserves row counts
   and deletes hourlies only on success; gap counting distinguishes continuous from
   sparse streams; backoff stays within [min, max*(1+jitter)] over 1000 draws;
   universe selection from a canned `metaAndAssetCtxs` payload (floors, core union,
   exclusion); RecorderBackend over a real socket (reuse the harness from
   `tests/test_control.py`); alert dedupe; schema validation accepts a good table and
   rejects a wrong-typed column.

## How to work

- Print a short plan (files, test list) and wait for OK before writing code.
- Implement in this order, committing after each with `make lint test` green:
  schemas+symbols; stream base + HL; other venues + coinalyze; buffers+writer+
  compaction; universe+health+alerts; backend+main+CLI; panel; fixture.
- `mypy` strict does not cover `recorder/`, but keep it type-annotated anyway.
- No new dependencies. Everything needed (websockets, httpx, pyarrow, duckdb, orjson,
  polars) is already installed.
- No em dashes anywhere.
- If a venue payload in the wild does not match `docs/DATA_SOURCES.md`, trust the wild
  payload, parse defensively (log `event=parse_error` with the raw message, drop the
  row, never crash the stream), and note the discrepancy in `docs/DATA_SOURCES.md`.

## Definition of done

`make lint test` green with zero skips. `uv run liqfade record --minutes 2` on a
networked machine writes valid Parquet for at least `trades`, `bbo`, `l2`, `candles_1m`,
`asset_ctx` (HL) and connects all three cross-venue liquidation streams; `liqfade
status` during the run shows per-stream lag under 2s and the universe list; after the
run, `duckdb` can `SELECT count(*)` from every written file; the panel shows feeds,
disk and the liquidation chart against the running recorder. The 48h no-gap DoD from the
roadmap is verified later by the owner on the VPS; it does not block this prompt.

End with: what you ran, what you could not verify without a long-running network
session, and the owner checklist (start it on a VPS or leave the laptop on; add
Telegram env vars).
