import { useCallback, useEffect, useState } from "react";
import { fetchLiveState } from "./api";
import type { StateResult } from "./api";
import type { OrderState, PositionState } from "./types";
import { Banner, Row, Section, pnlTone, stamp, usd } from "./ui";

const POLL_MS = 1000;

function Positions({ positions }: { positions: PositionState[] }) {
  if (positions.length === 0) return <p className="text-sm text-neutral-500">flat</p>;
  return (
    <table className="w-full text-sm">
      <thead className="text-left text-xs uppercase text-neutral-500">
        <tr>
          <th className="pb-1 font-medium">coin</th>
          <th className="pb-1 font-medium">side</th>
          <th className="pb-1 text-right font-medium">size</th>
          <th className="pb-1 text-right font-medium">entry</th>
          <th className="pb-1 text-right font-medium">mark</th>
          <th className="pb-1 text-right font-medium">notional</th>
          <th className="pb-1 text-right font-medium">unrealized</th>
          <th className="pb-1 text-right font-medium">opened</th>
        </tr>
      </thead>
      <tbody>
        {positions.map((p) => (
          <tr key={p.coin} className="border-t border-neutral-800/60">
            <td className="py-1">{p.coin}</td>
            <td className={`py-1 ${p.side === "long" ? "text-emerald-400" : "text-red-400"}`}>
              {p.side}
            </td>
            <td className="py-1 text-right tabular-nums">{p.size}</td>
            <td className="py-1 text-right tabular-nums">{usd(p.entry_px, 4)}</td>
            <td className="py-1 text-right tabular-nums">{usd(p.mark_px, 4)}</td>
            <td className="py-1 text-right tabular-nums">{usd(p.notional_usd)}</td>
            <td className={`py-1 text-right tabular-nums ${pnlTone(p.unrealized_pnl)}`}>
              {usd(p.unrealized_pnl)}
            </td>
            <td className="py-1 text-right tabular-nums text-neutral-400">{stamp(p.opened_ms)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Orders({ orders }: { orders: OrderState[] }) {
  if (orders.length === 0) return <p className="text-sm text-neutral-500">no resting orders</p>;
  return (
    <table className="w-full text-sm">
      <thead className="text-left text-xs uppercase text-neutral-500">
        <tr>
          <th className="pb-1 font-medium">coin</th>
          <th className="pb-1 font-medium">side</th>
          <th className="pb-1 text-right font-medium">px</th>
          <th className="pb-1 text-right font-medium">size</th>
          <th className="pb-1 text-right font-medium">filled</th>
          <th className="pb-1 font-medium">tif</th>
          <th className="pb-1 font-medium">status</th>
          <th className="pb-1 font-medium">cloid</th>
          <th className="pb-1 text-right font-medium">submitted</th>
        </tr>
      </thead>
      <tbody>
        {orders.map((o) => (
          <tr key={o.cloid} className="border-t border-neutral-800/60">
            <td className="py-1">
              {o.coin}
              {o.reduce_only ? <span className="ml-1 text-xs text-sky-400">RO</span> : null}
            </td>
            <td className={`py-1 ${o.side === "buy" ? "text-emerald-400" : "text-red-400"}`}>
              {o.side}
            </td>
            <td className="py-1 text-right tabular-nums">{usd(o.px, 4)}</td>
            <td className="py-1 text-right tabular-nums">{o.size}</td>
            <td className="py-1 text-right tabular-nums">{o.filled}</td>
            <td className="py-1">{o.tif}</td>
            <td className="py-1">{o.status}</td>
            <td className="py-1 font-mono text-xs text-neutral-500">{o.cloid.slice(0, 10)}</td>
            <td className="py-1 text-right tabular-nums text-neutral-400">
              {stamp(o.submitted_ms)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * The trading loop's page: what it holds, what it has resting, and why it is or is not
 * trading. Everything here comes from the loop's own state file, so the recorder being
 * healthy says nothing about whether this page has data.
 */
export default function Live({ onUnauthorized }: { onUnauthorized: () => void }) {
  const [result, setResult] = useState<StateResult | null>(null);

  const poll = useCallback(async () => {
    const next = await fetchLiveState();
    if (next.unauthorized) {
      onUnauthorized();
      return;
    }
    setResult(next);
  }, [onUnauthorized]);

  useEffect(() => {
    poll();
    const timer = setInterval(poll, POLL_MS);
    return () => clearInterval(timer);
  }, [poll]);

  const state = result?.state ?? null;
  const risk = state?.risk;
  // Read only whenever the snapshot is not fresh. A control sent against numbers this
  // old would be a decision taken on a picture of the past.
  const readOnly = result === null || result.stale || state === null;

  return (
    <div>
      {result?.error && (
        <Banner kind={state ? "warn" : "error"}>
          {result.error}
          {state ? ". Showing the last snapshot; controls are read only." : ""}
        </Banner>
      )}
      {state?.risk.killed && (
        <Banner kind="error">
          killed. The loop has fired its kill switch and will not resume without a restart.
        </Banner>
      )}

      <div className="grid gap-4 md:grid-cols-3">
        <Section title="process">
          <Row label="process" value={state?.proc ?? "-"} />
          <Row label="env" value={state?.env ?? "-"} />
          <Row
            label="live trading"
            value={state?.live_trading ? "ENABLED" : "off"}
            tone={state?.live_trading ? "text-amber-300" : undefined}
          />
          <Row
            label="uptime"
            value={state ? `${Math.round(state.uptime_s / 60)} min` : "-"}
          />
          <Row
            label="snapshot age"
            value={state ? `${(state.age_s ?? 0).toFixed(1)} s` : "-"}
            tone={readOnly ? "text-amber-300" : undefined}
          />
          <Row label="universe" value={String(state?.universe.length ?? 0)} />
        </Section>

        <Section title="risk">
          <Row
            label="paused"
            value={String(risk?.paused ?? "-")}
            tone={risk?.paused ? "text-amber-300" : undefined}
          />
          <Row
            label="halted"
            value={risk?.halt_reason ?? "no"}
            tone={risk?.halt_reason ? "text-amber-300" : undefined}
          />
          <Row
            label="daily pnl"
            value={usd(risk?.daily_realized_pnl)}
            tone={risk ? pnlTone(risk.daily_realized_pnl) : undefined}
          />
          <Row label="equity" value={usd(risk?.equity)} />
          <Row
            label="loss streak"
            value={String(risk?.consecutive_losses ?? "-")}
            tone={risk && risk.consecutive_losses > 0 ? "text-amber-300" : undefined}
          />
          {/* None until the loop tracks the address action budget. A dash here is
              "not measured", which is a different thing from "none left". */}
          <Row label="actions remaining" value={risk?.actions_remaining?.toString() ?? "-"} />
        </Section>

        <Section title="cooldowns">
          {risk && Object.keys(risk.coin_cooldowns).length > 0 ? (
            Object.entries(risk.coin_cooldowns).map(([coin, until]) => (
              <Row key={coin} label={coin} value={stamp(until)} />
            ))
          ) : (
            <p className="text-sm text-neutral-500">none</p>
          )}
        </Section>

        <div className="md:col-span-3">
          <Section title="positions">
            <Positions positions={state?.positions ?? []} />
          </Section>
        </div>

        <div className="md:col-span-3">
          <Section title="open orders">
            <Orders orders={state?.orders ?? []} />
          </Section>
        </div>
      </div>
    </div>
  );
}
