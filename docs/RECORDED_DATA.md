# The recorded month

The Tokyo VPS recorded live market data from **2026-09-05 12:38:49 UTC** until it was
retired in October 2026. This file says what that recording held, how complete it was,
what it did not hold and why, what it was for, and what the month showed.

**The data was not kept.** The owner deleted the box without copying its lake (decided
2026-10-13), so this file is the only record of it. A download of the irreplaceable tables
was started and stopped part way; nothing from it is in the repo.

Every number here was measured on the box on 2026-10-12 with read-only queries, over data
up to **2026-10-12 20:00 UTC**, the last hour flushed at the time.

## What it held

| table | days | rows | size | resolution | can it be bought or re-pulled? |
|---|---|---|---|---|---|
| `liquidations` | 38 | 617,672 | 14 MB | per event | **No.** Bybit and OKX publish live and keep nothing; see `docs/DATA_SOURCES.md` |
| `hl_positions` | 35 | 35.4M | 1.2 GB | each tracked address every 60 s | **No.** `clearinghouseState` answers for now only |
| `bbo` | 38 | 253.8M | 3.3 GB | p50 146 ms, p99 1.4 s per coin | **No.** No archive carries Hyperliquid's BBO |
| `asset_ctx` | 38 | 53.5M | 1.5 GB | p50 1.0 s per coin | Only at one row a minute, from the `asset_ctxs` archive |
| `l2` | 38 | 203.7M | 2.3 GB | one 20-level snapshot per coin every 5.4 s | Yes, the S3 archive has the same 5.4 s cadence since 2026-06-21, for egress |
| `trades` | 38 | 78.8M | 1.3 GB | every trade | Yes, from `hl_s3` fills, but without `rx_ms` |
| `candles_1m` | 38 | 0.9M | 27 MB | 1 minute | Yes, `liqfade derive candles` from `trades` |

Cadences measured on BTC on 2026-10-11. The `l2` cadence is the same 5.4 s measured on
2026-09-05, so `backtest.stale_book_s` still marks every live book stale (see `l2Book` in
`docs/DATA_SOURCES.md`). The other thing nothing else has is **`rx_ms`, the local receive
time, on every row**: the only honest availability clock for a no-lookahead study.

`liquidations` by venue:

| venue | rows | notional | coins |
|---|---|---|---|
| Bybit | 186,442 | $1,000M | the recorder's universe only, 10 to 24 a day |
| OKX | 431,230 | null (sizes are contracts) | every swap, 162 to 466 a day; 3,009 rows with `coin = null` (inverse or unmapped swaps) |
| Binance | 0 | | geo-blocked, see below |

`hl_positions` saw 27,675 distinct addresses. It starts 2026-09-08 14:15 UTC.

## Per day

`HL coins` is the universe that day. `silence` is the longest stretch with no Hyperliquid
trade at all, across every coin, by `rx_ms`. Polls of `hl_positions` are distinct minutes
with a row. Weekends are marked because they are visibly thinner.

