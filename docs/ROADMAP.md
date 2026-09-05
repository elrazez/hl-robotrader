# Roadmap

Each phase gets one or more Claude Code prompts. A phase is done only when its definition of done (DoD) passes and the owner has confirmed. Phases 1 and 2 can run in parallel with 3 once the recorder is live. Do not start Phase 6 until Phase 5 has run for at least a week without a reconciliation mismatch.

## Phase 0: Scaffold and environment (1 day)

Build: repo layout from `CLAUDE.md`, `pyproject.toml` with `uv`, ruff and mypy config, `Makefile`, `config/` models and YAML defaults with every parameter from `docs/STRATEGY_AND_RISK.md` plus `overrides.yaml` layering, `structlog` setup with `orjson`, `uvloop` bootstrap, `control/` protocol (typed commands, Unix-socket server and client, `state.json` writer) with a no-op backend so the panel and Telegram can be built against it before the live loop exists, `liqfade sync` (rsync wrapper), CI workflow running `make lint test`, `scripts/vps_bootstrap.sh` (Ubuntu 24.04 hardening: non-root service user, key-only SSH on a non-default port, ufw, fail2ban, unattended-upgrades, chrony, Tailscale install, uv, hardened systemd units for recorder, live, watchdog and panel, logrotate, disk alert cron), `docs/RUNBOOK.md` skeleton, `panel/` skeleton (FastAPI app with auth, `/api/health`, `/api/state` reading `state.json`, Vite scaffold with one page that renders the state).

DoD: `make setup lint test` passes on a clean clone on the local machine with zero tests skipped; `liqfade --help` lists all subcommands; config loads for paper, testnet and mainnet with mainnet refusing to enable live trading unless both env flags are set; `liqfade panel` serves the skeleton on `127.0.0.1:8787` behind the password; `control` round-trips a `status` command over the socket in a test; `scripts/vps_bootstrap.sh` has been read by the owner and runs idempotently on a fresh VPS (owner verifies once the VPS exists; not blocking for Phase 1 development locally).

Owner: GitHub repo, VPS, Telegram bot, Coinalyze key, HL testnet account. Mainnet account and AWS can wait until Phase 2 and 5.

## Phase 1: Recorder (2 to 3 days, then runs forever)

Build: `recorder/` with one async task per venue stream, reconnect with jittered backoff, ring buffers, hourly Parquet flush into the canonical schemas, nightly compaction, universe refresh from `metaAndAssetCtxs`, Coinalyze poller, lag and gap metrics, Telegram alert on any stream silent for more than 60s or disk over 80%. `liqfade record` and `liqfade status`.

Also: **Panel v1** (recorder page): per-stream lag, gaps, message rates, disk usage, universe list, WS ping RTT and REST RTT to Hyperliquid as time series, last 24h of liquidations per venue as a chart. Recorder registers with the control plane so `/status` and the panel share one source.

DoD: 48 hours on the VPS with no gap over 5s on HL trades and BBO for the core coins, Bybit liquidations flowing, Binance and OKX liquidations flowing, Parquet files readable with `duckdb` and matching the schema doc, `tests/fixtures/` populated with a 5-minute real sample used by unit tests, `make sync` pulls the lake to the local machine and a local `duckdb` query over it works, Panel v1 shows all of the above over Tailscale.

## Phase 2: Historical ingestion (2 days plus pull time)

Build: `data/ingest/hl_s3.py` (requester-pays, cost estimate first, resumable, writes canonical `liquidations` and `trades`), `data/ingest/hl_archive_l2.py` for hourly L2 snapshots of the core coins, `data/ingest/tardis_free.py` for first-of-month liquidation and trade CSVs across Binance, Bybit, OKX, HL, `data/ingest/hl_candles.py` via `candleSnapshot` for 1m candles back as far as the API allows (which turned out to be about 3 days, so the historical `candles_1m` table is instead derived from `trades` by `liqfade derive candles`). Symbol mapping, dedup by `(block_time, coin, tid)`.

DoD: 6 to 12 months of HL exact liquidations and trades for the core universe on disk, egress spend logged (a full year came to about $25, see `docs/DATA_SOURCES.md`), every backfilled day compacted to one file per table, `candles_1m` derived and validated against the API on the overlap days, a `data quality` report showing counts per day with no unexplained holes, Tardis days aligned to the same schema.

