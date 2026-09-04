# Prompt 01b: recorder hardening before the VPS runs it for real

Owner notes: a full-codebase review on 2026-09-04 found a set of recorder problems that
would silently record wrong data, hide a dead feed, or leave a control socket in a state
two processes can fight over. None of them matter while the recorder runs on a laptop for
minutes at a time; all of them matter the moment it runs 24/7 on the VPS next to a live
loop. The seven money, secret and data-destroying findings were fixed on 2026-09-04 in
`main`; this is the rest of the recorder's share. **Run this in a fresh chat before
`prompts/05_execution.md`, and deploy it to the VPS before Phase 5 starts.** It touches
`liqfade/data/ingest/common.py`, so it must come after `prompts/02d_rederive.md` and
`prompts/02f_lake_hardening.md` have committed.

---

You are working in the liqfade repo. Read `CLAUDE.md` in full, then `docs/DATA_SOURCES.md`
("Canonical Parquet schemas", "Endpoint details", "What the wire actually sends" line by
line, because the wire is the contract the parsers implement), `docs/HYPERLIQUID.md`
(ping cadence, WS and rate limits) and `docs/DEPLOY_VPS.md`. Then read
`liqfade/recorder/` in full: `main.py`, `backend.py`, `buffer.py`, `writer.py`,
`health.py`, `universe.py` and every module under `streams/`. Also
`liqfade/control/server.py`, `liqfade/control/state.py`, `liqfade/panel/app.py`,
`scripts/deploy.sh` and `scripts/systemd/`.

Phase: Phase 1 hardening, run before Phase 5. Definition of done: no venue can write a
wrong side or a wrong unit without the row being dropped and counted; a stream that dies
or never starts raises exactly one alert that a human will read; two processes cannot
share one control socket; and the recorder has no untested module.

## Decisions already made

- **One concern per commit with its test**, `make lint test` green before each. Group the
  C items at the end into one tidy commit. The order below is roughly worst first; keep it.
- **A wrong value is dropped and counted, never guessed.** Every side and unit mapping in
  the recorder is currently a two-branch default, so an unexpected token silently becomes
  the opposite side. A flipped sign inverts the strategy, and it would survive into a
  backtest looking like signal. The new rule: map explicitly, and on an unknown value drop
  the row, count it, and let the parse-error alert carry it.
- **`notional_usd` means USD or it means null.** OKX `sz` is contracts of `ctVal` each, so
  `px * sz` is out by up to 100x. Until `ctVal` is joined from the instruments endpoint,
  write null and keep `px` and `sz` intact so the fix is a backfill rather than a re-pull.
- **A metric nobody reads is not a metric.** Every counter added here has to reach
  `FeedState` and therefore `liqfade status`, the panel and Telegram.
- **Do not widen the schema casually.** `candles_1m` has no `rx_ms` by design. If the
  closed-bar fix needs one, that is a canonical schema change: update
  `docs/DATA_SOURCES.md`, `liqfade/data/schemas.py` and note it in `docs/DECISIONS.md` in
  the same commit.
- **Nothing here changes the strategy-facing shape of the lake** except that OKX
  `notional_usd` becomes null and unknown-side rows disappear. Both are corrections.

## Do

1. **OKX `notional_usd` is null until `ctVal` is joined.** `recorder/streams/okx.py` and
   `data/ingest/tardis_free.py` both compute `px * sz` with `sz` in contracts. The live
   parser at least has the caveat in `docs/DATA_SOURCES.md`; the Tardis path has nothing.
   Write null in both, keep the raw fields, and put the reason in the docstring at both
   sites. Test: an OKX liquidation row has `notional_usd is None` and non-null `px`/`sz`,
   in both paths.
2. **Explicit side maps that drop and count unknown values.** `streams/hl.py`,
   `binance.py`, `bybit.py` and `data/ingest/tardis_free.py` all read like
   `"long" if x == "Sell" else "short"`. Replace each with an explicit dict lookup;
   an unknown key returns `None` and the row is dropped and counted. Keep the mappings
   agreeing with "What the wire actually sends": Binance `SELL` and Bybit `Sell` both mean
   a long was liquidated; HL trade `B` is a buy aggressor. Test each venue with a renamed
   or re-cased token and assert the row is dropped rather than flipped.
3. **A row with no usable timestamp is quarantined, never 1970.** `write_partitioned` and
   `Buffers.add` both bucket on `row.get("rx_ms") or row.get("ts_ms") or 0`. Historical
   rows legitimately carry `rx_ms = 0`, so a fill whose `time` is missing lands silently in
   `date=1970-01-01`. Raise, or route to a quarantine partition, and count it. Test: a row
   with no timestamp never reaches a 1970 partition.
