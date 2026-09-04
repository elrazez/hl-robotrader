# CLAUDE.md

Project: **liqfade** (working name). An autonomous crypto perps trading bot on Hyperliquid that fades liquidation cascades on scalp to short-intraday horizons, with Telegram alerts and a hard kill switch.

Read this file fully before doing anything. Then read the reference doc for the area you are touching:

- `docs/DATA_SOURCES.md` before any data ingestion, recorder or backtest-data work
- `docs/HYPERLIQUID.md` before any execution, order, account or rate-limit work
- `docs/STRATEGY_AND_RISK.md` before any signal, sizing, risk or backtest-logic work
- `docs/ROADMAP.md` for the phase you are in and its definition of done

## 1. Owner context and decisions already made

- The project is driven through Claude Code. Explain non-obvious design choices in one or two sentences in commit messages or PR notes. Do not over-explain basics.
- Language: **Python 3.12+**. No Rust, no Go, no Node for the core. JS only if a small dashboard is ever needed.
- Venue: **Hyperliquid perps only** (mainnet for money, testnet for integration tests).
- Thesis: **fade liquidation cascades** (mean reversion after forced flow exhausts). Momentum and filter-only variants are out of scope for v0.
- Horizon: scalp to short intraday. Typical hold 1 to 60 minutes. Never overnight without an explicit config flag.
- Universe: high market-cap, liquid HL perps, selected dynamically by volume and open interest. Not hard-limited to BTC/ETH.
- Execution mode: fully autonomous, Telegram alerts on every state change, kill switch from Telegram and from a local file flag.
- Capital: starts small and scales. **Never hardcode capital.** All sizing is a function of live equity, per-coin liquidity and configured risk fractions. Leverage is an output of sizing, not an input.
- Budget: **under $50/month** for data plus hosting. This rules out Coinglass, Tardis subscriptions and paid HL streams. See `docs/DATA_SOURCES.md` for what we use instead.
- Infra: greenfield. Owner will create accounts, VPS, keys. Claude writes code and setup scripts; it never enters credentials.
- Hosting target: one small Linux VPS in **AWS Tokyo (ap-northeast-1) or a Tokyo datacenter** because Hyperliquid validators and API live there.
- Two runtimes. **Local machine** (owner's laptop/desktop): research notebooks and full backtests over Parquet synced down from the VPS with `rsync`. **VPS**: recorder, live loop, watchdog, web panel. Every CLI command must work on both. Nothing in `backtest/`, `features/`, `strategy/` may assume the VPS, a running recorder, or network access.
- **Web panel is required**, not optional: monitoring, order and fill history, charts, latency (WS ping RTT, REST RTT, submit-to-ack ms), recorder health, backtest report viewer, and safe runtime edits of sizing, risk limits and trigger thresholds. It binds to `127.0.0.1` on the VPS and is reached through an SSH tunnel (`ssh -L 8787:127.0.0.1:8787`), which itself runs over Tailscale/WireGuard. Tailscale alone does not reach it: nothing listens on the tailnet address. Never exposed publicly, never without auth.
- **Security and performance posture:** hardened Ubuntu 24.04 (key-only SSH on a non-default port, dedicated unprivileged `liqfade` user, ufw default deny, fail2ban, unattended security upgrades, chrony), hardened systemd units (`NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`, read-only code dir, writable data dir only), secrets in a `0600` env file readable only by the service user. Python hot paths use `uvloop` and `orjson`; no unnecessary daemons on the box. The trading process never runs the HTTP server; the panel is a separate process.

## 2. Non-negotiable rules

1. **No lookahead.** Every feature at decision time t may only use data with receive timestamp < t. Cross-exchange feeds carry local receive time; use that, not exchange time, for signal availability.
2. **Fees and latency are in every backtest number.** Never report gross PnL. Default costs: 0.045% taker, 0.015% maker per side on notional, funding accrual, plus a 1.0s signal-to-fill latency model. If a result only works without fees, it is not a result.
3. **Paper before testnet before mainnet.** No code path may place a mainnet order unless `LIQFADE_ENV=mainnet` AND `LIQFADE_LIVE_TRADING=1` AND the kill-switch file is absent. Default config must be paper mode.
4. **Every order is reduce-only-aware, has a client order id (cloid), and is tracked.** Fire-and-forget orders are forbidden. Reconciliation against `clearinghouseState` and `openOrders` runs on startup, on every WebSocket reconnect and every 60s.
5. **Kill switch wins.** `/kill` on Telegram, or presence of `data/KILL`, must within 5s: cancel all open orders, close all positions with IOC orders, stop the strategy loop, and post a confirmation. This path has its own tests and must not depend on the strategy module.
6. **Secrets never touch the repo.** `.env` is gitignored. Private keys are read once from env by `execution/signer.py` and never logged, printed, or passed around as strings beyond that module.
7. **Small, testable changes.** One concern per PR. Every module ships with tests. Do not refactor unrelated code while doing a task.
8. **Ask before destructive or costly actions:** deleting recorded data, S3 pulls above $5 estimated egress, changing risk limits, anything touching mainnet.
9. **Tell the owner what they must do manually** (account creation, funding, key generation, VPS) as an explicit checklist at the end of any task that needs it. Never simulate having done it.

## 3. Architecture

Single Python package, three long-running processes, one shared Parquet data lake on local disk.

```
liqfade/
  recorder/      24/7 market data recorder (HL + Binance + Bybit + OKX + Coinalyze) -> Parquet
  data/          schemas, loaders, historical ingestion (HL S3, Tardis free days)
  features/      pure functions: liquidation intensity, cascade detection, exhaustion, vol, CVD, OI delta
  strategy/      signal -> intent (side, coin, target, stop, urgency). No I/O. Deterministic. Backtest and live share this code.
  sizing/        intent -> notional, given equity, liquidity caps and risk config
  risk/          pre-trade checks, exposure caps, daily loss, drawdown halt, cooldowns, stale-data guard
  execution/     Hyperliquid client wrapper, order manager, cloid registry, reconciliation, signer
  backtest/      event-driven replay engine, fill model, cost model, metrics, walk-forward
  live/          the trading loop: wires recorder feeds -> features -> strategy -> sizing -> risk -> execution
  alerts/        Telegram bot: outbound alerts, inbound commands (/status /pause /resume /flat /kill)
  control/       local control plane: Unix-socket JSON-RPC server inside the live/recorder processes, typed
                 commands (status, pause, resume, flat, kill, reload_config, get_metrics), state snapshot
                 writer (data/state.json every 1s). Telegram and the panel are both clients of this.
  panel/         FastAPI backend (separate process) + React/Vite/TS frontend served as static files.
                 Reads: control socket, data/orders.sqlite, Parquet via duckdb, reports/. Writes: only
                 config/overrides.yaml through validated endpoints, then sends reload_config.
  config/        pydantic-settings models, YAML defaults, env overrides, overrides.yaml (panel-managed)
  cli.py         `liqfade record|backfill|backtest|paper|testnet|live|panel|status|sync`
tests/           mirrors package layout; `tests/integration/` needs testnet keys and is skipped by default
notebooks/       research only; never imported by package code
docs/            the reference docs listed above plus decision log
scripts/         VPS bootstrap, systemd units, cron for parquet compaction
data/            gitignored. parquet/{source}/{stream}/date=YYYY-MM-DD/*.parquet, plus KILL flag file
```

Data flow (live): WebSocket feeds -> in-memory ring buffers (per coin, per stream) -> feature engine ticks every 250ms -> strategy emits intents -> sizing -> risk gate -> order manager -> HL. The recorder writes the same feeds to Parquet so live and backtest see identical schemas.

Strategy code must be runnable against a replay of Parquet with zero changes. This is the single most important design constraint. If you find yourself adding a live-only branch inside `strategy/` or `features/`, stop and move it to `live/`.

Framework decision: we build a lightweight custom engine on top of the official `hyperliquid-python-sdk` rather than adopting NautilusTrader, Hummingbot or freqtrade. Reasons: the edge lives in custom cross-venue liquidation features that those frameworks do not model natively, Nautilus is mid-migration from v1 to v2 in 2026, and a custom engine keeps the codebase small enough for the owner to reason about. Keep `strategy/` framework-agnostic so a port to Nautilus stays possible if execution quality ever becomes the bottleneck.

## 4. Tech stack and conventions

- Package manager: `uv`. Lockfile committed. `uv run` for everything.
- Core libs: `hyperliquid-python-sdk` (official, sync; wrap in threads or use for signing and REST only), `websockets` for all streams, `uvloop` as the event loop, `orjson` for all JSON, `polars` for data, `duckdb` for ad-hoc queries over Parquet, `pydantic` v2 and `pydantic-settings` for config, `structlog` JSON logging, `httpx` for REST, `python-telegram-bot` or raw Bot API via httpx, `pyarrow` for Parquet writes, `numpy`, `aiosqlite` for the order store. Avoid pandas in package code; notebooks may use it.
- Panel backend: `fastapi` + `uvicorn` (separate process, `127.0.0.1:8787`), session auth with a single bcrypt-hashed password from env plus the network boundary (Tailscale/SSH). Panel frontend: Vite + React + TypeScript, `lightweight-charts` for price/liquidation/trade-marker charts, `uplot` for equity and latency series, Tailwind. Built bundle is committed under `panel/static/` so the VPS never needs Node.
- Config edits from the panel: every change is validated by the same pydantic model as the YAML, written to `config/overrides.yaml` with a timestamp and reason, applied by the live process at the next safe point (never mid-order), logged with before/after, and posted to Telegram. Risk-limit fields require the owner to type the field name as confirmation. Nothing in the panel can enable live trading; that stays an env flag on the box.
- Async everywhere in recorder, live and alerts. Strategy and features are pure sync functions on numpy/polars inputs.
- Time: integer milliseconds UTC everywhere internally. Name fields `ts_ms` (exchange time) and `rx_ms` (local receive time). Never use naive datetimes.
- Money: `Decimal` at the execution boundary (prices, sizes sent to HL), floats in features and backtest.
- Prices and sizes sent to HL must be rounded per asset (`szDecimals` and max 5 significant figures for price). Rounding lives in one place: `execution/rounding.py`.
- Config: `config/default.yaml` is the base, `config/{paper,testnet,mainnet}.yaml` override, env vars override both. Every risk parameter has a comment with its unit and rationale.
- Logging: structured, one JSON line per event, `event=` key always present. Log every order intent, submission, ack, fill, cancel, reject, reconnect, risk block, and kill.
- Tests: `pytest`, `pytest-asyncio`. Unit tests must run offline in under 30s. Fixtures under `tests/fixtures/` hold small real Parquet samples (a few minutes of data) captured by the recorder.
- Lint and types: `ruff` (format and lint), `mypy` on `strategy/`, `sizing/`, `risk/`, `execution/`. Type errors in those packages block merge.
- Style: small modules, explicit names, docstrings on public functions stating units and edge cases. No clever metaprogramming.
- Git: `main` is always runnable in paper mode. Feature branches. Conventional commit messages (`feat:`, `fix:`, `data:`, `research:`).

Commands (target state; create the Makefile in Phase 0):

```
make setup        # uv sync, pre-commit install
make test         # unit tests
make test-int     # integration tests against HL testnet (needs .env)
make lint         # ruff + mypy
make record       # start recorder locally
make backtest CFG=config/backtest/v0.yaml
make paper        # live feeds, simulated fills
make panel        # start the web panel (127.0.0.1:8787)
make status       # print positions, orders, risk state, recorder lag, latency
make sync         # rsync data/parquet, data/orders.sqlite and reports/ from the VPS to local
```

## 5. What "good" looks like for each layer

- Recorder: zero gaps longer than 5s per stream per day, automatic reconnect with backoff, per-stream lag metric, hourly Parquet flush, daily compaction, disk usage alert at 80%.
- Features: each feature is a pure function with a docstring stating window, units and the data it consumes. Property tests for invariants (non-negative intensities, z-scores finite, no future data).
- Strategy: deterministic given inputs. Emits `Intent` objects. Has a `explain()` that returns the feature values that triggered the intent, which is what Telegram posts.
- Sizing: never exceeds any of: risk-per-trade budget, per-coin liquidity cap, per-coin exposure cap, total exposure cap, HL max leverage for that asset. Returns 0 with a reason when blocked.
- Risk: stateless checks plus a small persisted state (daily PnL, peak equity, consecutive losses, cooldowns) saved to `data/risk_state.json` every change so restarts do not reset limits.
- Execution: idempotent submit by cloid, retries with jitter on transient errors, never retries on rejects, handles partial fills, reconciles on reconnect, respects rate limits (see `docs/HYPERLIQUID.md`).
- Backtest: same strategy code as live, realistic fill model for maker and taker, every metric net of fees and funding, walk-forward split, produces a one-page markdown report per run under `reports/`.
- Alerts: every intent, fill, exit, risk block, halt, reconnect and error goes to Telegram. Commands: `/status`, `/pause`, `/resume`, `/flat`, `/kill`, `/risk`. Inbound commands are authenticated by chat id allowlist.
- Control plane: one typed command set used by Telegram, the panel and the CLI, so there is exactly one implementation of `pause`, `flat`, `kill`, `reload_config`. Unix socket with `0600` perms owned by the service user. A `state.json` snapshot (positions, orders, risk state, feed lags, latency percentiles, universe) is written every second and is the only thing readers touch; readers never block the trading loop.
- Panel: loads in under a second on the VPS, shows recorder lag per stream, live positions and orders, fills history with filters, PnL and equity curves, per-coin charts with liquidation and trade markers, latency series (WS ping RTT, REST RTT, submit-to-ack p50/p95), backtest reports, and a settings page with diff preview and confirmation. Read-only when the live process is down. Every settings write is auditable.

## 6. Working with Claude Code on this repo

- Start each task by restating the phase (`docs/ROADMAP.md`) and the definition of done. Propose a short plan (files to touch, tests to add). Then implement.
- When a task needs data you do not have (a key, a funded account, a recorded sample), stop and produce the owner checklist rather than mocking around it.
- Prefer reading `docs/*.md` over web searching. If you must consult external docs, note the URL and date in `docs/DECISIONS.md`.
- After any change to `strategy/`, `sizing/`, `risk/` or `execution/`, run `make test` and `make lint` before declaring done.
- Never run `liqfade live` yourself. Never modify `config/mainnet.yaml` risk limits without an explicit instruction in the same message.
- When unsure about a Hyperliquid API detail, write a tiny script under `scripts/probe/` that hits testnet and prints the raw response, then encode what you learned in `docs/HYPERLIQUID.md`.
- Keep this file under control: if a section grows beyond a screen, move detail into `docs/` and leave a pointer.
- The `mattpocock-skills` plugin is installed. Use its skills whenever one fits, and when handing the owner a prompt for a new chat, put the skill invocation at the top of it: `/mattpocock-skills:code-review <commit>` at every phase boundary and after any batch of agent commits; `/grill-me` before a phase's design is pasted; `/mattpocock-skills:wizard` for anything only the owner can do by hand (VPS, keys, dashboards); `/mattpocock-skills:diagnosing-bugs` for a hard bug or a performance regression; `/mattpocock-skills:handoff` when a chat must continue elsewhere. Do not reach for the ticket, triage or architecture-scan skills mid-phase; they invite refactors CLAUDE.md forbids.

## 7. Owner's manual tasks (Phase 0 checklist)

Owner does these; Claude prepares scripts and instructions.

1. GitHub repo (private), push the scaffold.
2. VPS in Tokyo: 2 vCPU, 4 GB RAM, 80 GB disk, Ubuntu 24.04. Vultr Tokyo or AWS Lightsail Tokyo both fit the budget (roughly $12 to $24/month). Add your SSH public key at creation; the bootstrap script disables password login.
2b. Tailscale (free tier) on both the VPS and your local machine, so the panel and rsync go over a private network and the VPS exposes nothing but SSH. WireGuard by hand is the alternative.
2c. Local machine: install `uv` and `git`. Backtests and research run here; expect 20 to 60 GB of Parquet over time, so pick a disk with room.
3. Hyperliquid mainnet account via `app.hyperliquid.xyz`, fund with USDC on Arbitrum. Generate an **API wallet** (agent key) at `app.hyperliquid.xyz/API`. Note: the bot signs with the agent key but `account_address` is the main wallet address.
4. Hyperliquid **testnet** account at `app.hyperliquid-testnet.xyz`, claim faucet USDC, generate a separate testnet agent key.
5. Telegram bot via BotFather, get bot token, get your chat id.
6. Coinalyze account and API key (free).
7. AWS account with an IAM user that has `s3:GetObject` and `s3:ListBucket` on `hyperliquid-archive` and `hl-mainnet-node-data`, for requester-pays historical pulls. Set a billing alarm at $10.
8. Fill `.env` from `.env.example` on the VPS only. Never paste keys into chat.
