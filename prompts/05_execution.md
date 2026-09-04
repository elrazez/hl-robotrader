# Prompt 05: Phase 5 paper trading, testnet execution, Telegram, Panel v3

Owner notes: the biggest phase. Needs your testnet account and agent key in `.env`, and
the Telegram bot token and chat ids. Runs paper mode for 7 days and testnet probes
before anything else is allowed. Nothing here touches mainnet; the env flags make that
impossible without your explicit action.

---

You are working in the liqfade repo. Read `CLAUDE.md` in full, then `docs/HYPERLIQUID.md`
end to end (accounts, order semantics, rate limits, error strings, the six probes) and
`docs/STRATEGY_AND_RISK.md` section 5, and `docs/ROADMAP.md` Phase 5.

This session is **Phase 5: execution/, live/, alerts/, the real control backend, the
kill switch path, the watchdog, the testnet probes, and Panel v3**. The strategy stack
from Phase 4 is used unmodified; if anything in strategy/, features/, sizing/ or risk/
needs changing to go live, that is a design smell: fix it by moving the live-only
concern into live/, and note it in docs/DECISIONS.md.

Work in three commits-gated stages. Do not start a stage before the previous one's
tests pass: (A) execution primitives, (B) paper loop + alerts + control, (C) testnet +
panel v3.

## Non-negotiables restated for this phase

- Paper before testnet before mainnet. Nothing in this prompt may place a mainnet
  order; the env gate (`LIQFADE_ENV=mainnet` AND `LIQFADE_LIVE_TRADING=1` AND no
  `data/KILL`) is already enforced in config and is checked again in the order client.
- Every order carries a cloid, is reduce-only-aware, and is tracked in sqlite.
- Rejects are never retried. Transient transport errors retry max 3 with jitter.
- The kill path must not import the strategy module and has its own tests.
- The agent private key is read from env exactly once, inside `execution/signer.py`,
  and never appears in logs, exceptions, state, or any other module's namespace.

## Decisions already made

**Carried in from the 2026-09-04 code review (do these in Stage A, they are one commit each)**
- Split `venues.hyperliquid.api_url` into `info_url` and `exchange_url`, or derive the
  exchange base from `env` alone. Today one key is documented as the base for both, it
  defaults to mainnet, and `paper.yaml` does not override it, so an execution client that
  reads it in paper env would point at the mainnet exchange. Nothing hits `/exchange`
  yet, which is the only reason this is not already a bug.
- `universe.min_listing_days` is configured, documented and read by nothing. The recorder
  deliberately does not apply it (`metaAndAssetCtxs` carries no listing date); `risk/` is
  where the floor belongs, so consume it here or delete the key.
- `/api/config` returns `flatten(resolve(...))`, the raw pre-validation merge, so an
  unknown `LIQFADE_X__Y` env var present at request time is echoed to the browser instead
  of being rejected by `extra="forbid"`. Dump the validated `Settings` instead. Low risk
  today (the endpoint is behind auth) but the Phase 5 settings page writes through this
  surface.

