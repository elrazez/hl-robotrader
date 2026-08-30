# liqfade

A trading bot for Hyperliquid perpetual futures. It waits for a liquidation cascade
(forced selling or buying that pushes price too far), waits for that forced flow to run
out, then bets on price bouncing partway back. Holds for 1 to 60 minutes.

Full design: [CLAUDE.md](CLAUDE.md). Build order: [docs/ROADMAP.md](docs/ROADMAP.md).

## Where things stand

Phase 0 is done: the skeleton, config, logging, control plane, command line tool, web
panel and server setup scripts. **Nothing trades yet, and nothing backtests yet.** The
folders for the trading logic exist but are empty on purpose.

| Command | Works today? |
|---|---|
| `liqfade config show` | yes |
| `liqfade status` | yes |
| `liqfade panel` | yes |
| `liqfade sync` | yes |
| `liqfade record` | no, Phase 1 |
| `liqfade backfill` | no, Phase 2 |
| `liqfade backtest` | no, Phase 4 |
| `liqfade paper` / `testnet` / `live` | no, Phase 5 and 6 |

The unfinished ones print which phase adds them and exit with code 2.

## What each folder is

**Things that hold logic**

| Folder | What lives there | Built in |
|---|---|---|
| `liqfade/recorder/` | Listens to Hyperliquid, Binance, Bybit and OKX around the clock and saves everything to disk | Phase 1 |
| `liqfade/data/` | The shape of the saved files, plus the jobs that download old data | Phase 2 |
| `liqfade/features/` | Math on market data: how big is this cascade, how fast is price moving, is the forced selling slowing down | Phase 3 |
| `liqfade/strategy/` | Reads the math, decides whether to trade and at what price, target and stop | Phase 4 |
| `liqfade/sizing/` | Decides how much to buy or sell, from account size and how much the coin can absorb | Phase 4 |
| `liqfade/risk/` | The brakes: daily loss limit, drawdown halt, cooldowns, stale data checks, kill switch | Phase 4 |
| `liqfade/execution/` | Talks to Hyperliquid: places orders, tracks them, fixes mismatches, holds the signing key | Phase 5 |
| `liqfade/backtest/` | Replays saved market data through the strategy and scores it, after fees | Phase 4 |
| `liqfade/live/` | The loop that wires all of the above together and runs it | Phase 5 |
| `liqfade/alerts/` | Telegram messages out, `/status` `/pause` `/kill` commands in | Phase 5 |

`strategy/`, `features/`, `sizing/` and `risk/` never touch the network. That is what
lets the backtester and the live bot run the exact same code.

**Things that already work**

| Folder or file | What it does |
|---|---|
| `liqfade/config/` | Reads the settings files and checks every value is sane |
| `liqfade/control/` | A private socket inside the running bot. Telegram, the panel and the command line all send it the same commands, so `pause` means one thing, not three |
| `liqfade/panel/` | The web dashboard. Python backend, React page, already built into `panel/static/` |
| `liqfade/cli.py` | The `liqfade` command |
| `liqfade/log.py` | Writes one line of JSON per event, and blanks out anything that looks like a key |
| `liqfade/runtime.py` | Starts the fast event loop and shuts down cleanly on Ctrl-C |
| `config/*.yaml` | Every setting, with a comment saying its unit and why that number |
| `scripts/` | Server setup, deploy, systemd service files, and a connectivity probe |
| `tests/` | 104 tests, all run offline in under 10 seconds |
| `docs/` | The reference docs. Read the one for whatever you are touching |

**Settings load in this order**, later wins:
`config/default.yaml` → `config/paper.yaml` (or testnet/mainnet) → `config/overrides.yaml`
(written by the panel) → environment variables like `LIQFADE_SIZING__RISK_FRAC=0.004`.

`liqfade config show` prints the result and where each number came from.

## Running it locally

Everything below runs on your laptop. No server, no exchange keys, no money.

```bash
make setup     # install dependencies
make test      # 104 tests
make lint      # formatting and type checks
```

Look around:

```bash
liqfade --help
liqfade config show --env paper           # every setting and its source
liqfade config show --env mainnet         # the tighter live limits
uv run python scripts/probe/hl_meta.py    # top Hyperliquid perps right now, live
```

Open the dashboard:

```bash
liqfade panel hash-password               # type a password, get a hash
export LIQFADE_PANEL_PASSWORD_HASH='<paste the hash>'
liqfade panel                             # http://127.0.0.1:8787
```

It will say the state file is missing. That is correct: no bot is running.

## Getting to a backtest

This is the part you actually want, and it is three phases of work away. In order:

1. **Phase 1, recorder.** Saves live market data to disk. Needed because nobody sells
   the cross-exchange liquidation history you want, so you have to collect it yourself
   starting now. Runs fine on a laptop, but it has to stay on.
2. **Phase 2, download old data.** Pulls 6 to 12 months of Hyperliquid history from
   Amazon S3 to your laptop. See
   [docs/DATA_SOURCES.md, "Getting 6 to 12 months of history"](docs/DATA_SOURCES.md).
   This is the one step that costs money: roughly $16 for 6 months, $33 for 12.
3. **Phase 3, research.** Notebooks that answer one question first: after a cascade,
   does price actually bounce back enough to cover the 0.09% round-trip fee? If the
   answer is no, the project stops here and that is a good outcome, not a failure.
4. **Phase 4, backtester.** Only worth building once Phase 3 says there is something
   there. Then `liqfade backtest` works and you can run it as many times as you like.

Steps 2, 3 and 4 all run on your laptop. Only step 1 wants a machine that stays on.

## Putting it on a server, later

Not needed for backtesting. When the strategy has earned it, see
[docs/DEPLOY_VPS.md](docs/DEPLOY_VPS.md).

## Rules that are not up for negotiation

Copied from CLAUDE.md because they matter more than anything else here.

- No result is reported without fees, funding and a 1 second delay included.
- No code path can place a real order unless `LIQFADE_ENV=mainnet` **and**
  `LIQFADE_LIVE_TRADING=1` **and** the file `data/KILL` does not exist.
- `/kill` on Telegram, or creating `data/KILL`, cancels everything and closes every
  position within 5 seconds.
- Keys never go in the repo, never in a log line, never in a chat window.