**Done 2026-09-05.** 2025-09-01 to 2026-08-30 in the lake: 2.85bn fills, 1.79bn trades, 5.77M liquidations, 3.43bn L2 rows, 174.7M derived bars, 116.9M asset contexts, about $25 of egress. Every partition is one file. `reports/data_quality/counts.md` is the report and every gap in it is explained in `docs/DECISIONS.md`, 2026-09-05: the `l2` and `asset_ctx` days the archive does not hold, the Tardis first-of-month sampling, and one real defect (173,868 duplicated Bybit ETH rows on 2026-01-01, fix known, not yet applied). The side rule holds in all twelve months. Read the L2 cadence note in `docs/DATA_SOURCES.md` before choosing the Phase 4 walk-forward split.

Owner: AWS IAM user and billing alarm.

## Phase 3: Research (1 to 2 weeks, iterative)

Build: `features/` as pure functions with tests, notebooks answering the six research questions in `docs/STRATEGY_AND_RISK.md` section 7, proxy calibration report, first parameter proposals written back into `config/default.yaml` with rationale.

DoD: each research question has a written yes/no decision with sample sizes; the thesis either survives net of fees on HL-exact cascades or the owner decides to pivot; `hl_forced_proxy` precision/recall is documented.

This is the phase where the project can legitimately stop. If the edge is not there, say so.

## Phase 4: Backtester and strategy v0 (1 week)

Build: `backtest/` engine per spec, `strategy/` and `sizing/` and `risk/` per spec, `liqfade backtest`, report generator, walk-forward and bootstrap validation, parameter surface plots. All of this runs on the local machine over synced Parquet; the VPS is not involved. **Panel v2**: backtest report browser (runs list, metrics table, equity curve, trade list with per-trade chart replay showing the cascade, entry, exit and liquidation markers), runnable locally with `liqfade panel --local`.

DoD: backtest of v0 over all available data with a held-out last 25%; report shows net-of-cost metrics, PnL concentration, and stability surfaces; a deliberately lagged version (latency 3s) still shows positive expectancy or the strategy is marked latency-fragile; unit tests cover fill model edge cases (partial fills, stale book, stop gap-through).

## Phase 5: Paper trading and testnet execution (1 to 2 weeks running)

Build: `live/` loop in paper mode (real feeds, simulated fills using the backtest fill model), `alerts/` Telegram bot with all commands, `execution/` against testnet, order manager state machine with SQLite persistence, reconciliation, watchdog unit, kill switch path with its own tests, the six testnet probes listed in `docs/HYPERLIQUID.md`. **Panel v3**: live positions and orders, fills history, PnL and equity, submit-to-ack latency percentiles, pause/resume/flat buttons (kill stays Telegram and file only, on purpose), and the settings page: sizing fractions, risk limits, trigger thresholds, universe overrides, with diff preview, typed confirmation for risk fields, audit log, and Telegram notification on every change.

DoD: 7 days of paper trading with every intent and simulated fill posted to Telegram and logged; paper results within a plausible band of backtest expectations for the same days (document the comparison); testnet orders round-trip correctly including TP/SL grouping, reconnects and reconciliation; `/kill` verified to flatten within 5s from a state with resting orders and an open testnet position; watchdog verified by killing the loop process.

## Phase 6: Mainnet, small (ongoing)

Build: `config/mainnet.yaml` with conservative limits (risk_frac 0.25%, max 2 positions, daily loss 2%), deployment runbook `docs/RUNBOOK.md`, daily summary report to Telegram, weekly comparison of live fills versus the backtest fill model (maker fill rate, slippage, latency distribution) written to `reports/live/`.

DoD: two weeks live at minimum size with zero reconciliation mismatches and zero unhandled exceptions; measured latency and fill-rate numbers fed back into the backtest config; owner decides on scaling.

Scaling rule of thumb: increase `risk_frac` or deposit only after a full week where live PnL, fill rate and slippage are within the backtest's expected band. Never scale after a good day.

## Phase 7: Iterate

Candidates, each as its own research question first: cross-venue liquidation weights, funding and OI regime filters, book-imbalance timing, coin-tier specific parameters, trailing exits, HL paid liquidation feed if budget grows, partial-liquidation rhythm exploitation, momentum variant for market-wide sweeps.
