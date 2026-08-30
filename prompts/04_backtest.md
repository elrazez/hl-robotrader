# Prompt 04: Phase 4 backtester and strategy v0

Owner notes: only run this if `reports/research/SUMMARY.md` said continue. Everything
runs on the laptop against the Parquet lake. This is the phase you will re-run dozens of
times; speed and determinism matter as much as correctness.

---

You are working in the liqfade repo. Read `CLAUDE.md` in full, then
`docs/STRATEGY_AND_RISK.md` sections 3 to 6 (cascade, exhaustion, intent, sizing, risk,
backtest spec) and `docs/ROADMAP.md` Phase 4. `reports/research/SUMMARY.md` and
`docs/DECISIONS.md` carry the parameter values research chose; the config already
reflects them. Use the config values, never re-hardcode numbers from the docs.

This session is **Phase 4: strategy v0, sizing, risk, and the backtest engine**, plus
Panel v2 (report browser). Do NOT build execution/ (except `rounding.py`, below), live/,
or alerts/. The single most important constraint from CLAUDE.md: strategy code must run
against a Parquet replay with zero changes; if you feel the need for a live-only branch
in `strategy/` or `features/`, stop and put it in `live/`'s future scope instead.

## Decisions already made

**strategy/**
- `types.py`: frozen dataclasses (not pydantic; this is a hot path):
  `Intent(coin, side, entry_px, target_px, stop_px, time_stop_s, urgency, ts_ms,
  explain: dict[str, float])` and `CascadeState(coin, side, start_ts, anchor_px,
  extreme_px, extreme_ts, peak_liq_15s, exhaustion_streak)`.
- `cascade.py`: an online detector, one instance per coin, fed a `FeatureRow` every
  tick. Start conditions, exhaustion conditions, age windows exactly per spec section
  3, all thresholds from config. It must produce the SAME episodes as
  `features/episodes.py` on the same data within tolerance (test below).
- `engine.py`: `StrategyEngine.on_tick(now_ms, features_by_coin) -> list[Intent]`.
  Deterministic, no I/O, no wall clock, no randomness. `explain` carries every feature
  value that crossed a threshold.
- `filters.py`: the skip conditions from spec section 3, each returning a reason
  string; the engine logs which filter killed an intent.
- `rules.py`: the confluence rule layer, the owner's mechanism for trying different
  entry conditions without touching engine code. A `ConfluenceRule` protocol:
  `evaluate(features, cascade: CascadeState) -> RuleVerdict(passed: bool,
  values: dict[str, float])`. The engine runs every ENABLED rule after cascade and
  exhaustion fire; all enabled rules must pass or the intent is dropped with the rule
  name as the reason; every rule's values merge into `explain()` either way. Rules
  are pure and registered in one dict `RULES: dict[str, type[ConfluenceRule]]`.
  Config: a `strategy.rules.<name>` section per rule with `enabled: bool` plus its
  params (models.py + default.yaml as usual). Ship two rules in this phase:
  - `zone_proximity`: passes when the cascade extreme is within
    `zone_width_bps` of a pre-existing level from `features/levels.py`
    (enable it by default ONLY if Q7's DECISION was yes; otherwise ship disabled).
  - `min_cascade_notional`: passes when episode notional exceeds a config floor.
    Trivial on purpose: it proves the mechanism with something easy to reason about.
  With every rule disabled the engine is bit-identical to the bare spec behavior;
  the golden test runs in that configuration. The backtest report gets a per-rule
  table: evaluated / passed / intents killed, so the owner sees what each rule cost
  and saved. New rule ideas later mean: write one class, one config section, one
  test, rerun; no engine changes.
- `mypy` strict already covers strategy/, sizing/, risk/, execution/.

**sizing/**
- One public function `size_intent(intent, equity, stats: LiquidityStats, meta:
  AssetMeta, cfg) -> SizeDecision(notional_usd, size, leverage_implied, reason)` where
  reason is None on success or names the binding cap ("risk_budget", "vol_cap",
  "depth_cap", "coin_exposure", "total_exposure", "venue_max_leverage",
  "min_notional"). All five caps and the 10 USDC floor per spec section 4. Pure.

**risk/**
- `state.py`: `RiskStateStore` persisting `data/risk_state.json` atomically on every
  mutation (same temp+replace pattern as control/state.py), fields per spec section 5.
- `gates.py`: pure functions, one per gate, each `(state, now_ms, cfg) -> str | None`
  reason. `engine.py`: `RiskEngine.check_entry(...)` runs all gates,
  `RiskEngine.on_fill/on_exit/on_reject/on_reconnect` mutate state. Gates that also
  flatten return a `RiskAction.FLATTEN` marker the caller acts on. The kill switch
  file check lives here too (`data/KILL` present -> block everything) and must not
  import strategy.

**execution/rounding.py** (the only execution file this phase)
- `round_price(px, sz_decimals) -> Decimal` (max 5 significant figures AND max
  `6 - szDecimals` decimals, integers always valid) and `round_size(sz, sz_decimals)`.
  Property test: for every asset in a canned meta payload, 1000 random prices round to
  valid values and never move by more than one tick.

**backtest/**
- `data.py`: a merged event iterator over the lake for (coins, start, end), ordered by
  `effective_ts` (rx_ms if > 0 else ts_ms), yielding typed events (Trade, Bbo, L2,
  Liq, AssetCtx, Candle). Missing tables are allowed (S3-era data has no bbo/l2) and
  the fill model degrades per the rules below.
- `clock.py`: decision ticks every `features.decision_cadence_ms` between events.
- `engine.py`: the replay loop: events update rolling buffers; on each tick, features
  -> strategy -> sizing -> risk -> the order simulator. Feature computation uses the
  same `liqfade/features/` functions on incremental buffers; precompute what can be
  vectorized on a 1s grid per coin before the event loop (the spec's speed rule:
  vectorize features, keep fills event-driven).
- `fills.py`: latency: an intent at t becomes an order at `t + backtest.latency_ms`.
  Maker (Alo) fill: fills only when a trade prints strictly through the price, or
  cumulative traded volume at the price since placement exceeds the displayed size at
  placement times `maker_queue_pessimism`; partial fills allowed. Taker (Ioc): walk
  the latest l2 snapshot from the touch, VWAP the levels; if the snapshot is older
  than `stale_book_s` add `slip_stale_bps`; if NO l2 exists for that window (S3-era),
  fill at the last trade price plus `slip_stale_bps` and count the trade in the
  metrics bucket `no_book_fills` so the report shows how many fills used the degraded
  model. Stops trigger on mid and fill as taker; stop slippage reported separately.
- `costs.py`: maker/taker fees from config per fill; funding accrued each hour
  boundary a position is open, from `asset_ctx.funding` (or fundingHistory candles if
  ctx is absent for the window; if neither exists, funding cost 0 and the report says
  so).
- `portfolio.py`: cash, positions, equity mark-to-market on mid each tick; the risk
  engine and sizing read equity from here; entry orders expire per `entry_ttl_s` and
  reprice up to `max_reprices` like the live order policy, so the backtest exercises
  the same order lifecycle.
- `metrics.py`: every metric named in spec section 6, including PnL concentration
  (top-5 trade share) and the by-coin / by-hour / by-cascade-size / by-urgency splits.
- `report.py`: `reports/{run_id}/report.md` with config hash (sha256 of the resolved
  config json), data range, all metrics, and figures; `trades.parquet` with every
  simulated order and fill. `run_id = {utc timestamp}_{config hash[:8]}`.
- `validate.py`: chronological 75/25 split, walk-forward with
  `walk_forward_refit_days` (v0 has no fitted parameters, so walk-forward reduces to
  reporting metric stability per window; build the scaffolding anyway), trade-level
  bootstrap CIs, deflated Sharpe taking `n_trials` from a counter file
  `reports/.n_trials` that increments on every backtest run (an honest tally of how
  many configs were tried).
- Determinism: a fixed `--seed` (default 0) for anything stochastic; running the same
  command twice must produce byte-identical `trades.parquet`; there is a test for it.
- CLI: `liqfade backtest --config config/backtest/v0.yaml --start --end --coins
  [--seed N] [--out reports/]`. The run config layers over the env config as already
  supported by the loader. Also `--lagged-ms 3000` to rerun with degraded latency
  (the roadmap's latency-fragility check).

**Panel v2**
- Backend: `GET /api/reports` (list run_ids with headline metrics parsed from
  report.md front matter; add a small json sidecar `summary.json` per run to avoid
  parsing markdown), `GET /api/reports/{run_id}` (summary + report markdown),
  `GET /api/reports/{run_id}/trades?limit=500&offset=0`, and
  `GET /api/reports/{run_id}/trade/{n}/window` returning price candles, liquidation
  events and the entry/exit/stop marks for a window from 10 minutes before cascade
  start to exit, read from the lake via duckdb. All session-gated.
- Frontend: a Reports tab: run list, metrics table, equity curve with uplot, trade
  table, and a trade detail view using `lightweight-charts` (add dependency, rebuild,
  commit bundle). Keep it functional, not pretty.

## Deliverables

1. strategy/, sizing/, risk/, execution/rounding.py, backtest/ as specified.
2. CLI `backtest` implemented, placeholder removed.
3. Panel v2.
4. Tests, all offline, using the fixture lake (extend it with synthetic l2/bbo rows if
   Phase 1's fixture lacks them):
   - fill model: maker no-fill when price touches without trading through; maker
     partial fill; queue pessimism; taker VWAP walk over a book; stale book slip;
     no-book degraded fill counted; stop gap-through fills at the gapped price, not
     the stop price.
   - latency: an intent at t cannot fill from a trade at t + latency - 1ms.
   - costs: fee arithmetic; funding accrual across exactly one and multiple hour
     boundaries.
   - no-lookahead at the engine level: mutate all events after time T in a copy of the
     fixture, assert intents up to T are identical.
   - consistency: the online cascade detector vs `features/episodes.py` on the same
     synthetic stream: same episode count, start/end within one decision tick.
   - rules: a rule that fails kills the intent and names itself; rule values appear
     in explain(); all-rules-disabled equals the golden baseline byte-for-byte;
     zone_proximity passes and fails correctly against a constructed level set;
     an unknown rule name in config fails validation loudly.
   - sizing: one test per binding cap.
   - risk: one test per gate, plus persistence across a simulated restart.
   - rounding property test as above.
   - determinism: same seed, byte-identical trades.parquet.
   - golden run: fixed synthetic fixture with hand-checkable expected results
     (N cascades, M fills, final PnL within tolerance) so a refactor that changes
     behavior fails loudly.
5. A `--profile` flag that prints the top-20 cumulative profile, and a documented
   speed check: one month, 10 coins, under 5 minutes on the owner's machine (you
   verify on the fixture scaled up synthetically; the owner verifies for real).

## How to work

- Print a short plan and wait for OK.
- Commit order: rounding; strategy types+cascade+filters+rules+engine; sizing; risk;
  backtest data+clock; fills+costs+portfolio; engine+metrics+report; validate+CLI;
  panel v2; golden test last. `make lint test` green before each.
- Floats in features/backtest, Decimal only inside rounding.py outputs. Time is int
  ms UTC everywhere.
- No new Python dependencies. Frontend may add `lightweight-charts` only.
- No em dashes anywhere.

## Definition of done

`make lint test` green, zero skips. `liqfade backtest` on the fixture lake produces a
report directory with report.md, summary.json and trades.parquet, twice, byte-identical.
Owner-run acceptance per the roadmap: full-history run with 25% holdout, net-of-cost
metrics, concentration and stability sections present; the `--lagged-ms 3000` rerun
either stays positive or the report is stamped "latency fragile" on its first page.
End with the exact commands for those owner runs.