**Stage A: execution/**
- `signer.py`: reads `HL_{TESTNET|MAINNET}_AGENT_PRIVATE_KEY` and
  `HL_{...}_ACCOUNT_ADDRESS` per env at construction, builds the SDK signing objects,
  exposes only `sign_*` methods and the account address. `__repr__` redacts. A test
  asserts the key string is not reachable via any public attribute.
- `client.py`: `HLClient` async facade. Exchange actions (order, cancel, cancelByCloid,
  modify, updateLeverage) go through the sync SDK `Exchange` inside
  `asyncio.to_thread`, one lock around nonce assignment (one signer per process, per
  docs). Info calls via httpx async directly. User streams (`orderUpdates`,
  `userFills`) via our own `websockets` connection with the same backoff base class
  the recorder uses; resubscribe sends snapshots (`isSnapshot: true`) which the order
  manager treats as reconciliation input, not duplicates.
- "Market" orders are Ioc limits priced at touch plus/minus
  `execution.taker_slippage_bps`. The SDK's default 5% slippage is never used.
- `orders.py`: `OrderManager`. State machine per cloid exactly
  `NEW -> SUBMITTED -> ACKED -> (PARTIAL)* -> FILLED | CANCELED | REJECTED`, persisted
  to `data/orders.sqlite` via aiosqlite on EVERY transition (schema: cloid pk, oid,
  coin, side, px, sz, filled, tif, reduce_only, status, intent_json, ts columns,
  raw_last_event). Cloid is uuid4 hex (16 bytes) generated here. Idempotent submit:
  a cloid that already exists is never re-sent. Entry policy per docs: Alo at touch,
  TTL `entry_ttl_s`, reprice max `max_reprices` times (modify when size unchanged,
  else cancel+place), then give up, or Ioc fallback only when intent.urgency is high
  AND expected edge > 3x taker fee. On entry fill: place stop (`trigger` sl,
  isMarket, reduceOnly) and target (Gtc reduce-only) with `grouping: "positionTpsl"`.
  Time stop enforced by the loop with Ioc reduce-only. Unknown order status strings
  raise and alert.
- `reconcile.py`: on startup, on every WS reconnect, and every `reconcile_s`:
  `clearinghouseState` positions vs portfolio, `openOrders` vs registry. Unknown
  exchange order -> cancel it. Unknown position -> HALT + alert, never adopt. Local
  order the exchange lacks -> mark CANCELED with reason and alert. Result logged as
  `event=reconcile` with counts; any mismatch also increments a counter shown in
  status.
- `ratelimit.py`: token budget from `action_budget_per_hour`, decremented per action
  (batch counts n), surfaced in `RiskState.actions_remaining`; at zero, entries halt
  with reason `rate_limit`. Parse the documented "Too many cumulative requests" error
  string into that same halt.
- `errors.py`: the known error strings from docs/HYPERLIQUID.md mapped to typed
  exceptions with a `retryable: bool`.
- `watchdog.py`: standalone entry (`python -m liqfade.execution.watchdog`, matches the
  systemd unit). Loop: read `data/heartbeat` mtime; if older than
  `risk.watchdog_stale_s`, use its OWN HLClient (own nonce space is acceptable
  because the live process is by definition dead or wedged) to cancel all open
  orders, write `data/KILL`, send alert. It imports execution and config only, never
  strategy or live.

**Stage B: live/ + alerts/ + control**
- `live/feeds.py`: reuse the recorder's stream classes to feed in-process ring
  buffers (deque per coin per stream with maxlen from config). The live process does
  NOT write parquet; the recorder process does that separately.
- `live/loop.py`: every `decision_cadence_ms`: build features from buffers ->
  strategy -> sizing -> risk -> order manager. Touches `data/heartbeat` every
  `heartbeat_s`. Watches for `data/KILL` every tick.
- `live/paper.py`: paper fill engine reusing `backtest/fills.py` against the live
  book buffers, applying the same latency model. Paper fills flow through the same
  OrderManager persistence with `env=paper` so the whole pipeline is exercised.
- `live/backend.py`: `LiveBackend(ControlBackend)`: `status` from real state,
  `pause` blocks entries, `resume` clears resolvable halts only, `flat` closes via
  Ioc reduce-only (optionally one coin), `kill` = cancel all + flatten + write
  data/KILL + stop loop + alert, `reload_config` re-resolves and applies at the next
  tick boundary never mid-order, `get_metrics` real. Runs ControlServer +
  StateWriter, `proc` = "paper" | "testnet" | "live" per env.
- Kill deadline: from command received to all cancels and closes SENT must be under
  `risk.kill_deadline_s`; instrumented with a log event carrying the elapsed ms.
- `alerts/bot.py`: raw Bot API via httpx (no python-telegram-bot; one less
  dependency; note in DECISIONS.md). Long-poll `getUpdates` in its own task inside
  the live process. Inbound: `/status /pause /resume /flat [coin] /kill /risk`,
  authenticated by `TELEGRAM_ALLOWED_CHAT_IDS`; unknown chat ids get logged and
  ignored, never answered. Commands dispatch through the local ControlClient over the
  socket, the same path the panel uses, so there is exactly one implementation.
  Outbound: every intent (with explain()), fill, exit, risk block, halt, reconnect,
  reconcile mismatch, and error goes through the Phase 1 `send_alert` with the 10
  minute dedupe DISABLED for fills and intents (dedupe only operational warnings).
- CLI: `paper` and `testnet` subcommands run the loop with the respective env forced;
  `live` runs it only when the config gate passes and otherwise exits 1 printing the
  block reason. The `testnet` command refuses to start if the testnet keys are absent,
  printing the owner checklist.

**Stage C: probes + Panel v3**
- Probes: `scripts/probe/p1_roundtrip.py` ... `p6_userfees.py`, the six from
  docs/HYPERLIQUID.md, each self-contained against testnet, printing raw responses
  and writing `reports/probes/pN_<slug>.md`. After running them (you may, they are
  testnet), update docs/HYPERLIQUID.md where reality disagreed and note it in
  DECISIONS.md. If testnet keys are absent in this session, write the probes, do not
  run them, and put them at the top of the owner checklist.
- Panel v3 backend: positions/orders/fills endpoints reading `data/orders.sqlite`
  and state.json; `POST /api/control/{pause|resume|flat}` proxying through
  ControlClient (kill is deliberately NOT in the panel; Telegram and the file only);
  settings endpoints: `GET /api/settings` (whitelisted keys: sizing.*, risk.*,
  cascade.*, exhaustion.*, intent.*, filters.*, universe.exclude), `POST
  /api/settings/preview` (returns a diff against the resolved config),
  `POST /api/settings/apply` requiring, for any risk.* key, the exact field name
  typed back in a `confirm` field. Apply validates through the full pydantic model,
  writes `config/overrides.yaml` with timestamp and reason, appends an audit line to
  `data/config_audit.jsonl`, calls reload_config over the socket, and sends a
  Telegram alert with the before/after. Nothing in the panel can touch env, the
  live-trading flags, or the kill file.
- Panel v3 frontend: Positions, Orders, Fills (filterable), a latency section now
  fed by real submit-to-ack percentiles, Pause/Resume/Flat buttons with a confirm
  dialog, and the Settings page with diff preview and typed confirmation. Rebuild and
  commit the bundle.

## Deliverables and tests

All tests offline against a `FakeHLClient` (records calls, scripted responses,
scripted user-stream events). No test may hit the network.

1. Stage A files + tests: state machine transitions including partials and the
   snapshot-on-reconnect path; idempotent submit; reprice policy including the
   modify-vs-cancel rule; TP/SL grouping placed after entry fill; reject never
   retried, transient retried thrice; reconcile all four mismatch cases; rate budget
   depletion halts; signer redaction; watchdog fires on stale heartbeat and writes
   KILL (heartbeat as a tmp file, fake client asserts cancel-all was called).
2. Stage B files + tests: a full paper tick cycle on synthetic buffers produces an
   intent, a paper fill, sqlite rows and state.json updates; kill from a state with
   two resting orders and one open position cancels and flattens within the deadline
   on the fake clock and does not import strategy (assert `"liqfade.strategy" not in
   sys.modules` in a subprocess test); pause blocks entries but exits still manage;
   resume refuses unresolvable halts; reload applies only at tick boundary; Telegram
   command auth rejects unknown chat ids; every command round-trips through the real
   socket.
3. Stage C files + tests: settings whitelist enforced (a non-whitelisted key 400s);
   risk field without typed confirmation 400s; apply writes overrides.yaml that the
   loader then resolves (round trip test); audit line appended; panel control
   endpoints proxy correctly; kill absent from panel routes.
4. Bundle rebuilt and committed.

## How to work

- Print a plan per stage and wait for OK once, at the start.
- `make lint test` green before every commit; mypy strict now fully applies to
  execution/.
- Never run `liqfade live`. Running `liqfade paper` briefly and the probes against
  testnet is allowed and encouraged if keys exist.
- No em dashes. No new Python dependencies.

## Definition of done

`make lint test` green, zero skips. Owner-run acceptance per the roadmap: 7 days of
paper with every intent and fill on Telegram; paper results within a plausible band of
the backtest over the same days (write `scripts/research/paper_vs_backtest.py` that
produces that comparison table); all six probes run with results written back;
testnet round trip including TP/SL grouping and reconnect reconciliation; `/kill`
verified live on testnet from a state with resting orders and an open position, under
5 seconds; watchdog verified by kill -9 on the loop. End with that checklist plus the
exact commands, and what only the owner can do (keys, chat ids, the 7 day wait).
