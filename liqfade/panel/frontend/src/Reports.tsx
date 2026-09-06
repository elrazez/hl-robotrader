import { useCallback, useEffect, useState } from "react";
import { fetchEquity, fetchReport, fetchReports, fetchTradeWindow, fetchTrades } from "./api";
import EquityChart from "./EquityChart";
import TradeChart from "./TradeChart";
import type { EquityCurve, ReportDetail, ReportRow, TradeWindow, TradesPage } from "./types";

const PAGE = 200;

function stamp(ms: number | null | undefined) {
  if (!ms) return "-";
  return new Date(ms).toISOString().slice(0, 16).replace("T", " ");
}

function usd(value: number | null | undefined) {
  if (value === null || value === undefined) return "-";
  return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function pct(value: number | null | undefined) {
  if (value === null || value === undefined) return "-";
  return `${(100 * value).toFixed(1)}%`;
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-neutral-800 bg-neutral-900/40 p-4">
      <h2 className="mb-3 text-xs font-semibold uppercase tracking-wider text-neutral-500">
        {title}
      </h2>
      {children}
    </section>
  );
}

/**
 * The trigger is the first thing shown about a run, everywhere it appears.
 *
 * A backtest on `exact` reads Hyperliquid liquidations that arrive a day late, so its
 * numbers are a ceiling on an edge nobody can trade live. Anyone reading the table has to
 * know that before they read the PnL beside it.
 */
function Trigger({ value }: { value: string | null }) {
  if (!value) return <span className="text-neutral-500">-</span>;
  const tone = value === "exact" ? "text-amber-300" : "text-neutral-300";
  return (
    <span className={tone} title={value === "exact" ? "backtest only: not visible live" : ""}>
      {value}
      {value === "exact" ? " (ceiling)" : ""}
    </span>
  );
}

