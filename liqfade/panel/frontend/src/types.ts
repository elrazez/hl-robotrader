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

/** GET /api/recorder/liquidations: hourly notional per venue, aligned on hour_ms. */
export interface Liquidations {
  hours: number;
  hour_ms: number[];
  venues: Record<string, (number | null)[]>;
  totals: Record<string, number>;
  events?: Record<string, number>;
}