4. **Per-stream universe refresh, `self.coins` set only after every stream took it.**
   `RecorderMain._apply_universe` assigns `self.coins` before awaiting `on_change`, and
   applies streams serially, so if stream 2 raises (a `ConnectionClosed` mid-`_send`)
   streams 3 and 4 never get the new coins, and the next refresh sees `coins ==
   self.coins` and returns `False`. Those venues then under-record that coin permanently,
   with one warning. Apply per stream under its own try/except, track what each stream
   actually took, and only advance the shared state when all of them have. Test: a stream
   that raises does not stop the others and does not mark the refresh done.
5. **Closed-bar-only candles, or `rx_ms` on `candles_1m`.** HL's `candle` channel pushes
   the in-progress bar repeatedly, so the recorder writes many rows per `(coin, ts_ms)`;
   the table has no `rx_ms`, compaction re-sorts by `ts_ms`, and afterwards nothing can
   tell which row was the closed bar. Prefer dropping updates whose `t` equals the last
   seen for that coin until `t` advances, which needs no schema change. If that is not
   workable, add `rx_ms` per the schema rule above. Test: a repeated in-progress bar
   followed by the closed one yields exactly one row.
6. **Parse-error counters in `FeedState`, with an alert.** `streams/base.py` drops the
   whole message on a parse failure with no counter, so a venue field rename takes a
   sparse feed to zero rows while `connected=True` and nothing anywhere says so. Count per
   stream, surface in `FeedState`, alert above a rate.
7. **A declared stream that has never delivered alerts after a grace period.**
   `health.py` reads `elif not stat.connected and stat.last_msg_ms:`, so a stream that
   connects and delivers nothing, ever, is invisible. That is precisely the Binance
   regional block `docs/DATA_SOURCES.md` warns about: "Confirm Binance liquidations
   actually flow once the recorder runs in Tokyo". Alert once when a declared stream still
   has `last_msg_ms == 0` after a startup grace (an hour is fine), and drop the
   `and stat.last_msg_ms` condition from the disconnect branch. `tests/test_recorder_health.py`
   currently locks the old behaviour in; update it.
8. **Per-coin gap stats for the HL continuous streams.** `StreamStats` is keyed
   `venue.table`, so if BTC's `bbo` subscription dies while ETH and SOL keep the stream
   alive, `lag_ms` stays near zero and no gap is counted. The Phase 1 DoD is "no gap over
   5s on HL trades and BBO for the core coins" and today's metrics cannot evidence it. Key
   by `(venue, table, coin)` for the HL continuous streams and surface the worst coin.
9. **Alert dedup keyed on the stream, not the text.** `health.py` embeds a value that
   changes every tick (`silent for {silent_s:.0f}s`) in the alert text, and
   `telegram._deduped` keys on the exact string, so a ten minute outage sends roughly 120
   Telegram messages and gets rate limited. Dedupe on the stream name, or bucket the text.
10. **`flush_all` shielded from cancellation.** Shutdown flushes inside
    `runtime.run_main`'s grace window; a slow drain that gets cancelled loses the
    partitions it had not written yet. Shield it, and flush before or while waiting on the
    tasks rather than only after.
11. **`state.json` 0600, and the control socket created 0600.** `state.json` is written at
    the default umask (0644), so equity, positions and open orders are world readable on
    the box. The socket is created by `start_unix_server` at `0777 & ~umask` and only
    chmodded on the next line; CLAUDE.md section 5 says created 0600. Wrap the bind in
    `os.umask(0o177)` or create `data_dir` 0700, and chmod the state temp file before
    `os.replace`.
12. **A flock on the control socket.** `ControlServer.start()` unconditionally unlinks an
    existing socket; the comment says a second process "is already forbidden" but nothing
    enforces it. Recorder and live both run 24/7 under systemd, so whichever starts last
    silently steals the socket and `/kill` and `/flat` land on the wrong process while the
    other keeps trading. Take an exclusive `flock` on `<socket>.lock` (or connect first and
    refuse if something answers) before unlinking. Consider per-process names
    (`control-{proc}.sock`, `state-{proc}.json`) as the cleaner fix and say which you chose
    and why. Also make `stop()` only unlink a socket `start()` actually bound.
13. **Coinalyze tests, with `httpx.MockTransport`.** `streams/coinalyze.py` is 161 lines
    with no tests, and `xchg_ctx` is the only canonical table with no parser test, so a
    mapping that writes nulls ships silently. `poll_once` already takes an injectable
    client and api key, so this is the same shape as the existing `FakeTardis` and
    `FakeApi` tests. Cover the row mapping, `_load_markets`, the `_chunks` batching, and
    that the api key never reaches a log line.