| date | | HL coins | trades | silence s | Bybit liqs | OKX liqs | positions rows | addresses | polls |
|---|---|---|---|---|---|---|---|---|---|
| 09-05 | Sa | 18 | 864,820 | 14.8 | 1,177 | 4,525 | | | |
| 09-06 | Su | 22 | 2,201,498 | 18.6 | 5,098 | 9,910 | | | |
| 09-07 | | 23 | 1,917,889 | 11.2 | 3,097 | 9,393 | | | |
| 09-08 | | 23 | 2,134,280 | 28.1 | 3,577 | 12,209 | 470,399 | 490 | 585 |
| 09-09 | | 23 | 2,611,041 | 3.0 | 6,014 | 16,282 | 1,168,990 | 5,933 | 1,440 |
| 09-10 | | 22 | 2,160,309 | 2.8 | 5,327 | 12,035 | 1,075,880 | 5,552 | 1,440 |
| 09-11 | | 18 | 2,173,602 | 32.6 | 6,712 | 12,913 | 991,451 | 5,444 | 1,440 |
| 09-12 | Sa | 17 | 745,472 | 34.6 | 585 | 3,362 | 926,920 | 3,909 | 1,440 |
| 09-13 | Su | 12 | 803,616 | **101.2** | 2,407 | 6,978 | 773,576 | 4,017 | 1,439 |
| 09-14 | | 11 | 1,620,909 | 3.3 | 3,523 | 9,548 | 858,488 | 4,896 | 1,440 |
| 09-15 | | 15 | 1,965,615 | 3.1 | 7,970 | 14,045 | 877,011 | 5,211 | 1,440 |
| 09-16 | | 14 | 1,864,937 | 7.3 | 4,341 | 11,184 | 881,257 | 4,903 | 1,440 |
| 09-17 | | 16 | 2,033,401 | 2.3 | 3,862 | 11,206 | 1,003,249 | 5,076 | 1,440 |
| 09-18 | | 20 | 2,869,489 | 2.0 | 10,074 | 17,680 | 1,117,529 | 5,747 | 1,440 |
| 09-19 | Sa | 21 | 1,969,791 | **107.3** | 4,080 | 12,849 | 1,162,040 | 4,680 | 1,440 |
| 09-20 | Su | 21 | 2,193,186 | 19.8 | 4,786 | 14,946 | 1,148,820 | 5,018 | 1,440 |
| 09-21 | | 23 | 3,340,848 | 3.2 | 14,355 | 18,802 | 1,188,420 | 5,615 | 1,440 |
| 09-22 | | 24 | 2,684,836 | 9.3 | 5,126 | 14,760 | 1,162,808 | 5,080 | 1,440 |
| 09-23 | | 23 | 3,072,671 | **275.3** | 8,212 | 19,038 | 1,109,696 | 5,154 | 1,436 |
| 09-24 | | 25 | 2,603,662 | 9.8 | 4,890 | 12,241 | 1,072,818 | 5,332 | 1,440 |
| 09-25 | | 25 | 2,477,410 | 9.0 | 5,476 | 11,832 | 1,112,158 | 5,059 | 1,440 |
| 09-26 | Sa | 22 | 1,625,097 | **133.3** | 2,017 | 6,495 | 1,101,414 | 4,142 | 1,440 |
| 09-27 | Su | 22 | 1,748,341 | 3.1 | 3,306 | 7,118 | 1,061,534 | 4,516 | 1,440 |
| 09-28 | | 25 | 3,000,503 | 2.5 | 5,921 | 18,108 | 1,122,083 | 5,377 | 1,440 |
| 09-29 | | 26 | 2,566,499 | 2.6 | 4,301 | 11,301 | 1,120,315 | 5,022 | 1,440 |
| 09-30 | | 23 | 2,810,697 | 2.2 | 3,638 | 10,140 | 1,118,504 | 5,093 | 1,440 |
| 10-01 | | 22 | 2,319,265 | 9.5 | 2,737 | 9,424 | 1,100,184 | 4,749 | 1,440 |
| 10-02 | | 22 | 2,786,221 | 1.9 | 7,045 | 14,834 | 1,088,817 | 5,057 | 1,440 |
| 10-03 | Sa | 21 | 1,534,553 | **142.2** | 2,769 | 5,435 | 1,075,154 | 4,122 | 1,439 |
| 10-04 | Su | 17 | 1,356,248 | 3.0 | 1,765 | 4,839 | 982,680 | 3,985 | 1,440 |
| 10-05 | | 18 | 1,838,383 | **72.8** | 2,545 | 7,682 | 1,020,274 | 4,521 | 1,440 |
| 10-06 | | 19 | 1,544,616 | 2.6 | 1,756 | 6,842 | 1,046,734 | 4,290 | 1,440 |
| 10-07 | | 21 | 2,231,613 | **90.5** | 7,080 | 13,712 | 1,027,757 | 4,707 | 1,439 |
| 10-08 | | 23 | 3,193,562 | 2.2 | 16,328 | 23,568 | 1,019,681 | 4,947 | 1,440 |
| 10-09 | | 21 | 1,679,491 | 3.0 | 2,739 | 8,497 | 1,010,682 | 4,257 | 1,440 |
| 10-10 | Sa | 16 | 1,025,019 | **145.6** | 2,361 | 7,289 | 824,656 | 3,677 | 1,440 |
| 10-11 | Su | 15 | 1,258,210 | 7.3 | 4,311 | 8,426 | 820,183 | 3,872 | 1,440 |
| 10-12 | | 21 | 1,960,740 | 14.7 | 5,134 | 11,782 | 778,773 | 4,198 | 1,200 |

