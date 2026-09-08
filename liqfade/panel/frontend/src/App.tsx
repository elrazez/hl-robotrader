import { useCallback, useEffect, useState } from "react";
import { fetchDisk, fetchLatency, fetchLiquidations, fetchState, hasSession, logout } from "./api";
import Fills from "./Fills";
import LatencyChart from "./LatencyChart";
import LiquidationChart from "./LiquidationChart";
import Live from "./Live";
import Login from "./Login";
import Reports from "./Reports";
import type { DiskStats, FeedState, Latency, Liquidations, StateSnapshot } from "./types";
import { Banner, Row, Section, ms } from "./ui";

const POLL_MS = 1000;
// The lake does not move fast and duckdb has to read files for it, so poll it slowly.
const RECORDER_POLL_MS = 30_000;
const LIQ_HOURS = 24;

function gb(bytes: number) {
  return `${(bytes / 1e9).toFixed(1)} GB`;
}

function Feeds({ feeds }: { feeds: FeedState[] }) {
  if (feeds.length === 0) return <p className="text-sm text-neutral-500">no feeds registered</p>;
  return (
    <table className="w-full text-sm">
      <thead className="text-left text-xs uppercase text-neutral-500">
        <tr>
          <th className="pb-1 font-medium">stream</th>
          <th className="pb-1 text-right font-medium">lag</th>
          <th className="pb-1 text-right font-medium">msgs/min</th>
          <th className="pb-1 text-right font-medium">gaps 1h</th>
          <th className="pb-1 text-right font-medium">cadence</th>
          <th className="pb-1 text-right font-medium">parse errors 1h</th>
          <th className="pb-1 text-right font-medium">worst coin</th>
          <th className="pb-1 text-right font-medium">slowest coin</th>
        </tr>
      </thead>
      <tbody>
        {feeds.map((feed) => (
          <tr key={feed.name} className="border-t border-neutral-800/60">
            <td className="py-1">
              <span className={feed.connected ? "text-emerald-400" : "text-red-400"}>●</span>{" "}
              {feed.name}
            </td>
            <td className="py-1 text-right tabular-nums">{feed.lag_ms} ms</td>
            <td className="py-1 text-right tabular-nums">{feed.msgs_1m}</td>
            <td className="py-1 text-right tabular-nums">{feed.gaps_1h}</td>
            {/* The l2 gap threshold is a multiple of this, so a gap count that has moved
                is read next to the cadence that moved it. */}
            <td className="py-1 text-right tabular-nums">
              {feed.cadence_ms ? `${feed.cadence_ms} ms` : "-"}
            </td>
            <td
              className={`py-1 text-right tabular-nums ${
                feed.parse_errors_1h > 0 ? "text-amber-300" : ""
              }`}
            >
              {feed.parse_errors_1h}
            </td>
            <td className="py-1 text-right tabular-nums">
              {feed.worst_coin ? `${feed.worst_coin} ${feed.worst_lag_ms} ms` : "-"}
            </td>
            {/* How far behind the slowest coin is delivering, which the stream's own lag
                cannot show: it is never quiet while seventeen other coins are on time.
                Amber past lag_alert_s, the threshold that posts to Telegram. */}
            <td
              className={`py-1 text-right tabular-nums ${
                feed.delivery_lag_ms > 60_000 ? "text-amber-300" : ""
              }`}
            >
              {feed.delivery_coin ? `${feed.delivery_coin} ${feed.delivery_lag_ms} ms` : "-"}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

type Tab = "live" | "fills" | "recorder" | "reports";

function TabButton({
  tab,
  current,
  onPick,
  label,
}: {
  tab: Tab;
  current: Tab;
  onPick: (tab: Tab) => void;
  label: string;
}) {
  return (
    <button
      onClick={() => onPick(tab)}
      className={`rounded px-3 py-1 text-sm ${
        tab === current
          ? "bg-neutral-800 text-neutral-100"
          : "text-neutral-500 hover:text-neutral-300"
      }`}
    >
      {label}
    </button>
  );
}

export default function App() {
  const [tab, setTab] = useState<Tab>("live");
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [state, setState] = useState<StateSnapshot | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [disk, setDisk] = useState<DiskStats | null>(null);
  const [liquidations, setLiquidations] = useState<Liquidations | null>(null);
  const [latency, setLatency] = useState<Latency | null>(null);

  useEffect(() => {
    hasSession().then(setAuthed);
  }, []);

  const poll = useCallback(async () => {
    const result = await fetchState();
    if (result.unauthorized) {
      setAuthed(false);
      return;
    }
    if (result.state) setState(result.state);
    setProblem(result.error);
  }, []);

  const pollRecorder = useCallback(async () => {
    setDisk(await fetchDisk());
    setLiquidations(await fetchLiquidations(LIQ_HOURS));
    setLatency(await fetchLatency());
  }, []);

  // Both polls stop on the reports tab. A backtest report is a file that does not move,
  // and polling state.json once a second behind it would keep the trading loop's snapshot
  // warm for a page nobody is looking at.
  useEffect(() => {
    if (!authed || tab !== "recorder") return;
    poll();
    const timer = setInterval(poll, POLL_MS);
    return () => clearInterval(timer);
  }, [authed, tab, poll]);

  useEffect(() => {
    if (!authed || tab !== "recorder") return;
    pollRecorder();
    const timer = setInterval(pollRecorder, RECORDER_POLL_MS);
    return () => clearInterval(timer);
  }, [authed, tab, pollRecorder]);

  if (authed === null) return <div className="min-h-screen bg-neutral-950" />;
  if (!authed) return <Login onDone={() => setAuthed(true)} />;

  const risk = state?.risk;
  const scalars = state?.latency;

  return (
    <div className="min-h-screen bg-neutral-950 p-6 text-neutral-100">
      <header className="mb-4 flex items-baseline justify-between">
        <h1 className="text-lg font-semibold">
          liqfade{" "}
          <span className="text-sm font-normal text-neutral-500">
            {state?.env ?? "?"} / {state?.proc ?? "?"}
          </span>
        </h1>
        <div className="flex items-center gap-2">
          <TabButton tab="live" current={tab} onPick={setTab} label="live" />
          <TabButton tab="fills" current={tab} onPick={setTab} label="fills" />
          <TabButton tab="recorder" current={tab} onPick={setTab} label="recorder" />
          <TabButton tab="reports" current={tab} onPick={setTab} label="reports" />
          <button
            onClick={() => logout().then(() => setAuthed(false))}
            className="ml-4 text-sm text-neutral-500 hover:text-neutral-300"
          >
            log out
          </button>
        </div>
      </header>

      {tab === "live" ? <Live onUnauthorized={() => setAuthed(false)} /> : null}
      {tab === "fills" ? <Fills /> : null}
      {tab === "reports" ? <Reports /> : null}

      {tab === "recorder" && problem && <Banner kind="warn">stale: {problem}</Banner>}

      <div className={`grid gap-4 md:grid-cols-3 ${tab === "recorder" ? "" : "hidden"}`}>
        <Section title="status">
          <Row
            label="live trading"
            value={state?.live_trading ? "ENABLED" : "off"}
            tone={state?.live_trading ? "text-amber-300" : undefined}
          />
          <Row label="paused" value={String(risk?.paused ?? "-")} />
          <Row
            label="killed"
            value={String(risk?.killed ?? "-")}
            tone={risk?.killed ? "text-red-400" : undefined}
          />
          <Row label="halt reason" value={risk?.halt_reason ?? "-"} />
          <Row label="snapshot age" value={`${(state?.age_s ?? 0).toFixed(1)} s`} />
          <Row label="uptime" value={`${Math.round(state?.uptime_s ?? 0)} s`} />
          <Row
            label="disk"
            value={
              disk ? `${disk.used_pct.toFixed(0)}% used, ${gb(disk.free_bytes)} free` : "-"
            }
            tone={disk && disk.used_pct >= 80 ? "text-amber-300" : undefined}
          />
        </Section>

        <Section title="risk">
          <Row label="equity" value={(risk?.equity ?? 0).toFixed(2)} />
          <Row label="daily pnl" value={(risk?.daily_realized_pnl ?? 0).toFixed(2)} />
          <Row label="peak equity" value={(risk?.peak_equity ?? 0).toFixed(2)} />
          <Row label="open positions" value={String(risk?.open_positions ?? 0)} />
          <Row label="loss streak" value={String(risk?.consecutive_losses ?? 0)} />
          <Row label="actions left" value={risk?.actions_remaining?.toString() ?? "-"} />
        </Section>

        <Section title="latency">
          <Row label="ws ping p50" value={ms(scalars?.ws_ping_p50 ?? null)} />
          <Row label="ws ping p95" value={ms(scalars?.ws_ping_p95 ?? null)} />
          <Row label="rest p50" value={ms(scalars?.rest_p50 ?? null)} />
          <Row label="rest p95" value={ms(scalars?.rest_p95 ?? null)} />
          <Row label="submit to ack p50" value={ms(scalars?.submit_ack_p50 ?? null)} />
          <Row label="submit to ack p95" value={ms(scalars?.submit_ack_p95 ?? null)} />
          <Row label="samples" value={String(scalars?.samples ?? 0)} />
        </Section>

        <div className="md:col-span-3">
          <Section title="feeds">
            <Feeds feeds={state?.feeds ?? []} />
          </Section>
        </div>

        <div className="md:col-span-3">
          <Section title="round trip times to hyperliquid, ms">
            {latency ? (
              <LatencyChart data={latency} />
            ) : (
              <p className="text-sm text-neutral-500">loading</p>
            )}
          </Section>
        </div>

        <div className="md:col-span-3">
          <Section title={`liquidations, last ${LIQ_HOURS}h`}>
            {liquidations ? (
              <LiquidationChart data={liquidations} />
            ) : (
              <p className="text-sm text-neutral-500">loading</p>
            )}
          </Section>
        </div>

        <div className="md:col-span-3">
          <Section title="universe">
            <p className="text-sm text-neutral-300">
              {state?.universe.length ? state.universe.join("  ") : "none"}
            </p>
          </Section>
        </div>
      </div>
    </div>
  );
}