14. **A latency samples endpoint for the panel.** The Phase 1 DoD asks for "WS ping RTT
    and REST RTT to Hyperliquid as time series"; `LatencyTracker` keeps a rolling window
    but `LatencyState` exposes only p50/p95 scalars, and the panel renders six static rows
    (REST p95 is not even rendered). Add `/api/recorder/latency` returning the retained
    `(ts_ms, ms)` samples and plot it with the uPlot component already in the bundle.
15. **`deploy.sh` never auto-enables `liqfade-live`.** It runs `systemctl enable` and
    `restart` for any changed unit, so editing the live unit permanently enables the
    trading loop. Harmless today because `liqfade live` exits 2; load bearing from Phase 5
    on. Exclude it explicitly, matching the note the script already prints.
16. **Drop the watchdog unit until the process exists.** `liqfade-watchdog.service` is
    installed, enabled and restarted, but `liqfade.execution.watchdog` is an empty stub, so
    the unit crash loops and reads as "a watchdog is running". Remove the unit and its
    deploy wiring; `prompts/05_execution.md` adds both back together.
17. **`orjson` in `universe.py` and `streams/coinalyze.py`.** Both call `response.json()`,
    which is stdlib `json`. CLAUDE.md section 4 says orjson for all JSON.

18. **Cleanup, one commit.** `_REDACT` in `log.py` is exact-match on top-level keys, so
    `secret`, `signature`, `private` and anything nested pass through: make it a substring
    match on the lowered key. Delete `StreamStats.gaps_1h`, which is incremented and never
    read. Rename `record_message`, which fires per row so `msgs_1m` for `hyperliquid.l2`
    counts twenty times the real message rate, or count it once per message. Move
    `RecorderMain.reload`'s blocking YAML read off the event loop with `asyncio.to_thread`.
    Delete `recorder.flush_min` and `venues.coinalyze.rate_limit_per_min` from the model
    and the YAML, or wire them; today both are validated, printed by `config show`, and
    read by nothing. Fix the `ring_buffer_rows` description, which says "per coin per
    stream" while buffers are keyed `(table, venue, date, hour)`, so the real ceiling is
    per output file and the stated memory bound is off by the size of the universe. Give
    `Buffers.take_all` an explicit sort key so it cannot compare row lists on a key tie.
    Fix `runtime.py`'s shutdown reset, which the normal path returns before reaching.
    Give `liquidations_by_venue` an upper bound on `ts_ms` so a row with a future
    timestamp cannot 500 the panel's main chart. Drop the unreachable `MAX_LINE_BYTES`
    branch in `control/server.py`, which the StreamReader limit already covers, and the
    dead `--reload` flag on `panel serve`, which uvicorn ignores because it is passed an
    app object rather than an import string.

19. **Shutdown never drops the rows a failed write left buffered.** `_write` was changed on
    2026-09-04 to leave rows in the buffer when the write raises, so the next flush tick
    retries them rather than destroying the partition. At shutdown there is no next tick:
    `flush_all` walks the partitions once, and whatever is still buffered when it returns
    goes out with the process, with only a `log.error` behind it and a `health.alert` that
    may not survive the shutdown either. So the fix that stopped a failed write destroying
    rows mid-run still loses them at the end of one. Retry once, then write what is still
    buffered to a quarantine file under `data/` and alert; never drop. Use the quarantine
    mechanism item 3 introduces, so there is one place to look. Test: a `flush_all` whose
    writes fail every time leaves every row in a quarantine file and loses none. Pairs with
    item 10, which is the same window seen from cancellation rather than from a bad write.

## Deploy

After the commits, deploy to the VPS with `scripts/deploy.sh` and confirm on the box:
`liqfade status` shows every declared stream with a non-zero message rate (in particular
`binance.liquidations`, which is the whole point of item 7), the control socket and
`state.json` are both 0600 and owned by the service user, no watchdog unit is loaded, and
`liqfade-live` is present but disabled. Then let it run 24 hours and check the gap and
parse-error counters are zero or explainable.

## Report back

The commit hashes, what `liqfade status` showed on the box before and after, whether
Binance liquidations actually flow from Tokyo (and if they do not, propose dropping
Binance from `venues` rather than pretending it is a feed), which control-socket fix you
chose, and whether `candles_1m` needed the schema change. Next prompt:
`prompts/05_execution.md`.
