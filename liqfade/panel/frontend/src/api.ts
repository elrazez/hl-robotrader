import type {
  AuditEntry,
  DiskStats,
  EquityCurve,
  FillsPage,
  Latency,
  Liquidations,
  ReportDetail,
  ReportRow,
  StateSnapshot,
  TradeWindow,
  TradesPage,
} from "./types";

export interface StateResult {
  state: StateSnapshot | null;
  /** Set when the backend refused to serve fresh state. */
  error: string | null;
  /** True when the backend answered 503 because the snapshot is too old. */
  stale: boolean;
  /** True when the session cookie is missing or expired. */
  unauthorized: boolean;
}

export async function fetchState(): Promise<StateResult> {
  return fetchStateFrom("/api/state");
}

/** The trading loop's snapshot. Same shape and same staleness rule, a different file. */
export async function fetchLiveState(): Promise<StateResult> {
  return fetchStateFrom("/api/live/state");
}

async function fetchStateFrom(url: string): Promise<StateResult> {
  const res = await fetch(url, { credentials: "same-origin" });
  if (res.status === 401) {
    return { state: null, error: null, stale: false, unauthorized: true };
  }
  const body = await res.json().catch(() => null);
  if (res.status === 503) {
    // A stale snapshot still comes back in the body so the page can show last known
    // values behind the banner rather than going blank.
    return {
      state: (body?.state as StateSnapshot) ?? null,
      error: body?.detail ?? "state unavailable",
      stale: true,
      unauthorized: false,
    };
  }
  if (!res.ok) {
    return { state: null, error: body?.detail ?? res.statusText, stale: false, unauthorized: false };
  }
  return { state: body as StateSnapshot, error: null, stale: false, unauthorized: false };
}

export async function login(password: string): Promise<string | null> {
  const res = await fetch("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify({ password }),
  });
  if (res.ok) return null;
  const body = await res.json().catch(() => null);
  return body?.detail ?? res.statusText;
}

export async function logout(): Promise<void> {
  await fetch("/api/logout", { method: "POST", credentials: "same-origin" });
}

export async function hasSession(): Promise<boolean> {
  const res = await fetch("/api/session", { credentials: "same-origin" });
  if (!res.ok) return false;
  return (await res.json()).authenticated === true;
}

/** Recorder page data. Both are cheap but not free, so the page polls them slowly. */
export async function fetchDisk(): Promise<DiskStats | null> {
  const res = await fetch("/api/recorder/disk", { credentials: "same-origin" });
  return res.ok ? ((await res.json()) as DiskStats) : null;
}

export async function fetchLatency(): Promise<Latency | null> {
  return fetchLatencyFrom("/api/recorder/latency");
}

/** The trading loop's latency. The only place submit-to-ack is ever measured. */
export async function fetchLiveLatency(): Promise<Latency | null> {
  return fetchLatencyFrom("/api/live/latency");
}

async function fetchLatencyFrom(url: string): Promise<Latency | null> {
  const res = await fetch(url, { credentials: "same-origin" });
  return res.ok ? ((await res.json()) as Latency) : null;
}

export async function fetchLiquidations(hours = 24): Promise<Liquidations | null> {
  const res = await fetch(`/api/recorder/liquidations?hours=${hours}`, {
    credentials: "same-origin",
  });
  return res.ok ? ((await res.json()) as Liquidations) : null;
}

/**
 * The trading loop's fills, from `orders.sqlite`. `query` is the filter string built by
 * Fills.tsx, shared with the curve below so the chart always plots the table's rows.
 */
export async function fetchFills(query: string, offset = 0, limit = 100): Promise<FillsPage> {
  const res = await fetch(`/api/live/fills?${query}&limit=${limit}&offset=${offset}`, {
    credentials: "same-origin",
  });
  if (!res.ok) return { total: 0, offset: 0, fills: [] };
  return (await res.json()) as FillsPage;
}

export async function fetchLiveEquity(query: string): Promise<EquityCurve | null> {
  const res = await fetch(`/api/live/equity?${query}`, { credentials: "same-origin" });
  return res.ok ? ((await res.json()) as EquityCurve) : null;
}

/** What one control action came back with, in the shape the page shows. */
export interface ControlOutcome {
  action: string;
  ok: boolean;
  detail: string;
  ts_ms: number;
}

/**
 * Pause, resume or flat, through the loop's control socket. `confirm` is sent because
 * the endpoint requires it: the second half of click-and-confirm is enforced server
 * side, so a page reloaded mid-dialog cannot half submit an action.
 */
export async function sendControl(
  action: "pause" | "resume" | "flat",
  coin?: string,
): Promise<ControlOutcome> {
  const res = await fetch(`/api/live/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify({ confirm: true, coin: coin ?? null }),
  });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    return {
      action,
      ok: false,
      detail: body?.detail ?? res.statusText,
      ts_ms: Date.now(),
    };
  }
  return {
    action,
    ok: true,
    detail: JSON.stringify(body.result),
    ts_ms: body.entry?.ts_ms ?? Date.now(),
  };
}

export async function fetchAudit(limit = 100): Promise<AuditEntry[]> {
  const res = await fetch(`/api/audit?limit=${limit}`, { credentials: "same-origin" });
  if (!res.ok) return [];
  return ((await res.json()).entries ?? []) as AuditEntry[];
}

/** Backtest reports. All of these are session gated like everything else. */
export async function fetchReports(limit = 50): Promise<ReportRow[]> {
  const res = await fetch(`/api/reports?limit=${limit}`, { credentials: "same-origin" });
  if (!res.ok) return [];
  return ((await res.json()).runs ?? []) as ReportRow[];
}

export async function fetchReport(runId: string): Promise<ReportDetail | null> {
  const res = await fetch(`/api/reports/${encodeURIComponent(runId)}`, {
    credentials: "same-origin",
  });
  return res.ok ? ((await res.json()) as ReportDetail) : null;
}

export async function fetchTrades(runId: string, offset = 0, limit = 200): Promise<TradesPage> {
  const res = await fetch(
    `/api/reports/${encodeURIComponent(runId)}/trades?limit=${limit}&offset=${offset}`,
    { credentials: "same-origin" },
  );
  if (!res.ok) return { total: 0, offset: 0, trades: [] };
  return (await res.json()) as TradesPage;
}

export async function fetchEquity(runId: string): Promise<EquityCurve | null> {
  const res = await fetch(`/api/reports/${encodeURIComponent(runId)}/equity`, {
    credentials: "same-origin",
  });
  return res.ok ? ((await res.json()) as EquityCurve) : null;
}

export async function fetchTradeWindow(
  runId: string,
  index: number,
): Promise<TradeWindow | null> {
  const res = await fetch(
    `/api/reports/${encodeURIComponent(runId)}/trade/${index}/window`,
    { credentials: "same-origin" },
  );
  return res.ok ? ((await res.json()) as TradeWindow) : null;
}
