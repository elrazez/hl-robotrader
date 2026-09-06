# Prompt 04b: the backtester streams the lake and replays only where cascades are

Owner notes: the year run on four coins is killed by the OS on an 8 GB machine, twice.
`load_inputs` reads every trade in the window into memory (hundreds of millions of rows
on the top four coins) and the loop then ticks four times a second through a year in
which the strategy is idle 98% of the time. Phase 3's scripts solved the same problem in
`scripts/research/_lib.py` (`scan_day`, `windowed_trades`, `daily_grid`): reuse that
shape. No number may change. Check `pgrep -f "liqfade backtest"` before every run on the
lake, and never run two at once; this machine has 8 GB.

---

You are working in the liqfade repo. Read `CLAUDE.md`, then `liqfade/backtest/data.py`,
`liqfade/backtest/engine.py`, `liqfade/features/grid.py`, `scripts/research/_lib.py` and
`docs/STRATEGY_AND_RISK.md` section 6. The lake is at
`/Volumes/<drive>/liqfade-data`, one Parquet file per table per day.

## Decisions already made

1. **One day at a time, always.** `load_inputs` goes away. The engine takes a data source
   that yields days: for each UTC day, a column-pruned `pl.scan_parquet` of that day's
   file per table, collected, used, released. No function anywhere in `backtest/` may
   read a date range into one frame. Add a test that patches `read_parquet` to fail and
   proves the engine never calls it on more than one day's file.
2. **The feature grid is built per day and cached.** `build_feature_grid` runs on one day
   plus the lookback it needs from the previous day (the same previous-day carry Q3 uses),
   and writes the day's grid to `<data_dir>/cache/features/<feature_hash>/date=YYYY-MM-DD/
   grid.parquet`, where `feature_hash` is a sha256 of the resolved `features.*` and
   `cascade.*` config plus the trigger. A second run with the same features reads the cache
   and builds nothing; a changed stop, target, sizing or risk key does not invalidate it.
   `liqfade backtest --no-cache` rebuilds. The cache directory is documented in
   `docs/DATA_SOURCES.md` and gitignored.
3. **Raw trades are replayed only inside cascade windows.** From the day's grid, the
   active windows are `[cascade_start - backtest.window_warmup_s, last exit or
   cascade_start + exhaustion.max_age_s + intent.time_stop_s + backtest.window_margin_s]`,
   merged when they overlap. The event iterator yields trades, bbo, l2 and asset_ctx only
   inside those windows, and the decision clock ticks only inside them plus the ticks a
   working order or open position needs to expire, fill or exit. Outside a window the
   portfolio is flat and no order is working, by construction; assert that at every window
   end, and count the windows and the skipped seconds into the report ("replayed 3.1% of
   the tape").
4. **Funding and equity sampling still cover the whole window**, hour by hour, from
   `asset_ctx` at `HH:59`; a position never crosses a window boundary, so the accrual
   happens inside windows, but the equity curve gets one sample per hour outside them so
   the drawdown series is continuous.
5. **Nothing else changes.** Fills, costs, sizing, risk, strategy, metrics, the report,
   `liqfade watch` (its `stage` is `features` per day, `replaying` per window, with
   `sim_ms` advancing through skipped time too) all stay as they are.

## Proof, in this order

1. `trades.parquet` byte-identical to the current engine on the golden fixture, and on one
   real month, `2026-03-01` to `2026-03-31`, coins `BTC,ETH,SOL,HYPE`, run once with the
   current engine before you touch anything (commit that parquet's sha256 in the test as
   the reference, with the command that produced it).
2. Peak RSS under 2 GB and the wall time for the year, `2025-09-01` to `2026-08-30`, four
   coins, `/usr/bin/time -l`, nothing else running. Then the same for the full 42-coin
   research universe (`--coins` from `reports/research/episodes.parquet`'s qualified coins).
   Both numbers in the report you give back and in `docs/DECISIONS.md`.
3. A second year run with the cache warm: the wall time it saves.

## How to work

- Commits: reference parquet + test; data source; per-day grid + cache; windowed replay;
  progress stages; docs. `make lint test` green before each. No new dependencies.
- No em dashes. No refactor outside `backtest/` beyond what item 2 needs in `features/grid.py`.