function RunList({
  runs,
  selected,
  onSelect,
}: {
  runs: ReportRow[];
  selected: string | null;
  onSelect: (runId: string) => void;
}) {
  if (runs.length === 0) {
    return (
      <p className="text-sm text-neutral-500">
        no runs yet. `liqfade backtest --start ... --end ...` writes one into reports/.
      </p>
    );
  }
  return (
    <div className="max-h-[70vh] overflow-y-auto">
      <table className="w-full text-sm">
        <thead className="text-left text-xs uppercase text-neutral-500">
          <tr>
            <th className="pb-1 font-medium">run</th>
            <th className="pb-1 font-medium">trigger</th>
            <th className="pb-1 text-right font-medium">trades</th>
            <th className="pb-1 text-right font-medium">net</th>
            <th className="pb-1 text-right font-medium">bps</th>
            <th className="pb-1 text-right font-medium">hit</th>
            <th className="pb-1 text-right font-medium">top 5</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr
              key={run.run_id}
              onClick={() => onSelect(run.run_id)}
              className={`cursor-pointer border-t border-neutral-800/60 hover:bg-neutral-800/40 ${
                run.run_id === selected ? "bg-neutral-800/60" : ""
              }`}
            >
              <td className="py-1">
                <div className="font-mono text-xs">{run.run_id}</div>
                <div className="text-xs text-neutral-500">
                  {stamp(run.start_ms)} to {stamp(run.end_ms)}
                  {run.latency_fragile ? (
                    <span className="ml-2 text-red-400">latency fragile</span>
                  ) : null}
                  {run.error ? <span className="ml-2 text-red-400">{run.error}</span> : null}
                </div>
              </td>
              <td className="py-1">
                <Trigger value={run.trigger} />
              </td>
              <td className="py-1 text-right tabular-nums">{run.trades ?? "-"}</td>
              <td
                className={`py-1 text-right tabular-nums ${
                  (run.net_pnl ?? 0) < 0 ? "text-red-400" : ""
                }`}
              >
                {usd(run.net_pnl)}
              </td>
              <td className="py-1 text-right tabular-nums">
                {run.avg_net_bps === null || run.avg_net_bps === undefined
                  ? "-"
                  : run.avg_net_bps.toFixed(1)}
              </td>
              <td className="py-1 text-right tabular-nums">{pct(run.hit_rate)}</td>
              <td className="py-1 text-right tabular-nums">{pct(run.top5_pnl_share)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TradeTable({
  page,
  onPick,
  picked,
}: {
  page: TradesPage;
  onPick: (index: number) => void;
  picked: number | null;
}) {
  if (page.trades.length === 0) {
    return <p className="text-sm text-neutral-500">this run produced no trades</p>;
  }
  return (
    <div className="max-h-[50vh] overflow-y-auto">
      <table className="w-full text-sm">
        <thead className="text-left text-xs uppercase text-neutral-500">
          <tr>
            <th className="pb-1 font-medium">coin</th>
            <th className="pb-1 font-medium">side</th>
            <th className="pb-1 font-medium">entry</th>
            <th className="pb-1 text-right font-medium">hold</th>
            <th className="pb-1 font-medium">exit</th>
            <th className="pb-1 text-right font-medium">net</th>
            <th className="pb-1 text-right font-medium">bps</th>
          </tr>
        </thead>
        <tbody>
          {page.trades.map((trade) => (
            <tr
              key={trade.index}
              onClick={() => onPick(trade.index)}
              className={`cursor-pointer border-t border-neutral-800/60 hover:bg-neutral-800/40 ${
                trade.index === picked ? "bg-neutral-800/60" : ""
              }`}
            >
              <td className="py-1">{trade.coin}</td>
              <td className="py-1">{trade.side}</td>
              <td className="py-1 text-xs text-neutral-400">{stamp(trade.entry_ms)}</td>
              <td className="py-1 text-right tabular-nums">{Math.round(trade.hold_s)}s</td>
              <td className="py-1">{trade.exit_reason}</td>
              <td
                className={`py-1 text-right tabular-nums ${
                  trade.net_pnl < 0 ? "text-red-400" : "text-emerald-400"
                }`}
              >
                {usd(trade.net_pnl)}
              </td>
              <td className="py-1 text-right tabular-nums">{trade.net_bps.toFixed(1)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {page.total > page.trades.length ? (
        <p className="pt-2 text-xs text-neutral-500">
          showing {page.trades.length} of {page.total}
        </p>
      ) : null}
    </div>
  );
}

export default function Reports() {
  const [runs, setRuns] = useState<ReportRow[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<ReportDetail | null>(null);
  const [trades, setTrades] = useState<TradesPage | null>(null);
  const [equity, setEquity] = useState<EquityCurve | null>(null);
  const [picked, setPicked] = useState<number | null>(null);
  const [window_, setWindow] = useState<TradeWindow | null>(null);

  useEffect(() => {
    fetchReports().then((rows) => {
      setRuns(rows);
      if (rows.length > 0) setSelected((current) => current ?? rows[0].run_id);
    });
  }, []);

  const load = useCallback(async (runId: string) => {
    setPicked(null);
    setWindow(null);
    setDetail(await fetchReport(runId));
    setTrades(await fetchTrades(runId, 0, PAGE));
    setEquity(await fetchEquity(runId));
  }, []);

  useEffect(() => {
    if (selected) load(selected);
  }, [selected, load]);

  useEffect(() => {
    if (selected === null || picked === null) return;
    fetchTradeWindow(selected, picked).then(setWindow);
  }, [selected, picked]);

  return (
    <div className="grid gap-4">
      <Section title="backtest runs">
        <RunList runs={runs} selected={selected} onSelect={setSelected} />
      </Section>

      {selected && equity ? (
        <Section title="cumulative net PnL, USD">
          <EquityChart data={equity} />
        </Section>
      ) : null}

      {trades ? (
        <Section title="trades">
          <TradeTable page={trades} onPick={setPicked} picked={picked} />
        </Section>
      ) : null}

      {window_ ? (
        <Section
          title={`${window_.trade.coin} ${window_.trade.side}, ${stamp(
            window_.trade.entry_ms,
          )} to ${stamp(window_.trade.exit_ms)}`}
        >
          <TradeChart window={window_} />
        </Section>
      ) : null}

      {detail ? (
        <Section title="report.md">
          <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap font-mono text-xs text-neutral-300">
            {detail.markdown}
          </pre>
        </Section>
      ) : null}
    </div>
  );
}
