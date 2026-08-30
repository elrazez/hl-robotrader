# Strategy, sizing, risk and backtest spec (v0)

All numbers below are starting defaults for research, not tuned parameters. Every one of them lives in `config/default.yaml` with unit and rationale. Research in Phase 3 replaces them with evidence.

## 1. Thesis, stated precisely

When forced selling (long liquidations) or forced buying (short liquidations) dominates order flow in a coin for a short window, price overshoots the level that voluntary flow would have produced. Once the forced flow exhausts, price partially reverts toward the pre-cascade reference within minutes. We enter against the cascade direction at or after exhaustion and exit on partial reversion, a time limit, or a stop beyond the cascade extreme.

What would falsify it: after fees and a 1s latency, the average post-exhaustion reversion is smaller than the round-trip cost, or the hit rate collapses on out-of-sample data, or the effect only exists in a few large events that dominate the sample.

## 2. Feature definitions (`features/`)

All features are computed per coin, at decision cadence 250ms, from data with `rx_ms < t`.

Liquidation intensity
- `liq_long_usd_w`, `liq_short_usd_w`: summed liquidation notional in trailing window `w` in {15s, 60s, 300s}, per venue and aggregated. Aggregation weights venues by completeness: Bybit and HL-exact = 1.0, Binance and OKX = configurable upweight since they publish at most one event per second (default 1.0, revisit after research).
- `liq_z_w`: z-score of `log1p(liq_side_usd_w)` versus the trailing 24h distribution of the same window, computed on a 1-minute grid. Robust scaling (median, MAD).
- `liq_imbalance_w`: `(long - short) / (long + short + eps)` in the window.
- `hl_forced_proxy_w`: HL-native proxy since the free feed does not flag liquidations: aggressive volume in the move direction over `w` divided by trailing median aggressive volume, gated by `oi_delta_w < 0` and `abs(mark - mid) / mid` above its trailing 95th percentile. Calibrate against exact S3 liquidations; report precision and recall in `reports/proxy_calibration.md`.

Price and flow
- `ret_w`: log return over `w`. `disp_w`: distance of mid from the 5-minute pre-cascade VWAP anchor, in bps.
- `rv_1m`, `rv_5m`: realized volatility from 1s mid changes; `ret_w / rv_w` is the normalized displacement.
- `cvd_w`: cumulative volume delta over `w` from HL trades (buy aggressor minus sell aggressor), and its slope over the last 15s.
- `spread_bps`, `depth_10bps_bid/ask` from L2, `book_imbalance`.
- `oi_delta_w` from `activeAssetCtx.openInterest` (HL) and Coinalyze cross-venue OI (60s cadence).
- `funding_hl`, `funding_x` (cross-venue mean), `premium` (mark vs oracle).

Regime and context
- `btc_ret_5m`, `btc_liq_z_60s` for market-wide context.
- `session` bucket (UTC hour), `since_listing_days`.

## 3. Cascade and exhaustion detection (`strategy/`)

Cascade start (per coin, per side)
- `liq_z_60s >= 3.0` (aggregated) OR `hl_forced_proxy_60s >= 4.0`
- AND `abs(ret_60s) / rv_5m >= 2.0` in the direction implied by the liquidated side (longs liquidated implies down move)
- AND `liq_imbalance_60s` favours that side by at least 0.6
- Record `cascade_start_ts`, `anchor_px` (mid at start), running `extreme_px`, `peak_liq_15s`.

Exhaustion (earliest of, all must hold for 3 consecutive ticks)
- `liq_15s <= 0.25 * peak_liq_15s`
- `cvd_slope_15s` flips sign against the cascade direction, or `book_imbalance` flips
- `abs(mark - mid) / mid` back under its trailing 80th percentile
- mid has not made a new extreme for at least 10s
- Cascade age between 20s and 15 minutes (older cascades are a different regime; drop them)

Intent
- Side: opposite to cascade direction.
- `entry_px`: best bid (long) or best ask (short) at decision time; the order manager handles repricing.
- `target_px`: `extreme_px + k_target * (anchor_px - extreme_px)`, default `k_target = 0.5`.
- `stop_px`: `extreme_px - k_stop * rv_5m * mid`, default `k_stop = 1.5` (beyond the extreme, vol-scaled), never inside the extreme.
- `time_stop_s`: default 900s.
- `urgency`: high if `liq_z_60s >= 5` (large cascades revert faster), else normal. Only high urgency may fall back to taker entry.
- `explain()`: dict of the feature values above, posted to Telegram with the intent.

Filters (skip intent when any is true)
- `spread_bps` above config max for the coin's tier
- Expected edge `(target_px - entry_px) / entry_px` below `min_edge_multiple * (taker_fee + maker_fee)` (default 4x)
- BTC itself is in an active cascade in the same direction and the coin is not BTC (market-wide sweeps behave differently; keep as a research question, default skip)
- Funding at an extreme against the trade direction beyond config threshold
- Coin listed for fewer than 14 days, or in the exclusion list
- Any risk gate false (section 5)

## 4. Sizing (`sizing/`)

Inputs: intent, live equity `E`, coin liquidity stats, config.

- Risk budget per trade: `R = risk_frac * E`, default `risk_frac = 0.005` (0.5% of equity at the stop).
- Risk-based notional: `N_risk = R / stop_distance_frac` where `stop_distance_frac = abs(entry_px - stop_px) / entry_px`.
- Liquidity caps:
  - `N_vol = vol_participation * hl_notional_volume_5m`, default 2% of the coin's trailing 5-minute HL notional volume
  - `N_depth = depth_participation * depth_10bps_same_side`, default 25% of resting depth within 10 bps on the side we hit
