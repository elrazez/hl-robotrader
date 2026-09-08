import { useCallback, useEffect, useState } from "react";
import { fetchFills, fetchLiveEquity } from "./api";
import EquityChart from "./EquityChart";
import type { EquityCurve, FillRow, FillsPage } from "./types";
import { Row, Section, pnlTone, stamp, usd } from "./ui";

const PAGE = 100;

export interface FillFilters {
  coin: string;
  side: "" | "buy" | "sell";
  start: string;
  end: string;
}

const EMPTY: FillFilters = { coin: "", side: "", start: "", end: "" };

/** A `YYYY-MM-DD` box to ms UTC. Empty stays empty; the endpoint treats it as unbounded. */
function dayToMs(day: string, endOfDay: boolean): number | null {
  if (!day) return null;
  const at = Date.parse(`${day}T${endOfDay ? "23:59:59.999" : "00:00:00.000"}Z`);
  return Number.isNaN(at) ? null : at;
}

export function filterQuery(filters: FillFilters): string {
  const query = new URLSearchParams();
  if (filters.coin) query.set("coin", filters.coin.toUpperCase());
  if (filters.side) query.set("side", filters.side);
  const start = dayToMs(filters.start, false);
  const end = dayToMs(filters.end, true);
  if (start !== null) query.set("start_ms", String(start));
  if (end !== null) query.set("end_ms", String(end));
  return query.toString();
}

