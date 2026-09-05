# Runbook

Operational procedures for the VPS. Written in Phase 0 and filled in as each phase lands;
sections marked "Phase N" describe something that does not exist yet.

First time setting the server up? Start at [docs/DEPLOY_VPS.md](DEPLOY_VPS.md). This file
is for a server that already runs.

Conventions: `<vps>` is the Tailscale host name, `<port>` the SSH port chosen during
bootstrap (default 2222). Everything runs as the `liqfade` user under `/srv/liqfade`.

    /srv/liqfade/app     the repo, read only to the services
    /srv/liqfade/data    parquet lake, state.json, orders.sqlite, KILL, heartbeat
    /srv/liqfade/logs    rotated by logrotate
    /srv/liqfade/.env    secrets, mode 0600, owned by liqfade

## Stop trading right now

In order of preference. The first two work even if the trading loop is wedged.

1. Telegram: `/kill`. Cancels all orders, closes all positions with IOC reduce-only,
   stops the loop, confirms in chat. Target is under 5 seconds. (Phase 5)
2. File flag, from any shell on the box:

       touch /srv/liqfade/data/KILL

   The loop sees it within one tick, and `liqfade-live.service` refuses to start again
   while the file exists, so a restart loop cannot walk past it.
3. systemd, if the process is not responding at all:

       systemctl stop liqfade-live

   This leaves positions open. Only do it after 1 or 2, or if the loop is not trading.

To resume: remove the flag, confirm the account state by hand, then start the service.

    rm /srv/liqfade/data/KILL
    liqfade status
    systemctl start liqfade-live

## Deploy

From your laptop, with the change already pushed to `main`:

    ssh -p <port> root@<vps> 'bash -s' < scripts/deploy.sh

It pulls the repo, runs `uv sync --frozen`, installs the systemd units and restarts only
the services whose unit files changed. If you changed code but not a unit, restart the
service yourself on purpose:

    systemctl restart liqfade-recorder
    systemctl restart liqfade-live

First deploy on a new box needs the repo URL:

    ssh -p <port> root@<vps> "LIQFADE_REPO_URL=git@github.com:you/hl-robotrader.git bash -s" \
      < scripts/deploy.sh

## Restart a service

    systemctl restart liqfade-recorder
    systemctl status  liqfade-recorder
    journalctl -u liqfade-recorder -f -n 100

Logs are one JSON object per line. To read them:

    journalctl -u liqfade-live -n 500 -o cat | jq 'select(.event=="order_reject")'

## Check health

    liqfade status                 # positions, orders, risk, feed lag, latency
    liqfade status --json | jq     # the same snapshot, machine readable

`liqfade status` asks the control socket first and falls back to `data/state.json`, so it
still answers when the loop is dead, which is when you will be asking.

The panel shows the same thing plus charts. It binds loopback only:

    ssh -p <port> -L 8787:127.0.0.1:8787 <you>@<vps>
    # then open http://127.0.0.1:8787

Or reach it directly over Tailscale.

## Rotate the Hyperliquid agent key

The agent key is an API wallet that cannot withdraw, but a leaked one can trade the
account flat. Rotate it if it was ever on a screen, in a paste buffer or in a backup.

1. Pause first, so nothing is in flight:

       # Telegram: /pause    (Phase 5), or
       touch /srv/liqfade/data/KILL

2. On `app.hyperliquid.xyz/API`, generate a new API wallet and approve it. Note the
   private key once; it is not shown again. `account_address` stays the main wallet.
3. On the VPS, edit `/srv/liqfade/.env` and replace `HL_MAINNET_AGENT_PRIVATE_KEY`.
   Keep the file at mode 0600 owned by `liqfade`.
4. Revoke the old agent in the same Hyperliquid UI.
5. Restart and confirm:

       systemctl restart liqfade-live
       liqfade status

   Reconciliation runs at startup. Any position or order it did not open raises a halt
   and a Telegram alert rather than being adopted.

