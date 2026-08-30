import { useEffect, useRef } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import type { Liquidations } from "./types";

// One colour per venue, stable across renders so the eye can follow a venue over time.
const COLORS: Record<string, string> = {
  hyperliquid: "#34d399",
  bybit: "#f59e0b",
  binance: "#eab308",
  okx: "#38bdf8",
};
const FALLBACK = "#64748b";
const HEIGHT = 200;

export function color(venue: string): string {
  return COLORS[venue] ?? FALLBACK;
}

export function compact(usd: number): string {
  if (usd >= 1e9) return `$${(usd / 1e9).toFixed(1)}B`;
  if (usd >= 1e6) return `$${(usd / 1e6).toFixed(1)}M`;
  if (usd >= 1e3) return `$${(usd / 1e3).toFixed(1)}k`;
  return `$${usd.toFixed(0)}`;
}

/**
 * Stacked bars: series i holds the running total of venues 0..i, painted tallest first
 * so the shorter bars land on top. That is uPlot's usual stacking trick and it avoids a
 * second charting library for one chart.
 */
function build(data: Liquidations) {
  const venues = Object.keys(data.venues);
  const xs = data.hour_ms.map((ms) => ms / 1000);
  const cumulative = venues.map((_, upto) =>
    xs.map((_, hour) =>
      venues
        .slice(0, upto + 1)
        .reduce((sum, venue) => sum + (data.venues[venue][hour] ?? 0), 0),
    ),
  );
  const order = venues.map((_, i) => i).reverse();
  const bars = uPlot.paths.bars!({ size: [0.7, 40] });
  return {
    plotData: [xs, ...order.map((i) => cumulative[i])] as uPlot.AlignedData,
    series: [
      {},
      ...order.map((i) => ({
        label: venues[i],
        fill: color(venues[i]),
        stroke: color(venues[i]),
        width: 0,
        // Bars only: uPlot would otherwise dot every quiet hour along the baseline.
        points: { show: false },
        paths: bars,
      })),
    ],
  };
}

export default function LiquidationChart({ data }: { data: Liquidations }) {
  const host = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const node = host.current;
    if (!node || Object.keys(data.venues).length === 0) return;
    const { plotData, series } = build(data);
    const plot = new uPlot(
      {
        width: node.clientWidth || 600,
        height: HEIGHT,
        legend: { show: false },
        cursor: { y: false },
        series,
        axes: [
          { stroke: "#737373", grid: { stroke: "#26262699" } },
          {
            stroke: "#737373",
            grid: { stroke: "#26262699" },
            values: (_u, ticks) => ticks.map((t) => compact(t)),
          },
        ],
        scales: { y: { range: (_u, _min, max) => [0, max || 1] } },
      },
      plotData,
      node,
    );
    const resize = () => plot.setSize({ width: node.clientWidth || 600, height: HEIGHT });
    window.addEventListener("resize", resize);
    return () => {
      window.removeEventListener("resize", resize);
      plot.destroy();
    };
  }, [data]);

  if (Object.keys(data.venues).length === 0) {
    return <p className="text-sm text-neutral-500">no liquidations recorded yet</p>;
  }

  return (
    <div>
      <div ref={host} />
      <div className="mt-3 flex flex-wrap gap-4 text-sm">
        {Object.entries(data.totals).map(([venue, total]) => (
          <span key={venue} className="flex items-center gap-2">
            <span
              className="inline-block h-2 w-2 rounded-sm"
              style={{ backgroundColor: color(venue) }}
            />
            <span className="text-neutral-400">{venue}</span>
            <span className="tabular-nums">{compact(total)}</span>
            <span className="text-neutral-600">({data.events?.[venue] ?? 0})</span>
          </span>
        ))}
      </div>
    </div>
  );
}
