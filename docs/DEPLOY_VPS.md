# Putting liqfade on a server

**Not needed yet.** Backtesting and research run on your laptop. This file is here for
the day the strategy has earned a server. Nothing in it is urgent.

One exception, and it is worth thinking about now: the recorder. Nobody sells the
cross-exchange liquidation history this strategy wants, so the only way to get it is to
start saving it. Every week you wait is a week of data you cannot get back later. A $12
a month box running only the recorder places no orders and risks no money. See
"Recorder-only server" at the bottom if you want that without the rest.

## What you are buying

| Item | Spec | Cost |
|---|---|---|
| Server | 2 CPU, 4 GB RAM, 80 GB disk, Ubuntu 24.04, in Tokyo | $12 to $24 a month |
| Tailscale | Private network between your laptop and the server | free |
| Telegram bot | Alerts and the kill switch | free |

Tokyo because Hyperliquid's servers are there. From far away you add about 200 ms to every
order, which matters less than you would think for a strategy that holds for minutes, but
there is no reason to pay it.

## Before you start

You need these. I cannot create any of them for you.

1. **Server.** Vultr Tokyo or AWS Lightsail Tokyo. Add your SSH public key when you
   create it, because the setup script turns off password login.
2. **Hyperliquid mainnet account** at `app.hyperliquid.xyz`, funded with USDC on
   Arbitrum. Then generate an **API wallet** at `app.hyperliquid.xyz/API`. The bot signs
   with that key; it cannot withdraw. Your main wallet address is the account address.
3. **Hyperliquid testnet account** at `app.hyperliquid-testnet.xyz`, faucet USDC, and a
   separate API wallet. Test here first, always.
4. **Telegram bot.** Message @BotFather, get a token. Get your own chat id.
5. **Coinalyze** free API key, for cross-exchange open interest and funding.
6. **Tailscale** account, free tier.

## Setup, in order

### 1. Harden the server

Copy the repo up, or clone it on the box, then as root:

```bash
bash scripts/vps_bootstrap.sh 2222
```

That creates a `liqfade` user, installs everything, moves SSH to port 2222, turns off
password login, sets up the firewall, fail2ban, automatic security updates and clock
sync, and installs Tailscale without turning it on.

**The script prints a checklist at the end. Follow it before you close that terminal.**
The important one: open a second terminal and prove `ssh -p 2222` works before you
disconnect the first. The old port 22 stays open until you delete it yourself, for
exactly this reason.

The script is safe to run twice.

### 2. Turn on Tailscale

```bash
tailscale up --ssh=false
```

It prints a URL. Open it, log in, done. The server now has a private address only your
devices can reach.

### 3. Put the secrets file in place

```bash
install -o liqfade -g liqfade -m 0600 /dev/null /srv/liqfade/.env
nano /srv/liqfade/.env
```

Copy the contents of [.env.example](../.env.example) into it and fill in the real values.
That file lists every variable the code reads. Mode 0600 means only the `liqfade` user
can read it.

Never paste a key into a chat window, a commit, or anywhere it gets logged.

### 4. Deploy

From your laptop:

```bash
ssh -p 2222 root@<server> "LIQFADE_REPO_URL=git@github.com:elrazez/hl-robotrader.git bash -s" \
  < scripts/deploy.sh
```

After the first time, just:

```bash
ssh -p 2222 root@<server> 'bash -s' < scripts/deploy.sh
```

To put a branch on the box instead of `main`, which is how a recorder or panel change is
watched on live feeds before it is merged:

```bash
ssh -p 2222 root@<server> 'LIQFADE_BRANCH=01c-recorder bash -s' < scripts/deploy.sh
```

The branch has to be pushed first: the box resets to `origin/<branch>` and never builds
from your working tree. Deploying `main` again is the same command with the variable left
off. `LIQFADE_UNITS` narrows which services a deploy may restart, and
`liqfade-live`/`liqfade-watchdog` are never enabled by the script whatever it says.

It pulls the code, installs dependencies from the lockfile, installs the service files,
and restarts only the services whose service file changed. If you changed code but not a
service file, restart it yourself on purpose:

```bash
systemctl restart liqfade-recorder
```

### 5. Start the recorder

```bash
systemctl enable --now liqfade-recorder
journalctl -u liqfade-recorder -f
```

Leave it running. Check back the next day and confirm there are no gaps.

### 6. Open the dashboard

Through an SSH tunnel, over Tailscale once step 2 is done, or over the public address
if a VPN firewall on your laptop blocks the tailnet (the wizard detects this and says
so). Tailscale on its own does not reach the dashboard: it listens on the server's own loopback address and
nowhere else, so there is nothing on the tailnet address to connect to.

```bash
ssh -p 2222 -L 8787:127.0.0.1:8787 <you>@<server>
# then open http://127.0.0.1:8787
```

Generate the password hash on the server with `liqfade panel hash-password` and put it in
`/srv/liqfade/.env` as `LIQFADE_PANEL_PASSWORD_HASH`.

The dashboard only ever listens on the server's own loopback address. It is never exposed
to the internet, and the tunnel is what carries it to you. The code refuses to start if you point it at a public address without
explicitly overriding that.

## The four services

| Service | What it does | Which phase |
|---|---|---|
| `liqfade-recorder` | Saves market data around the clock | 1 |
| `liqfade-live` | The trading loop | 5 and 6 |
| `liqfade-watchdog` | Cancels everything if the trading loop stops responding | 5 |
| `liqfade-panel` | The dashboard | 1 |

All four run as the `liqfade` user, cannot gain extra permissions, cannot write anywhere
except the data and logs folders, and restart themselves if they crash.

`liqfade-live` refuses to start while `/srv/liqfade/data/KILL` exists. So if you hit the
kill switch, a restart loop cannot quietly undo it.

## Turning on real money

Deploying does not do this. Real orders need all three, at once:

1. `LIQFADE_ENV=mainnet` in `/srv/liqfade/.env`
2. `LIQFADE_LIVE_TRADING=1` in the same file
3. No file at `/srv/liqfade/data/KILL`

Check what the bot thinks before you flip anything:

```bash
liqfade config show --env mainnet
```

It prints `live trading: ENABLED` or the exact reason it is blocked.

Do not do this until Phase 5 has run for a week on testnet with no mismatches between
what the bot thinks it holds and what the exchange says.

## Day to day

Once it is running, [docs/RUNBOOK.md](RUNBOOK.md) covers stopping trading in a hurry,
restarting services, reading logs, rotating the signing key, what each halt reason means,
and pulling data down to your laptop.

## Recorder-only server

If you want to start collecting cross-exchange data while you research, without any of
the trading parts:

1. Steps 1 to 4 above.
2. In `/srv/liqfade/.env` set `LIQFADE_ENV=paper` and leave `LIQFADE_LIVE_TRADING` empty.
3. Start only the recorder and the panel:

   ```bash
   systemctl enable --now liqfade-recorder liqfade-panel
   ```

4. Skip the Hyperliquid account, the API wallets and the funding entirely. The recorder
   only reads public data.
5. Pull the data to your laptop whenever you want:

   ```bash
   LIQFADE_SYNC_HOST=liqfade@<server> liqfade sync
   ```

This box can never place an order. There is no key on it to sign one with.
