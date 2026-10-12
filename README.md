# liqfade

An autonomous trading bot for [Hyperliquid](https://hyperliquid.xyz) perpetual futures that
**fades liquidation cascades**: when forced selling (or buying) pushes price too far, it
waits for the forced flow to run out and bets on price snapping partway back, holding for 1
to 60 minutes.

It's a complete, tested system: a 24/7 market data recorder, historical ingestion from
Hyperliquid's S3 archives, a research toolkit, an event-driven backtester, paper and testnet
trading loops, a Telegram bot with a kill switch, a watchdog and a web panel. It was built
in Python 3.12 between August and October 2026, almost entirely with
[Claude Code](https://claude.com/claude-code).

> [!WARNING]
> **This is a research project, not a money printer, and it has never traded real money.**
> The edge it was built to capture is real in historical data, but nobody has yet shown it
> can be captured live with free public data. Read [What we found](#what-we-found) before
> you spend a dollar on it. Nothing here is financial advice, and the software comes with
> no warranty (see [LICENSE](LICENSE)).

## Contents

- [What we found](#what-we-found)
- [How it works](#how-it-works)
- [Quick start](#quick-start)
- [The command line](#the-command-line)
- [Data](#data)
- [Research and backtesting](#research-and-backtesting)
- [Paper, testnet and live trading](#paper-testnet-and-live-trading)
- [Running it on a server](#running-it-on-a-server)
- [Configuration](#configuration)
- [Repository layout](#repository-layout)
- [Documentation map](#documentation-map)
- [How this was built](#how-this-was-built)
- [Contributing](#contributing)
- [License](#license)

## What we found

The short version: **the bounce after a cascade is real, but the profit lives in the first
30 to 60 seconds after the bottom, and timing the bottom needs Hyperliquid's own
liquidation stream, which no free live feed provides.**

**The edge exists on exact data.** Over a year of Hyperliquid history (2025-09-01 to
2026-08-30: 2,722 cascades in 1,758 independent clusters, 42 coins), price reverts after a
cascade by **+84.6 bps at 15 minutes** net of a stressed cost model, 95% CI [+25, +180].
About +30 bps of that survives once the handful of market-wide crash days are taken out. A
volatility-scaled stop beyond the extreme and a target at 80% of the retrace keep +80.6 of
the +84.6. Each question was preregistered before any script read the data:
[`reports/research/SUMMARY.md`](reports/research/SUMMARY.md).

| # | question | answer |
|---|---|---|
| Q1 | does price revert after an exact cascade? | **yes**, +84.6 bps at 15 min |
| Q2 | does an exhaustion rule beat a fixed delay? | no |
| Q3 | can public data stand in for exact liquidations? | **no**, precision 0.01 at recall 0.68 |
| Q4 | do Bybit-only cascades revert on Hyperliquid? | inconclusive, 12 days of data |
| Q5 | do partial-liquidation chunks leave a rhythm? | no |
| Q6 | do filters remove the losing tail? | no |
| Q7 | do supply/demand zones help? | no, worse inside them |
| Q8 | stop and target from the price path | yes, with a stop |

**Realistic entries lose money.** Those numbers enter one second after the true extreme,
which you only know afterwards. Every backtest with entries a live bot can actually take
lost money, even on the exact (best case) trigger: v0 -11.6% before its drawdown halt, v1
-6.0%. Most entries landed before the bottom and the rest of the cascade ran them into
their stops.

**The entry that works needs data a bot can't see live.** A replay of the 2,722 cascades
found that entering 5 to 10 seconds after the cascade's *last liquidation* earns about +132
bps per trade at maker costs (+38% a year at 0.5% risk per trade in simulation, 11 of 12
months positive). But Hyperliquid's public feeds don't flag liquidations, and the S3
archive that does delivers them about a day late. See
[`docs/research/hl_realtime_liquidations.md`](docs/research/hl_realtime_liquidations.md).
The only complete live source found is running your own non-validating Hyperliquid node
(about $100 a month in hardware). That simulation hasn't been confirmed by a full-year
backtest yet; [`config/backtest/v2a_flow_exhaustion.yaml`](config/backtest/v2a_flow_exhaustion.yaml)
and its two siblings are the configs written to test it.

**A month of live recording** (2026-09-05 to 2026-10-12, Tokyo) is documented in
[`docs/RECORDED_DATA.md`](docs/RECORDED_DATA.md). Binance's liquidation stream is blocked
from Tokyo, and OKX publishes contracts rather than dollars, so the cross-venue substitute
for Hyperliquid's own liquidations was Bybit alone. A 4.5-day paper run on it placed no
trades. The recording itself wasn't kept.

If you want to pick this up, the open questions are:

1. Does the flow-timed entry (the v2 configs) survive a full-year backtest on the exact
   trigger? If even that best case loses after fees, stop there.
2. If it wins, is a self-hosted Hyperliquid node worth it to see liquidations live?
3. Does the public positions map (`hl_positions`: the liquidation prices of addresses seen
   trading, polled from `clearinghouseState`) predict where cascades run? That's Q9,
   specified in [`prompts/05d_external_ideas.md`](prompts/05d_external_ideas.md) and never
   run. It needs 21 days of fresh recording.
4. Does a liquidation ladder projected from swing points measure anything? That's Q10:
   preregistered in [`reports/research/PREREGISTRATION_Q10.md`](reports/research/PREREGISTRATION_Q10.md),
   with the script built and tested ([`scripts/research/q10_ladder.py`](scripts/research/q10_ladder.py))
   but not yet run over the year.

## How it works

```
 WebSocket feeds ──► ring buffers ──► features (every 250 ms) ──► strategy ──► sizing ──► risk ──► orders
 (HL, Bybit, OKX)                     pure functions             Intent      notional    gates     Hyperliquid
        │
        └──► recorder ──► Parquet lake ──► backtester (the same features and strategy code)
```

- **One strategy, two runtimes.** `features/`, `strategy/`, `sizing/` and `risk/` are pure
  functions with no I/O, so the backtester and the live loop run exactly the same code.
- **No lookahead.** Every row carries `ts_ms` (exchange time) and `rx_ms` (local receive
  time). A decision at time t only sees data received before t.
- **Costs are always in.** Every backtest number includes 0.045% taker / 0.015% maker fees
  per side, funding, and a 1 s signal-to-fill latency. No gross PnL anywhere.
- **Three processes:** the recorder, the trading loop and the web panel. They share one
  Parquet lake and talk over a local Unix-socket control plane.
- **Safety first.** Every order has a client order id and is tracked. Positions and orders
  are reconciled against the exchange on startup, on every reconnect and every 60 s. A kill
  switch (Telegram `/kill` or a `data/KILL` file) cancels everything and closes every
  position within 5 seconds. A watchdog writes the kill flag if a loop stops breathing.

The thesis, every feature, sizing and risk rule are specified in
[`docs/STRATEGY_AND_RISK.md`](docs/STRATEGY_AND_RISK.md).

## Quick start

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/). Nothing below needs an account,
a key or money.

```bash
git clone https://github.com/elrazez/hl-robotrader.git
cd hl-robotrader
make setup      # uv sync --all-extras, pre-commit hooks
make test       # ~1,750 unit tests, offline, about 35 s
make lint       # ruff and mypy
```

Look around:

```bash
uv run liqfade --help
uv run liqfade config show --env paper      # every setting and where it came from
uv run python scripts/probe/hl_meta.py      # top Hyperliquid perps right now
uv run liqfade record --minutes 5 --data-dir /tmp/lake   # record five minutes of live data
```

Open the web panel (it binds to 127.0.0.1 only):

```bash
uv run liqfade panel hash-password          # type a password, get a hash
export LIQFADE_PANEL_PASSWORD_HASH='<the hash>'
uv run liqfade panel                        # http://127.0.0.1:8787
```

## The command line

`uv run liqfade --help` lists everything. The main commands:

| command | what it does |
|---|---|
| `record` | run the 24/7 market data recorder (Hyperliquid, Bybit, OKX, public positions) |
| `backfill` | pull history into the lake: Hyperliquid fills and L2 from S3, asset contexts, Tardis free days |
| `derive` | rebuild a table from another one, offline (e.g. 1-minute candles from trades) |
| `compact` | fold a range of days into one file per table |
| `backtest` | replay the strategy over the lake and write a report to `reports/` |
| `lab` | a terminal workbench for configs, runs and results ([`docs/LAB.md`](docs/LAB.md)) |
| `watch` | live progress of a running backtest |
| `paper` | the live loop on real feeds with simulated fills |
| `testnet` | the live loop against Hyperliquid testnet |
| `live` | the live loop against mainnet, behind three locks (below) |
| `refusals` | price what the live loop refused to trade, over the tape that followed |
| `drift` | compare a paper run against a backtest replay of the same days |
| `status` | positions, orders, risk state, feed lag and latency |
| `panel` | the web panel |
| `config` | the resolved configuration |
| `sync` | rsync a server's lake and reports to this machine (read the caveat in [`docs/RUNBOOK.md`](docs/RUNBOOK.md)) |

## Data

Everything lands in one Parquet lake:
`<data_dir>/parquet/{table}/date=YYYY-MM-DD/{venue}_{hour}.parquet`, compacted nightly to one
file per table per day. Tables: `trades`, `liquidations`, `bbo`, `l2`, `asset_ctx`,
`candles_1m`, `hl_positions`, `fills_full`, and Coinalyze's `xchg_ctx`. Schemas, sources and
every quirk measured on the wire are in [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md).

| source | what | cost |
|---|---|---|
| Hyperliquid WebSocket and Info API | trades, BBO, L2, candles, asset contexts, public positions | free |
| `s3://hl-mainnet-node-data` | every fill, with exact liquidations | requester pays, about $0.09/GB; a year was about $25 |
| `s3://hyperliquid-archive` | L2 snapshots, asset contexts | requester pays |
| Bybit, OKX public WebSockets | cross-venue liquidations (Bybit complete, OKX throttled) | free, live only |
| Tardis.dev free days | first-of-month samples, all venues | free |
| Coinalyze | cross-venue open interest and funding | free key |

The S3 jobs print an egress estimate first and refuse anything over $5 without `--yes`.
Nobody sells complete cross-venue liquidation history cheaply, which is why the recorder
exists: whatever you don't record yourself is gone.

## Research and backtesting

- **Research** lives in [`scripts/research/`](scripts/research/), one script per question,
  each reading the lake one day at a time so it fits in 8 GB of memory. The
  preregistration, the reports and the go/no-go are in [`reports/research/`](reports/research/).
- **Backtests:** `uv run liqfade backtest --config config/backtest/v0.yaml --start 2025-09-01
  --end 2026-08-30 --coins BTC,ETH,SOL,HYPE --equity 10000 --data-dir <lake>`. Add
  `--lagged-ms 3000` to test latency fragility. Each run writes a one-page report, a summary
  and a trade list under `reports/<run id>/`.
- **The engine** is event-driven with a maker/taker fill model, partial fills, stale-book
  penalties, stop gap-through, funding accrual, a per-cluster exposure cap (a market-wide
  sweep is one bet, not ten), walk-forward splits and bootstrap checks.
- **`liqfade lab`** is a terminal UI to edit configs, launch runs and compare results.

## Paper, testnet and live trading

All three run the same loop (`liqfade/live/`) with a different execution backend.

- **Paper** (the default environment): real feeds, simulated fills from the backtest's fill
  model, Telegram alerts and the kill switch. No keys needed.
- **Testnet:** real orders against Hyperliquid testnet with an API wallet.
- **Mainnet** places real orders only when **all three** hold: `LIQFADE_ENV=mainnet`,
  `LIQFADE_LIVE_TRADING=1`, and no `data/KILL` file. The config also refuses the
  backtest-only `exact` trigger on mainnet. Nothing in the panel can enable live trading.

The signing key is an Hyperliquid **API wallet** that can trade but not withdraw. It's read
once from the environment by `execution/signer.py` and never logged. Telegram commands
(`/status /pause /resume /flat /kill /risk`) are accepted only from an allowlisted chat id.
What to do when something halts is in [`docs/RUNBOOK.md`](docs/RUNBOOK.md); every
Hyperliquid API detail the code relies on, measured against the venue, is in
[`docs/HYPERLIQUID.md`](docs/HYPERLIQUID.md).

## Running it on a server

[`docs/DEPLOY_VPS.md`](docs/DEPLOY_VPS.md) walks through a hardened Ubuntu 24.04 box
(Hyperliquid's API is in Tokyo): an unprivileged service user, key-only SSH on a
non-default port, a firewall, fail2ban, unattended upgrades, sandboxed systemd units and a
`0600` secrets file. `scripts/vps_bootstrap.sh` and `scripts/deploy.sh` automate it and
refuse to run unless `LIQFADE_OWNER_RUN=1` is set. Read them first: they rewrite SSH and
firewall config. The panel binds to loopback only and is reached through an SSH tunnel.

## Configuration

Settings load in this order, later wins: `config/default.yaml` → `config/{paper,testnet,mainnet}.yaml`
→ `--config <file>` → `config/overrides.yaml` (written by the panel) → environment
variables (`LIQFADE_SIZING__RISK_FRAC=0.004`). Every risk parameter carries a comment with
its unit and rationale. `uv run liqfade config show` prints the result and the source of
each value. Secrets go in `.env`; [`.env.example`](.env.example) lists every variable.

## Repository layout

| path | contents |
|---|---|
| `liqfade/recorder/` | the 24/7 recorder: one async task per venue stream, reconnects, hourly flush, nightly compaction |
| `liqfade/data/` | schemas, loaders, historical ingestion (S3, Tardis), derivations |
| `liqfade/features/` | pure feature functions: liquidation intensity, cascade detection, exhaustion, volatility, levels, the projected liquidation map |
| `liqfade/strategy/` | signal to `Intent`, deterministic, with `explain()` |
| `liqfade/sizing/`, `liqfade/risk/` | notional from equity and liquidity caps; pre-trade gates, halts, cooldowns, persisted risk state |
| `liqfade/execution/` | Hyperliquid client, order manager, cloid registry, reconciliation, signer, watchdog |
| `liqfade/backtest/` | the replay engine, fill and cost models, metrics, reports, drift |
| `liqfade/live/`, `liqfade/alerts/`, `liqfade/control/` | the trading loop, Telegram, the control plane |
| `liqfade/panel/` | FastAPI backend plus a React/Vite frontend, prebuilt into `panel/static/` |
| `liqfade/lab/` | the terminal workbench |
| `config/` | YAML defaults per environment and the backtest configs |
| `scripts/` | server setup and deploy, systemd units, testnet probes, research scripts |
| `tests/` | the unit suite, with small real-data fixtures; `tests/integration/` needs testnet keys |
| `reports/` | research results and backtest reports |
| `docs/`, `prompts/`, `CLAUDE.md` | reference docs, and how the project was built (below) |

## Documentation map

| doc | read it for |
|---|---|
| [`docs/STRATEGY_AND_RISK.md`](docs/STRATEGY_AND_RISK.md) | the thesis, features, sizing and risk rules, research questions |
| [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md) | every data source, schema and measured wire quirk |
| [`docs/HYPERLIQUID.md`](docs/HYPERLIQUID.md) | the exchange API as the code uses it, rate limits, rounding, rejects |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | the phases, their definitions of done, and where each stopped |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | the decision log: every non-obvious choice, measurement and reversal |
| [`docs/RECORDED_DATA.md`](docs/RECORDED_DATA.md) | what a month of live recording held and showed |
| [`docs/LAB.md`](docs/LAB.md) | the terminal workbench |
| [`docs/DEPLOY_VPS.md`](docs/DEPLOY_VPS.md), [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | setting up a server, and operating one |
| [`docs/research/`](docs/research/) | the real-time liquidation feed survey and a scan of public liquidation bots |

## How this was built

Almost every line here was written by Claude Code, steered through phase prompts. Those are
included because they're the honest record of the process and may help anyone building the
same way:

- [`CLAUDE.md`](CLAUDE.md) is the standing brief the agent read in every session: the
  decisions, the non-negotiable rules (no lookahead, fees in every number, paper before
  testnet before mainnet, the kill switch wins) and the conventions.
- [`prompts/`](prompts/) holds the prompt for each phase, in order.
- [`docs/DECISIONS.md`](docs/DECISIONS.md) records what each session found, including the
  bugs, the wrong turns and the numbers that didn't move.

## Contributing

Issues and pull requests are welcome. Keep changes small and tested: `make lint test` must
pass, and anything touching `strategy/`, `sizing/`, `risk/` or `execution/` needs tests and
must keep mypy clean. Never commit keys; the pre-commit hooks run gitleaks.

## License

[MIT](LICENSE). Trading perpetual futures can lose more than you put in. This software is
provided as is, without warranty of any kind, and nothing in it is financial advice.
