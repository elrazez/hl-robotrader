// Mirrors liqfade/control/protocol.py. Bump alongside SCHEMA_VERSION there.
export const SCHEMA_VERSION = 1;

export interface FeedState {
  name: string;
  connected: boolean;
  last_msg_ms: number;
  lag_ms: number;
  msgs_1m: number;
  gaps_1h: number;
  reconnects_1h: number;
  parse_errors_1h: number;
  worst_coin: string | null;
  worst_lag_ms: number;
  cadence_ms: number;
  delivery_coin: string | null;
  delivery_lag_ms: number;
}

export interface PositionState {
  coin: string;
  side: "long" | "short";
  size: number;
  entry_px: number;
  mark_px: number;
  notional_usd: number;
  unrealized_pnl: number;
  opened_ms: number;
}

export interface OrderState {
  cloid: string;
  oid: number | null;
  coin: string;
  side: "buy" | "sell";
  px: number;
  size: number;
  filled: number;
  tif: string;
  reduce_only: boolean;
  status: string;
  submitted_ms: number;
}

export interface RiskState {
  paused: boolean;
  halt_reason: string | null;
  halt_until_ms: number | null;
  killed: boolean;
  equity: number;
  day_start_equity: number;
  peak_equity: number;
  daily_realized_pnl: number;
  consecutive_losses: number;
  open_positions: number;
  coin_cooldowns: Record<string, number>;
  actions_remaining: number | null;
}

export interface LatencyState {
  ws_ping_p50: number | null;
  ws_ping_p95: number | null;
  rest_p50: number | null;
  rest_p95: number | null;
  submit_ack_p50: number | null;
  submit_ack_p95: number | null;
  samples: number;
}

export interface StateSnapshot {
  schema_version: number;
  proc: string;
  ts_ms: number;
  env: string;
  live_trading: boolean;
  uptime_s: number;
  feeds: FeedState[];
  positions: PositionState[];
  orders: OrderState[];
  risk: RiskState;
  latency: LatencyState;
  universe: string[];
  age_s?: number;
}

export interface DiskStats {
  path: string;
  total_bytes: number;
  used_bytes: number;
  free_bytes: number;
  used_pct: number;
}

/** GET /api/recorder/latency: the retained round trip samples as [ts_ms, ms] pairs. */
export interface Latency {
  ws_ping: [number, number][];
  rest: [number, number][];
  ws_ping_p50: number | null;
  ws_ping_p95: number | null;
  rest_p50: number | null;
  rest_p95: number | null;
}

/** GET /api/recorder/liquidations: hourly notional per venue, aligned on hour_ms. */
export interface Liquidations {
  hours: number;
  hour_ms: number[];
  venues: Record<string, (number | null)[]>;
  totals: Record<string, number>;
  events?: Record<string, number>;
}

/** GET /api/reports: one row per backtest run, newest first. */
export interface ReportRow {
  run_id: string;
  error: string | null;
  trigger: string | null;
  env: string | null;
  start_ms: number | null;
  end_ms: number | null;
  coins: string[];
  latency_fragile: boolean | null;
  trades: number | null;
  net_pnl: number | null;
  avg_net_bps: number | null;
  hit_rate: number | null;
  sharpe_per_trade: number | null;
  max_drawdown_frac: number | null;
  top5_pnl_share: number | null;
  no_book_share: number | null;
}

/** GET /api/reports/{run_id}: the sidecar plus the markdown the owner reads. */
export interface ReportDetail {
  run_id: string;
  summary: Record<string, unknown>;
  markdown: string;
}

/** One row of trades.parquet. `index` is its place in entry order across the whole run. */
export interface TradeRow {
  index: number;
  coin: string;
  side: string;
  size: number;
  entry_px: number;
  exit_px: number;
  entry_ms: number;
  exit_ms: number;
  hold_s: number;
  exit_reason: string;
  net_pnl: number;
  net_bps: number;
  fees_usd: number;
  funding_usd: number;
  entry_slippage_bps: number;
  exit_slippage_bps: number;
  no_book_entry: boolean;
  no_book_exit: boolean;
  urgency: string;
  cascade_notional_usd: number;
}

export interface TradesPage {
  total: number;
  offset: number;
  trades: TradeRow[];
}

/** GET /api/reports/{run_id}/equity: cumulative net PnL at each exit. */
export interface EquityCurve {
  ts_ms: number[];
  equity: number[];
  drawdown: number[];
}

/** GET /api/live/fills: one fill joined to the order it belongs to. */
export interface FillRow {
  id: number;
  cloid: string;
  ts_ms: number;
  coin: string;
  side: "buy" | "sell";
  px: number;
  size: number;
  notional_usd: number;
  fee: number;
  maker: boolean;
  tag: string;
  reduce_only: boolean;
  status: string;
}

export interface FillsPage {
  total: number;
  offset: number;
  fills: FillRow[];
}

export interface Candle {
  ts_ms: number;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
}

export interface LiquidationMark {
  ts_ms: number;
  venue: string;
  side: string;
  px: number;
  notional_usd: number | null;
}

/** GET /api/reports/{run_id}/trade/{n}/window: everything one trade's chart needs. */
export interface TradeWindow {
  trade: TradeRow;
  start_ms: number;
  end_ms: number;
  candles: Candle[];
  liquidations: LiquidationMark[];
}