## How complete it was

**No day is missing and the recorder ran the whole month.** It was restarted once by hand
(2026-10-01 06:28 UTC, a 9.5 s hole). The positions poll answered in every minute of every
full day from 2026-09-09. The first day starts at 12:38:49 UTC and the last day ends
wherever the recorder was stopped.

**The universe moves.** The recorder follows the top Hyperliquid perps by volume and
re-picks every hour, so 49 coins appear but only eight are there every day: BTC, ETH, SOL,
HYPE, XRP, ZEC, NEAR, PUMP. UNI has 37 days, LIT 36, ENA 35, PONS 29, TAO and WLD 25, SUI 24,
DOGE, ARB, VVV and ONDO 20, and the rest fewer. Bybit liquidations are subscribed for the
same universe, so **a coin outside the universe that day has no Bybit liquidations either**.
OKX is unaffected: it records every swap. Weekends shrink the universe (11 coins on
2026-09-14).

**The silences over a minute have two different causes.** Telling them apart needed the
other feeds:

| when (UTC) | length | Bybit/OKX and the positions poll | cause |
|---|---|---|---|
| 09-13 08:36, 09-19 08:33, 09-26 08:31, 10-03 09:10, 10-10 08:17 | 101 to 146 s | kept arriving | **Hyperliquid's websockets.** Their REST API kept answering. Five of the six weekends; the first weekend's recording began after its Saturday morning |
| 09-23 23:36 | 275 s | stopped too, and `state.json` stopped for 5.6 min | the box or its network |
| 10-05 19:38, 10-07 21:30 | 73 s, 91 s | stopped too | the box or its network, probably |

Everything else is under 35 s. Phase 1's bar of no gap over 5 s per stream per day is not
met on most days: the all-coin trade silence alone stays under 5 s on only 16 of the 38. A
study should drop an episode whose window crosses one of the eight silences above rather
than read the silence as a quiet market.

**Reconnects** (from the health log, 2026-09-06 to 2026-10-12): 85 on the Hyperliquid socket,
which all five Hyperliquid streams share, 54 on Bybit and 82 on OKX. The median day has one
Hyperliquid reconnect; the weekend-morning days have 7 or 8.

**Receive delay** (`rx_ms - ts_ms`, 2026-10-11): Hyperliquid BTC trades p50 255 ms, p99
760 ms; Bybit liquidations p50 287 ms, p99 535 ms; **OKX liquidations p50 1.0 s, p99 41 s**.
OKX's throttled stream delivers some events very late, so an OKX liquidation is available
at its `rx_ms`, never at its `ts_ms`.

## Rules it needed

Kept because a future recording of the same feeds will need the general ones.

- **Join on `ts_ms`, decide on `rx_ms`.** CLAUDE.md rule 1. It matters most for OKX (above)
  and for `asset_ctx`, whose `ts_ms` is set to `rx_ms` because the payload carries no time.
- **`trades`: dedupe on `(coin, tid)`.** Only 2026-09-06 has duplicates (4,411), from a
  restart before the fix (`docs/DATA_SOURCES.md`, "the `trades` subscribe answer").
- **`hl_positions`: exclude `rx_ms < 1788880763381`** (before 2026-09-08 15:19:22 UTC,
  47,125 rows) from any receive-time analysis. Those rows were stamped once per poll
  (`docs/DECISIONS.md`, 2026-09-08). **Count Q9 days from 2026-09-10**: the poller switched
  to a priority set plus a sweep on 2026-09-09, and that changed the sample
  (`docs/DECISIONS.md`, 2026-09-09).
- **OKX `notional_usd` is null.** Join `ctVal` from OKX's instruments endpoint to fill it,
  or use OKX for counts only.
- Weekdays and weekends differ by up to 3x in trades and liquidations. Compare like with
  like.

## What it did not hold, and why

- **Binance liquidations.** Binance is geo-blocked from Tokyo. The socket connected for four
  hours on 2026-09-05 and never delivered a message, the same silent block seen
  elsewhere. It was switched off that day with
  `LIQFADE_VENUES__BINANCE__ENABLED=false` in the box's `.env`, and that comment was the only
  record of it. Binance stays enabled in `config/default.yaml`.
