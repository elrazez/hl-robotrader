# Prompts 05d: the four ideas kept from the external bots scan

Owner notes: five prompts in one file, each pasted into its own fresh chat. They come from
`docs/research/external_bots_scan.md` (2026-09-08), which graded four public liquidation
bots against this repo and kept four ideas: a liquidation map recorded from public
Hyperliquid positions, the refusals the live loop currently throws away, a live-versus-replay
drift check (the Phase 5 DoD sentence as a command), and later a stop-order map. Every one of
them is test-driven: the prompt names the seams, the chat writes the failing test first.

None of them changes what the strategy does. A and B are recorder and plumbing work that can
start now, beside the 05c paper soak, because they touch nothing the soak measures. C is
gated on the entry policy landing in the live path. D is gated on weeks of A's data. E is
gated on D saying yes.

| Prompt | Builds | Branch | Chat | Runs | Gate |
|---|---|---|---|---|---|
| A | `hl_positions` table, the address book, the positions poller, deployed to the recorder | `07a-liqmap-recorder` | NEW | concurrently with B | none |
| B | refusals persisted from the live loop, `liqfade refusals` scorer | `05d-refusals` | NEW | concurrently with A | none |
| C | `liqfade drift`, live paper versus replay of the same days | `05e-drift-check` | NEW | after B is merged **and** the entry policy is in `live/` | see the 2026-09-07 05c entry in `docs/DECISIONS.md` |
| D | Q9, the preregistered research question on A's data | `07b-q9-liqmap` | NEW | after A has run 21 days on the VPS and the days are synced | data precondition inside the prompt |
| E | `hl_triggers` table from `frontendOpenOrders` | `07c-trigger-map` | NEW | only after D's report says yes | D |

Each prompt's first line is its routing line. Each chat sends its final report to the control
chat by `SendMessage` (session `hl-robotrader-38` at the time of writing; the name changes when the
session restarts, so ask the owner if it is gone rather than guessing from `ListAgents`). The control chat
merges. Paste only the part below each `---`.

Before pasting A, optionally paste this whole file into a chat with
`/mattpocock-skills:grilling` on top and let it argue with the design for ten minutes. The
scan doc already lists what could kill the map; the grill is for what the scan missed.

---

## Prompt A: the liquidation map recorder

ROUTING: NEW chat. Runs concurrently with prompt B. Branch `07a-liqmap-recorder` off `main`,
in its own worktree: `git worktree add ../hl-robotrader-liqmap -b 07a-liqmap-recorder main`,
then `uv sync --all-extras` there. When done, send the final report (prompt name, branch, tip
hash, what was deployed and what the first hour on the VPS showed) to the control chat
`hl-robotrader-38` with `SendMessage`.

/mattpocock-skills:tdd

You are working in the liqfade repo. Read `CLAUDE.md` in full, then
`docs/research/external_bots_scan.md` section 1 (the whole section, including "What could
kill it" and "How to start"), `docs/DATA_SOURCES.md` ("Canonical Parquet schemas" and the
Hyperliquid endpoint section), the rate limits section of `docs/HYPERLIQUID.md`, and these
files end to end, because the work mirrors them: `liqfade/recorder/streams/coinalyze.py`,
`tests/test_recorder_coinalyze.py`, `liqfade/recorder/main.py`, `liqfade/data/schemas.py`,
`liqfade/recorder/writer.py` (`_dedup_fields` and `_compact_partition`),
`liqfade/recorder/streams/hl.py` (`make_parser`, `_trade`, `_first_sight`) and the first 60
lines of `liqfade/recorder/streams/base.py` (the `CONTINUOUS_STREAMS` contract).

This session builds **step one of the liquidation map: recorder only.** A new Parquet
table of public Hyperliquid positions, polled from `clearinghouseState` for the addresses
the tape names, deployed to the recorder on the VPS so the snapshots start piling up. No
feature, no strategy wiring, no panel work, no config surface beyond one venue block. The
research question that grades this data is prompt D and cannot run until weeks of it exist,
which is why this step is small and starts now.

Print a short plan (files, seams, slice order) and wait for an OK before writing anything.

### Non-negotiables restated for this session

- Recorder only. Nothing under `strategy/`, `features/`, `sizing/`, `risk/`, `live/`.
- `TRADES` in `data/schemas.py` does not change. It is in `MULTI_VENUE_TABLES`, shared with
  Bybit, OKX and Binance rows that carry no addresses. The addresses go to an in-memory book
  and a JSON file, not to a column and not to a per-trade table.
- `hl_positions` is **not** added to `CONTINUOUS_STREAMS` or `PER_COIN_STREAMS`. It is a
  poll, sparse by nature, and only its connection is monitored, exactly like
  `coinalyze.xchg_ctx`. A declared-but-idle stream pages Telegram every ten minutes.
- Read-only public info calls to the mainnet `/info` endpoint, keyless, no account. This is
  the same host and the same kind of call the universe refresh already makes
  (`UniverseManager` on `venues.hyperliquid.info_base`). CLAUDE.md rule 8's "anything
  touching mainnet" is about orders and accounts; this is neither. Do not stop to ask.