Never paste a private key into a chat window, a commit, or a log line. `liqfade/log.py`
redacts key-like fields, but the reliable protection is not putting them there.

## Telegram says HALT

A halt means new entries are blocked. Existing positions may or may not have been closed,
depending on the reason. The alert names the reason; act on it.

| Reason | What happened | What to do |
|---|---|---|
| `daily_loss` | Daily realized loss hit `risk.max_daily_loss_frac` | Nothing. Entries resume at the next UTC day. Review the day's trades before then. |
| `drawdown` | Equity fell `risk.max_dd_frac` below peak. Positions were flattened | Do not resume blind. Compare live fills against the backtest fill model. Resume with `/resume` only after you understand the loss. |
| `loss_streak` | `risk.max_consecutive_losses` in a row | Auto clears after `cooldown_after_streak_min`. If it repeats the same day, the regime has changed; pause and look. |
| `stale_data` | A feed went quiet past `risk.stale_global_flatten_s` | Check `liqfade status` feed lag and the recorder journal. Usually a venue outage or a network problem. Resume once the feed is back and lag is normal. |
| `reconnect_storm` | More than `risk.max_reconnects_10min` on a critical feed | Same as above. Look at the network before resuming. |
| `rejects` | Exchange rejected more than `risk.max_rejects_10min` orders | Read the reject strings in the journal against the known list in `docs/HYPERLIQUID.md`. Insufficient margin or minimum order value means a sizing bug: do not resume, fix it. |
| `rate_limit` | Address action budget exhausted | Wait; the budget refills with traded volume. Lower `execution.max_reprices` if it recurs. |
| `unknown_position` | Reconciliation found a position the bot did not open | Do not resume. Look at the account by hand. The bot never adopts positions. |

After any halt, before resuming: `liqfade status`, then confirm positions and orders on
`app.hyperliquid.xyz` match what the bot thinks it has.

## Recorder gaps and disk

`/usr/local/bin/liqfade-disk-check` runs every 15 minutes and writes
`/srv/liqfade/data/ALERT_DISK` at 80% used. When it fires:

    du -sh /srv/liqfade/data/parquet/*        # find the big tables
    liqfade sync                              # pull to the laptop first (from the laptop)

Then remove old partitions only after they are safely on the laptop. Deleting recorded
data is a one way door; per CLAUDE.md rule 8, do it deliberately.

## Quarantined rows

`/srv/liqfade/data/quarantine/date=YYYY-MM-DD/{table}_{venue}.jsonl` holds rows the lake
refused, one JSON object per line with a `reason` field. Two reasons exist:

- `no_timestamp`: the row carried neither `rx_ms` nor `ts_ms`, so it has no partition. It
  used to land in `date=1970-01-01` and look like data forever.
- `write_failed`: a shutdown flush could not write the partition even on a retry, and
  there is no next flush tick to try again on.

Either one is a Telegram alert and a `rows_quarantined` log line. Nothing in this repo
deletes these files. A non-empty quarantine directory means a parser or the disk needs
looking at; read a few lines, fix the cause, and the rows can be replayed by hand into
the lake or written off deliberately.

## Pull data to the laptop

From the laptop, not the VPS:

    LIQFADE_SYNC_HOST=liqfade@<vps> liqfade sync --dry-run
    LIQFADE_SYNC_HOST=liqfade@<vps> liqfade sync

macOS ships openrsync, which does not support `--info=progress2`. Install GNU rsync
(`brew install rsync`) or drop that flag from `sync.rsync_flags` in your local config.

## Phase 0 verification

    make setup
    make lint
    make test
    liqfade --help
    liqfade config show --env paper
    liqfade config show --env mainnet
    liqfade panel                       # needs LIQFADE_PANEL_PASSWORD_HASH
    uv run python scripts/probe/hl_meta.py
    shellcheck scripts/*.sh