- Exposure caps: `N_coin_max = coin_exposure_frac * E` (default 0.25), `N_total_max = total_exposure_frac * E` minus current gross exposure (default 1.0).
- Final: `N = min(N_risk, N_vol, N_depth, N_coin_max, remaining_total, HL max leverage * E)`. Round to `szDecimals`. If `N < 10 USDC` or `N < min_notional_cfg`, return 0 with a reason.
- Leverage is whatever `N / margin` implies. Report it; never target it.
- Scaling with equity is automatic. The owner scales by depositing; nothing in the code references dollar amounts except the 10 USDC exchange minimum.

## 5. Risk gates and state (`risk/`)

Persisted state in `data/risk_state.json`: `day_start_equity`, `peak_equity`, `daily_realized_pnl`, `consecutive_losses`, `coin_cooldowns`, `halt_reason`, `halt_until`.

Gates (any false blocks new entries; some also flatten)
- Daily loss: `daily_pnl <= -max_daily_loss_frac * day_start_equity` (default 3%) -> halt entries until next UTC day, alert.
- Drawdown: `equity <= (1 - max_dd_frac) * peak_equity` (default 10%) -> halt entries and flatten, require manual `/resume`.
- Consecutive losses `>= max_consecutive_losses` (default 6) -> halt entries for `cooldown_after_streak_min` (default 120).
- Per-coin cooldown after a loss (default 30 min) and after any exit (default 5 min).
- Max concurrent positions (default 4), max positions per direction (default 3).
- Stale data: no HL trade or BBO for the coin in `stale_s` (default 5s) -> no new entries in that coin; global feed silence for 15s -> cancel all resting entries; 60s -> flatten everything, alert, halt.
- Reconnect storm: more than 5 reconnects in 10 minutes on any critical feed -> halt entries.
- Exchange rejects beyond `max_rejects_10min` (default 5) -> halt entries.
- Kill switch: Telegram `/kill` or file `data/KILL` -> cancel all, flatten with `Ioc` reduce-only, stop loop, alert. `/flat` flattens but keeps the loop paused. `/pause` stops entries only. `/resume` requires the halt reason to be resolvable and clears it.

Heartbeat: the live loop touches `data/heartbeat` every second; the watchdog (separate systemd unit) cancels all orders and alerts if the heartbeat is older than 10s.

## 6. Backtest engine requirements (`backtest/`)

- Event-driven replay over the canonical Parquet tables at their native granularity (trades, BBO, L2 snapshots, liquidations, asset ctx). Decision cadence 250ms as in live. Same `features/`, `strategy/`, `sizing/`, `risk/` code paths.
- Latency model: intent at `t` becomes an order visible to the market at `t + latency_ms` (default 1000, configurable distribution later).
- Maker fill model: a resting `Alo` order at price `p` fills only when the market trades through `p` by at least one tick, or when traded volume at `p` since placement exceeds an assumed queue ahead equal to the displayed size at `p` when placed. Partial fills allowed. Configurable pessimism factor.
- Taker fill model: walk the most recent L2 snapshot from the touch, apply the size, compute VWAP; if the snapshot is older than 1s, add an extra slippage of `slip_stale_bps`.
- Costs: maker/taker fees per fill from config (or from a recorded `userFees`), funding accrued hourly while a position is open, no gas.
- Stops: trigger at mark proxy (mid) and fill as taker with the model above. Report stop slippage separately.
- Metrics per run: trade count, net PnL, gross PnL, fees, funding, hit rate, average win and loss in bps, profit factor, Sharpe and Sortino on per-trade and daily returns, max drawdown, average and max hold time, exposure time fraction, turnover, PnL by coin, by hour, by cascade size bucket, by urgency, and PnL concentration (share of PnL from the top 5 trades).
- Validation: chronological split with the last 25% held out; walk-forward with monthly refits for any parameter that research chose; parameter stability plots (PnL surface must be smooth, not a spike); trade-level bootstrap for confidence intervals; deflated Sharpe given the number of parameter combinations tried. A run that wins only on the top 5 trades fails.
- Output: `reports/{run_id}/report.md` with the config hash, data range, all metrics, and the parameter surface figures; `trades.parquet` with every simulated order and fill.
- Speed target: one month of a 10-coin universe in under 5 minutes on the VPS. Vectorize feature computation on a 1s grid where exactness allows; keep the fill simulation event-driven.

## 7. Research questions for Phase 3 (answer with event studies, not backtests)

1. After an HL-exact liquidation cascade (from S3 fills), what is the distribution of mid reversion at 1, 5, 15, 30, 60 minutes, by coin tier and cascade notional bucket? Net of 0.09% round trip, where is the edge, if any?
2. Does exhaustion timing (our definition) improve the entry versus a fixed delay after cascade start?
3. How well does `hl_forced_proxy` recover exact HL liquidations? Where does it fail (thin coins, market-wide moves)?
4. Cross-venue: when Bybit liquidations spike but HL does not, does HL still revert? This decides how much cross-exchange data matters for v0.
5. Do partial-liquidation chunks (positions over 100k USDC, 30s cooldown) create a detectable rhythm we can wait out?
6. Which filters remove the losing tail without removing most of the sample?

Deliverable per question: a notebook plus a short markdown summary in `reports/research/` with the sample size, effect size and a yes/no decision for v0.