- The paper soak at `/srv/liqfade/paper` is not touched by the deploy. `scripts/deploy.sh`
  restarts only `liqfade-recorder` and `liqfade-panel` by default and never the soak's
  units (see its comments and commit `1c18cd2`). Verify after deploying, do not assume.
- `make lint test` green before every commit. Conventional commits, one slice per commit.

### The seams (agreed here; do not re-ask)

Tests live at these public boundaries and nowhere inside them. One seam, one failing test,
one minimal implementation per cycle. Refactoring waits for the review.

1. `liqfade/data/schemas.py`: the `hl_positions` schema, and the two existing parity tests
   in `tests/test_schemas.py` that tie it to `docs/DATA_SOURCES.md`.
2. `liqfade/recorder/streams/hl.py::make_parser(on_users=...)`: a callback that receives
   both addresses of a trade, once per first-sighted trade.
3. `liqfade/recorder/addresses.py::AddressBook`: `note`, `top`, `save`, `load`.
4. `liqfade/recorder/streams/hl_positions.py::HlPositionsPoller.poll_once` against
   `httpx.MockTransport`, as `tests/test_recorder_coinalyze.py` does.
5. `liqfade/recorder/main.py::Recorder`: the poller is wired, fed the universe, reported in
   `feed_states`, and absent when disabled (mirror `tests/test_recorder_main.py`).
6. `liqfade/recorder/writer.py::_dedup_fields("hl_positions")` and compaction over a
   partition holding one duplicated poll.

### Decisions already made

**The table.** `hl_positions`, one row per open position per poll, exactly these columns in
this order:

    coin, ts_ms, rx_ms, source, address, szi, entry_px, liquidation_px,
    position_value_usd, margin_used_usd, leverage, leverage_type, max_leverage

`ts_ms` is the response's `time`; `rx_ms` is local receive; `source` is `recorder`. `szi`
is signed as Hyperliquid sends it (negative short). `liquidation_px` is **nullable and stays
null** when the venue sends null: a position with no liquidation level is a fact about the
account, and a zero would put a cluster at price zero. A position with `szi` of `0.0` is not
a row (`parse_clearinghouse_state` in `execution/hl.py` drops those for the same reason).
`leverage_type` is `cross` or `isolated` from `leverage.type`; `leverage` is
`leverage.value`. Only coins in the current universe are written. The table is single-venue
(not in `MULTI_VENUE_TABLES`), so compaction folds a day to `hyperliquid_day.parquet`.