- **Cross-venue OI and funding (`xchg_ctx`, Coinalyze).** The poller died at its first 429
  (`prompts/02g_review_followups.md` item 29) and was switched off by
  `LIQFADE_VENUES__COINALYZE__ENABLED=false` the same day. The code still has no 429 backoff,
  so item 29 is open.
- **Hyperliquid's own liquidations.** The public feed does not flag them; they come only
  from S3, a day late.
- **Strategy results.** The paper loop never traded (see below).

## What it was for, and what is lost with it

| question | what this data would have given it | state |
|---|---|---|
| **Q9**, does the liquidation map measure anything | 32 complete days of `hl_positions` from 2026-09-10 to 2026-10-11, against a bar of 21 | **The bar was met and Q9 was never run.** `hl_positions` cannot be re-pulled, so Q9 starts from zero on any future recorder |
| **Q4**, do Bybit-only cascades revert on Hyperliquid | 37 full days of Bybit and OKX, against the preregistered **60 recorder days** | Never run on it. Stays at its 12 Tardis days (`reports/research/q4_crossvenue.md`) |
| **Q3 re-cut**, the HL-native proxy | `asset_ctx` at about 1 s, against the archive's one a minute | Never run on it |
| **Phase 5 drift**, paper against replay | nothing: paper placed nothing | Could not run |

## What the month showed

1. **The paper acceptance run placed nothing.** It ran from 2026-09-07 17:51 to 2026-09-12
   06:24 UTC, four and a half days, on `cascade.trigger: crossvenue` (set in the unit) with
   the default thresholds (`liq_z_min` 3.0, `ret_over_rv_min` 2.0). `orders.sqlite` holds
   only the 2026-09-06 and 09-07 drills: no order after them and no refusal in the
   `refusals` table, which the paper loop had from its 2026-09-08 15:02 restart. With Binance off and OKX notional
   null, the cross-venue liquidation notional was Bybit alone. Whether that is why the
   trigger never fired was a replay question, and it went unanswered with the data.
   `config/backtest/v2*.yaml` lowered
   `ret_over_rv_min` to 1.0 because 2.0 starts only 24% of the research cascades.
2. **The watchdog stopped it, and the whole box had stalled.** At 2026-09-12 06:24:44 UTC the
   watchdog wrote `KILL`: "paper loop is stale. heartbeat is 11.5s old, over the 10s limit".
   In the same minute the recorder's own message rate fell to a quarter (64 trades against
   about 200), so the stall was not the paper loop's alone. The box has 3.8 GB of memory and
   was using 0.8 GB of swap on 2026-10-12. The cause cannot be recovered: that day's journal
   has rotated away.
3. **The flag stayed up for a month and nothing said so.** `liqfade-paper` and
   `liqfade-testnet` restart every 3 s, and each start is refused while `KILL` exists:
   **621,241 refused starts each** by 2026-10-12. A refused start is not an alert, so the
   Phase 5 acceptance run silently ended on its fifth day. Before `liqfade-live` exists, a
   `KILL` older than an hour should page, and a unit guarded by the flag should stop
   restarting rather than retry forever.
4. **The journal is not an archive.** Those restarts and the once-a-minute poll log keep the
   journal to about one day (195 MB). The month's operational history in this file comes
   from a loop left over from the 01c work, `/tmp/watch.sh`, started 2026-09-06 20:14, which
   copied every feed's stats out of `state.json` once a minute. That's the health log below.
5. **Hyperliquid's websockets go quiet for about two minutes on weekend mornings,** around
   08:30 UTC, while its REST API keeps answering. Seen five times in six weeks. A live loop
   will see this as stale data and should not halt for the day over it.

## The health log

`data/backup/watch-01c.jsonl` on the box, 51,846 lines, one a minute from 2026-09-06 20:14.
It went with the box; the reconnect and stall figures above were read from it first. Each
line was `{"at": <state.json ts_ms>, "feeds": {name: [gaps_1h, reconnects_1h, msgs_1m,
cadence_ms, worst_coin, worst_lag_ms, delivery_coin, delivery_lag_ms]}}`. The counts are
rolling one-hour windows sampled every minute, so counting events means taking the last
sample of each hour. The five Hyperliquid streams share one socket, so their reconnect
counts are the same events five times. A future recorder should write this itself rather
than leave it to a stray script.