function FillTable({ fills }: { fills: FillRow[] }) {
  if (fills.length === 0) return <p className="text-sm text-neutral-500">no fills match</p>;
  return (
    <table className="w-full text-sm [&_td:not(:first-child)]:pl-4 [&_th:not(:first-child)]:pl-4">
      <thead className="text-left text-xs uppercase text-neutral-500">
        <tr>
          <th className="pb-1 font-medium">time (UTC)</th>
          <th className="pb-1 font-medium">coin</th>
          <th className="pb-1 font-medium">side</th>
          <th className="pb-1 text-right font-medium">px</th>
          <th className="pb-1 text-right font-medium">size</th>
          <th className="pb-1 text-right font-medium">notional</th>
          <th className="pb-1 text-right font-medium">fee</th>
          <th className="pb-1 font-medium">liq</th>
          <th className="pb-1 font-medium">tag</th>
          <th className="pb-1 font-medium">cloid</th>
        </tr>
      </thead>
      <tbody>
        {fills.map((f) => (
          <tr key={f.id} className="border-t border-neutral-800/60">
            <td className="py-1 tabular-nums text-neutral-400">{stamp(f.ts_ms)}</td>
            <td className="py-1">{f.coin}</td>
            <td className={`py-1 ${f.side === "buy" ? "text-emerald-400" : "text-red-400"}`}>
              {f.side}
            </td>
            <td className="py-1 text-right tabular-nums">{usd(f.px, 4)}</td>
            <td className="py-1 text-right tabular-nums">{f.size}</td>
            <td className="py-1 text-right tabular-nums">{usd(f.notional_usd)}</td>
            <td className="py-1 text-right tabular-nums text-neutral-400">{usd(f.fee, 4)}</td>
            {/* Maker or taker decides which fee tier this row paid, which is the whole
                cost model. Worth a column of its own rather than an inference from the fee. */}
            <td className="py-1 text-xs text-neutral-400">{f.maker ? "maker" : "taker"}</td>
            <td className="py-1 text-xs">
              {f.tag}
              {f.reduce_only ? <span className="ml-1 text-sky-400">RO</span> : null}
            </td>
            <td className="py-1 font-mono text-xs text-neutral-500">{f.cloid.slice(0, 10)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const INPUT =
  "rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-sm outline-none focus:border-neutral-500";

/**
 * Every fill the loop has taken, from the registry the loop already writes, plus the
 * realized cash curve over exactly the rows the filters select. One store, so the chart
 * and the table can never tell different stories.
 */
export default function Fills() {
  const [filters, setFilters] = useState<FillFilters>(EMPTY);
  const [applied, setApplied] = useState<FillFilters>(EMPTY);
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<FillsPage | null>(null);
  const [curve, setCurve] = useState<EquityCurve | null>(null);

  const load = useCallback(async () => {
    const query = filterQuery(applied);
    setPage(await fetchFills(query, offset, PAGE));
    setCurve(await fetchLiveEquity(query));
  }, [applied, offset]);

  useEffect(() => {
    load();
  }, [load]);

  function apply(next: FillFilters) {
    setFilters(next);
    setApplied(next);
    setOffset(0); // a new filter starts at its own first page, not page four of the old one
  }

  const total = page?.total ?? 0;
  const realized = curve?.equity.at(-1) ?? 0;
  const fees = (page?.fills ?? []).reduce((sum, f) => sum + f.fee, 0);

  return (
    <div className="grid gap-4">
      <Section
        title="filters"
        right={
          <button
            onClick={() => apply(EMPTY)}
            className="text-xs text-neutral-500 hover:text-neutral-300"
          >
            clear
          </button>
        }
      >
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-xs text-neutral-500">
            coin
            <input
              className={INPUT}
              placeholder="all"
              value={filters.coin}
              onChange={(e) => setFilters({ ...filters, coin: e.target.value })}
              onBlur={() => apply(filters)}
              onKeyDown={(e) => e.key === "Enter" && apply(filters)}
            />
          </label>
          <label className="flex flex-col gap-1 text-xs text-neutral-500">
            side
            <select
              className={INPUT}
              value={filters.side}
              onChange={(e) => apply({ ...filters, side: e.target.value as FillFilters["side"] })}
            >
              <option value="">both</option>
              <option value="buy">buy</option>
              <option value="sell">sell</option>
            </select>
          </label>
          {/* Native date inputs: the browser already has a calendar and a locale. */}
          <label className="flex flex-col gap-1 text-xs text-neutral-500">
            from (UTC)
            <input
              type="date"
              className={INPUT}
              value={filters.start}
              onChange={(e) => apply({ ...filters, start: e.target.value })}
            />
          </label>
          <label className="flex flex-col gap-1 text-xs text-neutral-500">
            to (UTC)
            <input
              type="date"
              className={INPUT}
              value={filters.end}
              onChange={(e) => apply({ ...filters, end: e.target.value })}
            />
          </label>
        </div>
      </Section>

      <div className="grid gap-4 md:grid-cols-3">
        <Section title="matched">
          <Row label="fills" value={String(total)} />
          <Row label="fees on this page" value={usd(fees, 4)} />
        </Section>
        <div className="md:col-span-2">
          <Section title="realized pnl, cumulative">
            <Row label="net of fees" value={usd(realized)} tone={pnlTone(realized)} />
            <p className="mt-1 text-xs text-neutral-500">
              Running average cost per coin: a fill that reduces a position realizes the move
              against that average, an entry costs only its fee. Open positions are not in
              this number; the live page has their unrealized.
            </p>
          </Section>
        </div>
      </div>

      <Section title="equity curve">
        {curve ? <EquityChart data={curve} /> : <p className="text-sm text-neutral-500">loading</p>}
      </Section>

      <Section
        title="fills"
        right={
          <span className="flex items-center gap-2 text-xs text-neutral-500">
            <button
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE))}
              className="rounded px-2 py-0.5 hover:text-neutral-300 disabled:opacity-30"
            >
              prev
            </button>
            <span className="tabular-nums">
              {total === 0 ? 0 : offset + 1}-{Math.min(offset + PAGE, total)} of {total}
            </span>
            <button
              disabled={offset + PAGE >= total}
              onClick={() => setOffset(offset + PAGE)}
              className="rounded px-2 py-0.5 hover:text-neutral-300 disabled:opacity-30"
            >
              next
            </button>
          </span>
        }
      >
        <FillTable fills={page?.fills ?? []} />
      </Section>
    </div>
  );
}