Add it to `SCHEMAS`, to the schema list in `docs/DATA_SOURCES.md` in the exact `- \`table\`:
\`cols\`` format the parity tests parse, a row in the summary table ("HL positions and
liquidation prices (public, per address)"), and a paragraph under the Hyperliquid endpoint
section saying what `clearinghouseState` exposes for any address and at what weight.

**Dedup.** `_dedup_fields` today keys on `tid` alone and answers `()` for everything else.
Give `hl_positions` the key `("address", "coin", "ts_ms")`: two rows with the same key are
the same poll written twice (a re-flush), never two facts.

**The address book.** `AddressBook` in `liqfade/recorder/addresses.py`. `note(address,
notional_usd, ts_ms)` adds to a cumulative notional and stamps last-seen; `top(n)` is the
n addresses by cumulative notional; `save(path)` and `load(path)` are one JSON file written
the way `control/state.py::write_state` writes (temp sibling plus `os.replace`), and a
missing or unreadable file loads as empty and logs once. Bounded: keep at most
`max_tracked` (5000) addresses, pruning the tail on save. Cumulative and never decayed,
because a whale who opened a position a week ago and has not traded since is exactly who
the map is for; a `# ponytail:` comment names the ceiling (a permanently dormant address
holds its rank forever) and the upgrade (decay by last-seen). The file is
`<data_dir>/hl_addresses.json`, saved after every poll, loaded at recorder start.

**The harvest.** `make_parser(*, on_users=None)`. When set, it is called once per trade
that passes `_first_sight` (so the resubscribe replay does not count a trade twice) with
`(coin, notional_usd, ts_ms, buyer, seller)`, notional being `px * sz`. `build_streams`
gains the same optional `on_users` and passes it to the Hyperliquid stream only; the
`Recorder` passes `book.note` through a two-line adapter; `live/loop.py` passes nothing and
is unchanged. With no callback the parser's output is byte-identical to today, which the
existing parser tests already prove. The `HL_TRADES` payload in
`tests/test_recorder_parsers.py` already carries the `users` field.

**The poller.** `HlPositionsPoller(cfg, sink, book, info_base)` in
`liqfade/recorder/streams/hl_positions.py`, shaped like `CoinalyzePoller`: `stats` holds one
`StreamStats` named `hyperliquid.positions` only when active; `set_universe`;
`feed_states`; `poll_once(client)`; `run(stop)`. `poll_once` asks
`book.top(cfg.max_addresses)`, posts `{"type": "clearinghouseState", "user": address}` to
`{info_base}/info` **sequentially** (never gathered: a slow venue stretches the poll rather
than bursting the budget), sinks one row per open universe position, records one message
per poll however many rows it returned, and returns the row count. An address that fails
is logged, counted as a parse error on the stream, and skipped; a poll where every address
failed sets `connected` false. If a poll runs longer than `poll_s`, the next starts at once
rather than queueing. httpx timeout 10s. The address book is saved after every poll.

**Config.** A nested model on `HyperliquidVenue`:

    venues:
      hyperliquid:
        positions:
          enabled: true
          poll_s: 60         # seconds between polls of the tracked addresses
          max_addresses: 200 # addresses per poll, ranked by cumulative traded notional

with the budget in the comment: `clearinghouseState` weighs 2 against 1200 per minute per
IP (`docs/HYPERLIQUID.md`), so 200 addresses a minute is 400, one third of the budget, and
the universe refresh is the only other info traffic the recorder makes. The env files are
untouched: the recorder runs one env and reads the mainnet info base in all of them.

**Wiring.** In `Recorder.__init__`: the book (loaded from disk), the poller, and both added
to what `Health` and `feed_states` see; in `_apply_universe`: `set_universe`; in the `jobs`
list: `("hl_positions", ...)`. Reload does not reach the poller (Coinalyze's does not
either); a restart applies config.

**The probe.** Before the poller's test payload is written, capture a real response:
`scripts/probe/hl_positions.py`, public and keyless, posts `clearinghouseState` for one
address from the `HL_TRADES` fixture and prints the raw JSON. Embed one real response with
at least two positions (one cross, one with a null `liquidationPx` if you can find one)
verbatim in the poller test, as `ws_payloads.py` output is embedded in the parser tests.
Record the run in `docs/HYPERLIQUID.md` as CLAUDE.md section 6 asks.

**Disk.** At 200 addresses, five universe coins each and 1440 polls a day, expect about 1.4
million rows a day, of the order of the `trades` table. After the first full day on the
VPS, measure the compacted partition (`du -sh /srv/liqfade/data/parquet/hl_positions`).
Above 200 MB a day, the follow-up is a changed-only write (a row when `szi`, `entry_px`,
`liquidation_px` or `margin_used_usd` moved, plus a full snapshot at the top of each hour);
leave the `# ponytail:` comment naming that and do not build it now. The 80% disk alert
already exists.

**Docs.** `docs/DECISIONS.md` gets a dated entry (the design above, the budget arithmetic,
the row estimate). `docs/ROADMAP.md` Phase 7's candidate list gets "liquidation map from
public positions (recorder since <date>, graded by Q9)". Section 1 of
`docs/research/external_bots_scan.md` gets one line under "How to start": step one built,
date, branch, table name.

### Slices, in order

Each is red then green then commit. The completion criterion of each is the test that
failed now passing with `make lint test` green.

1. Schema and doc parity (seam 1).
2. `AddressBook` (seam 3): ranking, round trip, bounded, corrupt file.
3. Parser callback (seam 2): both addresses once per first-sighted trade; a replayed trade
   after resubscribe does not call it again; no callback means no change.
4. Poller `poll_once` (seam 4): one row per open universe position from the real payload;
   flat position skipped; null `liquidation_px` kept null; one message per poll; a failing
   address skipped and counted; only `top(max_addresses)` asked; calls arrive in sequence;
   disabled config declares no stream.
5. Wiring (seam 5) and dedup (seam 6).
6. Config block, docs, probe script committed.
7. Deploy and verify (below).

### Deploy and verify

After the branch is merged to `main` by the control chat (report first, then wait for the
merge notice), deploy from the laptop with the only ssh path that exists for this box,
the `liqfade-vps` alias (`docs/RUNBOOK.md`; the box bans after four failed tries, never
trial anything else against it):

    ssh liqfade-vps 'bash -s' < scripts/deploy.sh
    ssh liqfade-vps 'systemctl restart liqfade-recorder'

`deploy.sh` restarts only a unit whose file changed, so the second line is the restart, on
purpose. It deploys `main`, which by now also carries Panel v3 and the 05c testnet loop;
that is intended, `/srv/liqfade/app` should be on `main`. Then, in this order, and report each as seen rather than assumed:

1. `systemctl status liqfade-paper` is unchanged and its heartbeat file is fresh: the soak
   was not restarted.
2. `journalctl -u liqfade-recorder -n 200 -o cat | jq 'select(.event | startswith("hl_positions"))'`
   shows polls with row counts within two minutes of the restart.
3. `hyperliquid.positions` appears in `liqfade status` (or the panel) as connected.
4. After the first hourly flush, `ls /srv/liqfade/data/parquet/hl_positions/` shows the
   partition, and `<data_dir>/hl_addresses.json` exists and has grown past the fixture's
   two addresses.
5. `du -sh` of the partition after one hour, times 24, against the 200 MB line above.

If anything in the deploy needs a hand only the owner has (a key, the alias not resolving
from this machine), print the checklist and stop; never simulate having done it.

---

## Prompt B: the refusals the live loop throws away

ROUTING: NEW chat. Runs concurrently with prompt A. Branch `05d-refusals` off `main`, in its
own worktree: `git worktree add ../hl-robotrader-refusals -b 05d-refusals main`, then
`uv sync --all-extras` there. When done, send the final report (prompt name, branch, tip
hash) to the control chat `hl-robotrader-38` with `SendMessage`.

Since this prompt was written, 05c landed on `main`: `liqfade/live/venue.py` holds
`VenueSession`, the session that runs against a real account (testnet now, mainnet in Phase
6), beside `PaperSession`; `OrderStore` gained a migration step for new columns; and
`execution/policy.py` owns the entry policy. Read `liqfade/live/venue.py` with the files
below, and read the current code rather than the line references here.

/mattpocock-skills:tdd

You are working in the liqfade repo. Read `CLAUDE.md` in full, then
`docs/research/external_bots_scan.md` section 4, `docs/STRATEGY_AND_RISK.md` sections 3 and
6, and these files end to end: `liqfade/strategy/engine.py` (`Rejection`, `take_rejections`,
`_consider`, `_reject`, `_price`), `liqfade/backtest/engine.py` (`BacktestResult`,
`_place_entry`, `recent_blocks`), `liqfade/live/paper.py` (`PaperSession.tick`),
`liqfade/live/loop.py::report_risk_blocks`, `liqfade/execution/orders.py` (`OrderStore` and
its schema), `liqfade/backtest/costs.py::fee_for`, `liqfade/features/grid.py::effective_ts`,
and `liqfade/backtest/source.py` (how a replay reads one day at a time).

This session does two things that are one concern: **the live loop keeps every intent it
refused, with the prices it would have traded at, and a command says what those refusals
would have made or lost.**

The finding that starts it: `StrategyEngine.take_rejections()` is called by `run_backtest`
at the end of a replay and by nothing in `live/`. In a paper session `_rejections` grows for
the life of the process and is never written anywhere, and `PaperSession.result.rejections`
stays `[]`. The module docstring says the drain exists so a live loop does not grow a list
forever; nothing drains it. Sizing and risk refusals fare no better: `_place_entry` records
`(ts_ms, coin, gate, reason)` into `recent_blocks` and drops the intent, so the price that
was refused is gone by the time `report_risk_blocks` logs it.

Print a short plan (files, seams, slice order) and wait for an OK before writing anything.

### Non-negotiables restated for this session

- `strategy/` changes are additive and mypy-clean: `Rejection` gains fields, nothing else
  moves. The strategy stays deterministic and I/O free.
- Every number the scorer reports is net: taker fee both sides via `fee_for`, and the
  report says so in its header. CLAUDE.md rule 2.
- The scorer is a **bound, stated as one.** It fills as a taker at the first print after
  `ts_ms + backtest.latency_ms`; no queue, no book walk. The report's first paragraph says
  that the exact answer for a filter or rule is a second backtest with it switched off
  (`filters.*`, `strategy.rules.*`), which `liqfade lab` already runs, and that this
  command exists for the week that just happened rather than for the year.
- `report_risk_blocks` and its Telegram behaviour are unchanged; its tests still pass.
- No lookahead: prints are read in `effective_ts` order and a print before the fill time
  cannot fill. Same-second ambiguity resolves as the stop: when target and stop are both
  touched inside one 1s bucket (the grid convention), the stop is first.
- Memory: the scorer reads the tape only inside each refusal's window, never a whole day
  into memory. The laptop has 8 GB and the lake is on USB; `--data-dir` is explicit.
- `make lint test` green before every commit. Conventional commits.

### The seams (agreed here; do not re-ask)

1. `StrategyEngine.take_rejections()`: a rule or filter rejection carries `entry_px`,
   `target_px`, `stop_px`, `time_stop_s` and `urgency` equal to the intent that was killed.
2. `backtest/engine.py::_place_entry`: a sizing refusal and a risk refusal each land in
   `result.refused` as a `Rejection` with stage `sizing` or `risk` and the intent's prices
   (in-memory `RunInputs`, as `tests/test_backtest_engine.py` builds them).
3. `OrderStore.add_refusals(...)` and `OrderStore.refusals(start_ms, end_ms)` round trip
   through a temp sqlite file.
4. `PaperSession.tick` and `VenueSession.tick`: after a tick, `strategy._rejections` is
   empty and the refusals of that tick are in the store; a session with `store=None` drains
   and discards. One drain, shared by both sessions, not two copies.
5. `liqfade/backtest/shadow.py::score_refusal(refusal, prints, settings) -> Outcome`, pure,
   on hand-built prints: target hit; stop hit; both in one second resolves as stop; time
   stop; a print before `ts_ms + latency_ms` cannot fill.
6. `liqfade refusals` end to end over a temp lake written with `write_rows` (pattern:
   `tests/fixtures/golden_lake.py`) and a temp sqlite: the report exists and its per-reason
   totals equal the sum of seam 5's outcomes.

### Decisions already made

**`Rejection`.** Add `entry_px: float`, `target_px: float`, `stop_px: float`,
`time_stop_s: float`, `urgency: Urgency`. Non-optional: rules and filters run after
`_price`, so every rejection the engine records was priced. `_reject` takes the intent.
`stage` gains two values, `sizing` and `risk`, written by `_place_entry`.

**`result.refused`.** A second bounded deque on `BacktestResult`, `deque[Rejection]` with the
same `maxlen` as `recent_blocks`, appended beside each `recent_blocks.append` in
`_place_entry`. `recent_blocks` itself is unchanged: `report_risk_blocks` unpacks four
fields and is tested. In `run_backtest`, `result.rejections` keeps its meaning (rule and
filter rejections drained at the end); the report is not changed in this session.

**The table.** In `data/orders.sqlite`, created by `OrderStore` beside `orders` and `fills`:

    refusals(id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL, coin TEXT NOT NULL,
             side TEXT NOT NULL, stage TEXT NOT NULL, reason TEXT NOT NULL,
             entry_px REAL NOT NULL, target_px REAL NOT NULL, stop_px REAL NOT NULL,
             time_stop_s REAL NOT NULL, urgency TEXT NOT NULL, explain_json TEXT NOT NULL)

with an index on `ts_ms`. `explain_json` is `orjson` of the mapping (CLAUDE.md: orjson for
all JSON; `tests/test_conventions.py` enforces it). The table is created through the same
migration path 05c added for new columns, so an existing `orders.sqlite` on the box gains it
on the next start.

**The drain.** At the end of `PaperSession.tick`, after `_on_tick`: collect
`self.strategy.take_rejections()` plus everything in `self.result.refused` (then clear it),
and `await self.store.add_refusals(...)` when there is a store. One write per tick, which is
zero writes on almost every tick.

**The scorer.** `liqfade refusals --start YYYY-MM-DD --end YYYY-MM-DD [--data-dir] [--db
<orders.sqlite>] [--equity 10000] [--out reports/live/]`, writing
`reports/live/refusals_<start>_<end>.md` and `.parquet`. Per refusal: notional is what
sizing's first rule gives before any cap, `equity * sizing.risk_frac / |entry_px - stop_px|
* entry_px` (equity from `--equity`, defaulting to the paper equity, because the account's
equity at that millisecond is not persisted and this is a bound anyway); entry fills as a
taker at the first recorded print (`rx_ms > 0`) of that coin at or after
`ts_ms + backtest.latency_ms`; exit at the first of target touched, stop touched (the
`stop_triggered` rule in `backtest/fills.py`), or `time_stop_s` elapsed, at that print;
fees both sides via `fee_for(maker=False)`; funding ignored and the header says so (holds
are minutes). Output grouped by `(stage, reason)`: count, hit / stop / timeout counts, net
PnL sum, mean net bps, and one line per group in plain words ("`spread_too_wide` refused 14
entries that would have netted −$31: the filter earned its keep"). A refusal whose window
has no prints in the lake is reported as `no tape`, never scored as zero. Reads the tape
the way `backtest/source.py` does, a coin and a window at a time.

**Docs.** `docs/RUNBOOK.md` gets a new `## Weekly review` section (there is none today) holding this command; prompt C adds `liqfade drift` beside it. `docs/DECISIONS.md`
gets a dated entry with the leak finding and the design. Section 4 of
`docs/research/external_bots_scan.md` gets one line: built, date, branch.

### Slices, in order

1. `Rejection` fields (seam 1). 2. `result.refused` in `_place_entry` (seam 2). 3. The
store (seam 3). 4. The drain (seam 4). 5. `score_refusal` (seam 5), one tracer bullet per
outcome. 6. The CLI (seam 6). 7. Docs.

---

## Prompt C: live versus replay

ROUTING: NEW chat. Worktree: `git worktree add ../hl-robotrader-drift -b 05e-drift-check main`,
then `uv sync --all-extras`. Both gates below were met on 2026-09-08 (B merged at 256999b,
the entry policy in the live path since 05c). Two review-fix chats are live at the same time:
in `liqfade/live/loop.py` touch only the `paper_started` line (seam 5) and nothing else, and
stay out of `liqfade/live/venue.py`, `execution/` and `panel/`. Start only after **both** of
these are true: prompt B is merged to
`main`, and the entry policy (`execution.entry_ttl_s`, `execution.max_reprices`) is honoured
by the live path — the qualification in `docs/DECISIONS.md`, 2026-09-07 "05c: the seven day
paper acceptance run", has been lifted by a later entry. If either is false, say so and
stop. Branch `05e-drift-check` off `main`. When done, send the final report to the control
chat `hl-robotrader-38` with `SendMessage`.

/mattpocock-skills:tdd

You are working in the liqfade repo. Read `CLAUDE.md` in full, then `docs/ROADMAP.md`
Phase 5 (the DoD sentence about "a plausible band"), every 2026-09-07 entry in
`docs/DECISIONS.md` that mentions 05c, `docs/research/external_bots_scan.md` section 3, and
these files end to end: `liqfade/live/paper.py` (the module docstring is the claim this
session tests), `liqfade/live/loop.py`, `liqfade/backtest/engine.py` (docstring and
`run_backtest`), `liqfade/backtest/source.py` (`LakeSource`), `liqfade/backtest/report.py`
(`config_hash`, `run_id_for`), `liqfade/backtest/validate.py::bootstrap_mean_bps`,
`liqfade/backtest/portfolio.py::Trade`, `liqfade/execution/orders.py` (the `orders`, `fills`
and `refusals` tables), `liqfade/config/loader.py` (the layering docstring),
`scripts/systemd/liqfade-paper.service` (its `Environment=` lines), and the `sync` block
of `config/default.yaml`.

This session builds **`liqfade drift`: replay the days the paper loop actually traded
through the backtest engine, over the recorder's lake for the same days, under the same
resolved config and equity, and diff the two.** The Phase 5 DoD asks whether paper lands
in a plausible band of the backtest for the same days; today that is a sentence, and after
this session it is a command with a verdict per dimension.

`live/paper.py` says paper *is* the engine driven by a live clock. If that is true the
diff is small and the remaining drift has names: the online feature engine against the
vectorized grid on real tape (`tests/test_live_features.py` proves it on synthetic tape
only), wall-clock ticks against grid ticks, the live process's own sockets against the
recorder's (two connections, two sets of holes), and risk state carried across days. This
command is how each of those gets a number.

Print a short plan (files, seams, slice order) and wait for an OK before writing anything.

### Non-negotiables restated for this session

- Every dimension reports one of `MATCH`, `DRIFT`, `NOT_COMPARABLE`, with its threshold
  printed beside its number. `NOT_COMPARABLE` is for a field the live side genuinely does
  not persist; it is never quietly defaulted to zero and diffed. This is the one discipline
  the scan doc asked to be copied verbatim.
- The replay's settings are resolved the way the soak's are: `--env paper`, the same YAML
  layers including `config/overrides.yaml`, and the same `LIQFADE_` variables the unit sets
  (`liqfade-paper.service` sets `LIQFADE_CASCADE__TRIGGER=crossvenue`; there may be
  others, read the unit). `config_hash` of the replay is printed. The live run's hash is
  read from the `paper_started` log line if it carries one; today it does not, so this
  session adds `config_hash=` to that line (seam 5) and the current soak reports the config
  dimension as `NOT_COMPARABLE` with the reason.
- Memory and the laptop: the replay already streams one UTC day at a time; `--data-dir`
  is explicit; `pgrep -f liqfade` before launching anything, one job at a time.
- The live side comes from `liqfade sync` (it pulls `data/parquet/`, `data/orders.sqlite`,
  `data/state.json`, `reports/`). The soak writes `orders.sqlite` under `/srv/liqfade/data`
  (its unit's `ReadWritePaths` and `Settings.order_db_path`), so the synced copy is the
  input; confirm with `systemctl cat liqfade-paper` rather than assuming.
- `make lint test` green before every commit. Conventional commits.

### The seams (agreed here; do not re-ask)

1. `liqfade/backtest/drift.py::match_intents(live, replay, tol_ms)`, pure: matched pairs,
   live-only, replay-only, on hand-built lists keyed by `(coin, side, ts_ms)`.
2. `drift.compare(...) -> DriftReport`, pure: the verdict rules below on hand-built inputs;
   `NOT_COMPARABLE` when refusals are absent or the live hash is unknown.
3. `drift.read_live(db_path, start_ms, end_ms)`: entry attempts and round trips from
   `orders` + `fills`, refusals from `refusals`, out of a temp sqlite a `PaperSession`
   wrote (reuse the fixture pattern in `tests/test_live_loop.py`).
4. `liqfade drift` end to end: a `PaperSession` fed the golden lake's events
   (`tests/fixtures/golden_lake.py`) and `run_backtest` over the same lake must report
   `MATCH` on every comparable dimension; the same run with one perturbed setting on the
   replay side must report `DRIFT` on the dimension it perturbs. This is the tracer bullet
   and the real assertion of the session.
5. `paper_started` carries `config_hash`.

### Decisions already made

**Command.** `liqfade drift --start --end --equity 10000 [--coins] [--data-dir] [--db
<orders.sqlite>] [--live-config-hash <hex>] [--out reports/live/]`, writing
`reports/live/drift_<start>_<end>.md` and `.json`. `--coins` defaults to the core
universe and the report says the comparison is on those coins; the soak's universe is
dynamic and is not persisted per day, which is a `NOT_COMPARABLE` note on coverage, not a
guess.

**What an intent is on the live side.** An entry attempt is an `orders` row with `tag = "entry"`
plus every `refusals` row (prompt B). Intents dropped while paused
(`dropped_intents`) are counted in the log only and the report says so.

**Dimensions and verdicts.**

- *config*: hashes equal → `MATCH`; unequal → `DRIFT` and the report stops after printing
  both, because nothing below is comparable; unknown → `NOT_COMPARABLE`.
- *intents*: matched within `tol_ms = 2000` on `(coin, side)`; `MATCH` when ≥ 90% of live
  intents are matched and ≥ 90% of replay intents are matched; otherwise `DRIFT`, listing
  the unmatched with timestamps.
- *entries*: for matched intents that became orders on both sides, entry price difference
  in bps: `MATCH` when the median is ≤ 5 bps.
- *exits*: exit reason agreement on matched round trips ≥ 90%, and exit price median ≤ 5
  bps.
- *pnl*: `|Σ net live − Σ net replay|` within `max(n_trades × one taker round trip on the
  median notional, the width of bootstrap_mean_bps' interval on the replay trades)`.
- *refusals*: per `(stage, reason)` counts, live against the replay's `sizing_blocks`,
  `risk_blocks`, `strategy.stats.filters` and rule kills; `MATCH` when every reason's count
  is within 10% or ±2, whichever is larger. `NOT_COMPARABLE` when the live table is empty
  across the window. Day-one risk-gate refusals may differ because the replay starts from a
  clean `RiskState` and the soak carries its own; the report says so under the table.
- *tape holes*: any coin-day in the window where the lake shows a gap over
  `recorder.trades_gap_s` on `hyperliquid.trades` is listed, and intents inside a hole are
  excluded from the intents verdict and counted separately.

**Docs.** `docs/RUNBOOK.md` under `## Weekly review` (prompt B created it); `docs/DECISIONS.md` with the first real
run's verdicts (run it over the soak's window once it is built, on the synced data, and
record what it said, including every `NOT_COMPARABLE` and why); `docs/ROADMAP.md` Phase 5
DoD gets "(`liqfade drift`, see reports/live/)" appended to the band sentence. Section 3 of
`docs/research/external_bots_scan.md` gets one line: built, date, branch.

### Slices, in order

1. `match_intents`. 2. `compare` with the verdict rules, one tracer bullet per dimension.
3. `read_live`. 4. `config_hash` on `paper_started`. 5. The CLI and the golden-lake
end-to-end (MATCH, then perturb, then DRIFT). 6. Run it on the soak's synced window; docs.

---

## Prompt D: Q9, does the map measure anything

ROUTING: NEW chat. Start only when the data precondition below holds. Branch
`07b-q9-liqmap` off `main`. When done, send the final report (the DECISION lines verbatim)
to the control chat `hl-robotrader-38` with `SendMessage`.

/mattpocock-skills:tdd

You are working in the liqfade repo. Read `CLAUDE.md` in full, then
`reports/research/PREREGISTRATION.md` end to end (ground rules, the DECISION format, the
stop rules), `reports/research/SUMMARY.md`, `reports/research/q3_proxy.md`,
`docs/research/external_bots_scan.md` section 1, `docs/STRATEGY_AND_RISK.md` section 7, and
these files: `scripts/research/_lib.py` (`build_episodes`, `attach_outcomes`,
`cluster_bootstrap`, `write_report`, `decision`, `scan_day`), `scripts/research/q3_proxy.py`
(`triggers_of`, `score`, the bars), `scripts/research/q2_exhaustion.py`,
`tests/fixtures/research_lake.py` and `tests/test_research_scripts.py`.

This session answers **Q9, preregistered: does the liquidation map recorded since prompt A
measure Hyperliquid's own forced flow, and does fuel remaining at cascade start separate
the cascades that revert from the ones that keep going?** Research only, Phase 3 style: a
preregistration committed before the script reads real data, one script, one report, three
DECISION lines, nothing built in `strategy/` or `features/`.

Print a short plan and wait for an OK before writing anything.

### Data precondition (stop and say so if it fails; no workaround)

- At least 21 complete UTC days of `hl_positions` at 60s cadence or better, synced to the
  local lake with `liqfade sync`. Count them with a duckdb query over the partitions and
  print the count and the median poll interval per day.
- `fills_full` and `liquidations` with `source = hl_s3` for **the same days**, pulled with
  `liqfade backfill s3-fills --start --end`. About 1 GB a day at $0.09/GB, so 21 days is
  about $2; the job prints its estimate and refuses over $5 without `--yes`, and this is
  under it. These exact liquidations are the validation set; without them Q9a cannot run.
- The laptop has 8 GB and the lake is on USB: every scan is one day at a time through
  `_lib.scan_day`, peak RSS under 2 GB, `--data-dir` explicit, `pgrep -f liqfade` before
  launching, one job at a time.

### Preregistration, committed first as `reports/research/PREREGISTRATION_Q9.md`

Transcribe the definitions and bars below before any script reads real data. Any later
change is a dated amendment in `docs/DECISIONS.md` stating what had already been seen.

**Q9a, measurement.** Per coin and per poll interval `[t0, t1)`: `crossed_usd` is the sum
of `position_value_usd` over positions in the `t0` snapshot whose `liquidation_px` lies
between the mark at `t0` and the extreme of the mark over `[t0, t1)` on the side that
liquidates (long positions below, short above), and which are absent or smaller at `t1`.
Mark comes from `asset_ctx`. Compare against the exact hl_s3 `liquidations` notional in
the same interval, same coin: (i) Spearman correlation over intervals with any activity;
(ii) coverage, `Σ crossed / Σ exact`; (iii) **the Q3 number**: `robust_z` the `crossed_usd`
series as Q3 z-scored the proxy, `triggers_of` at the same thresholds (2, 3, 4, 6), `score`
against qualifying episodes with the same 120s match window, on core+major. DECISION yes
when precision ≥ 0.6 and recall ≥ 0.4 at some threshold (Q3's own bar); no when precision
< 0.5 at every threshold (Q3's park bar); inconclusive between, or under 40 qualifying
episodes in the window.

**Q9b, exhaustion.** For each qualifying episode from `build_episodes` with a snapshot whose
`rx_ms` is inside the 120s before the cascade start (no snapshot, no episode: absence is
not zero fuel): `fuel_usd` is the sum of `position_value_usd` of same-side positions whose
`liquidation_px` lies beyond the mid at start by up to `2 × rv_5m × mid` in the cascade
direction. Split episodes into terciles by `fuel_usd`. Outcomes via `attach_outcomes`:
net-stress reversion at 5, 15 and 30 minutes, and the stop-out rate in the config's
`(k_stop, k_target)` cell from Q8. Preregistered direction: the low-fuel tercile reverts
better. DECISION yes when low minus high at 15 minutes net-stress has a cluster-bootstrap
interval excluding zero in the preregistered direction **and** the stop-out rate
difference has the same sign; no when the interval excludes zero the other way;
inconclusive otherwise or under 40 episodes per tercile.

**Q9c, cost of cadence** (descriptive, no DECISION): the distribution of poll intervals
actually achieved, and the share of exact liquidation notional that fell inside intervals
longer than 90s. This is the number that says whether the "cadence" risk in the scan doc
bit.

**Stop rules.** Q9a no and Q9b no: the poller is deleted in a follow-up prompt and the
scan doc records why. Q9a yes: the next prompt is an HL-native trigger candidate, Phase 4
style, grilled first. Q9b yes and Q9a no: the next prompt is an exhaustion feature only.

### The seams (agreed; do not re-ask)

1. `scripts/research/q9_liqmap.py::crossed_notional(snapshot_t0, snapshot_t1, marks)`, pure,
   on hand-built frames: a long whose liquidation price the mark path crossed and which is
   gone at `t1` counts; one the path did not reach does not; one crossed but still present
   at `t1` does not; a null `liquidation_px` never counts.
2. `fuel_at(episode_start, snapshot, rv, mid)`, pure: same-side positions inside the band
   count, the wrong side and outside the band do not; a snapshot with `rx_ms ≥ start` is
   refused (the no-lookahead property test: mutate rows after `t`, output unchanged).
3. `tests/test_research_scripts.py`: the script runs end to end over `research_lake`,
   which gains an `hl_positions` table in `tests/fixtures/research_lake.py` (a handful of
   synthetic addresses whose liquidation prices the injected cascades cross), and every
   DECISION reads `inconclusive` because four hours cannot clear a 21-day bar.

### Deliverables

`scripts/research/q9_liqmap.py` with the run line in its docstring;
`reports/research/q9_liqmap.md` with the three-column net tables, the three DECISION lines,
episode and cluster counts, and the bootstrap intervals; a Q9 row in
`reports/research/SUMMARY.md`; a dated `docs/DECISIONS.md` entry; and one line under "How
to start" in section 1 of `docs/research/external_bots_scan.md`: step two run, date, the
three verdicts.

---

## Prompt E: the stop-order map

ROUTING: NEW chat. Start only if Q9's report says yes on Q9a or Q9b; otherwise this prompt
is never run. Branch `07c-trigger-map` off `main`. When done, send the final report to the
control chat `hl-robotrader-38` with `SendMessage`.

/mattpocock-skills:tdd

You are working in the liqfade repo. Read `CLAUDE.md` in full, then
`docs/research/external_bots_scan.md` section 2, the rate limits section of
`docs/HYPERLIQUID.md`, `liqfade/recorder/streams/hl_positions.py` and its tests (prompt A's
work, which this extends), and `liqfade/recorder/addresses.py`.

This session adds **one more table from the same address book: `hl_triggers`, the resting
stop and take-profit trigger orders of the tracked wallets, from `frontendOpenOrders`.**
Recorder only, same shape as the positions poller, same seams. Stops are forced flow that is
not a liquidation, and they cluster where `features/levels.py` already draws zones.

Print a short plan and wait for an OK before writing anything.

### Decisions already made

- **Budget first.** `frontendOpenOrders` weighs 20, ten times `clearinghouseState`
  (<https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits.md>,
  read 2026-09-08). Defaults `poll_s: 60`, `max_addresses: 20` → 400 weight a minute, which
  with the positions poller's 400 is two thirds of the IP budget. The config comment
  carries that arithmetic. The two pollers share the address book and take the top of the
  same ranking.
- **The table.** `hl_triggers`: `coin, ts_ms, rx_ms, source, address, oid, side,
  order_type, trigger_px, trigger_condition, sz, reduce_only, is_position_tpsl`. Only rows
  with `isTrigger` true; plain resting limits are the book and are already recorded.
  `ts_ms` is the order's `timestamp`. Dedup key `("address", "oid", "ts_ms")`. Documented
  in `docs/DATA_SOURCES.md` in the parity-tested format, and a paragraph in
  `docs/HYPERLIQUID.md` on what `frontendOpenOrders` exposes about other wallets.
- **Probe first.** `scripts/probe/hl_triggers.py`, public and keyless, prints one real
  response; embed it in the poller test.
- **Same seams as prompt A's 4, 5 and 6**, plus one: a non-trigger order in the payload
  produces no row.
- Deploy and verify exactly as prompt A did, including that the soak was not restarted,
  and add the first hour's partition size to the report.
