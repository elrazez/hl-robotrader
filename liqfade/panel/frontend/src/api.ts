import type {
  DiskStats,
  EquityCurve,
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
  const res = await fetch("/api/state", { credentials: "same-origin" });
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
  const res = await fetch("/api/recorder/latency", { credentials: "same-origin" });
  return res.ok ? ((await res.json()) as Latency) : null;
}

export async function fetchLiquidations(hours = 24): Promise<Liquidations | null> {
  const res = await fetch(`/api/recorder/liquidations?hours=${hours}`, {
    credentials: "same-origin",
  });
  return res.ok ? ((await res.json()) as Liquidations) : null;
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
