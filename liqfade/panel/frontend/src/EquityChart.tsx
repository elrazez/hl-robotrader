import { useEffect, useRef } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import type { EquityCurve } from "./types";

// Cumulative net PnL above, drawdown below it in red. Two series on one axis rather than
// two charts, because the question a reader has is where in the curve the drawdown was.
const EQUITY_COLOR = "#34d399";
const DRAWDOWN_COLOR = "#f87171";
const HEIGHT = 200;

export default function EquityChart({ data }: { data: EquityCurve }) {
  const host = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const node = host.current;
    if (!node || data.ts_ms.length === 0) return;
    const plotData = [
      data.ts_ms.map((ms) => ms / 1000),
      data.equity,
      data.drawdown,
    ] as unknown as uPlot.AlignedData;

    const plot = new uPlot(
      {
        width: node.clientWidth || 600,
        height: HEIGHT,
        cursor: { drag: { x: true, y: false } },
        legend: { live: true },
        series: [
          {},
          { label: "net PnL", stroke: EQUITY_COLOR, width: 2 },
          { label: "drawdown", stroke: DRAWDOWN_COLOR, width: 1, fill: "rgba(248,113,113,0.12)" },
        ],
        axes: [
          { stroke: "#71717a", grid: { stroke: "#27272a" } },
          { stroke: "#71717a", grid: { stroke: "#27272a" } },
        ],
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

  if (data.ts_ms.length === 0) {
    return <p className="text-sm text-neutral-500">this run produced no trades</p>;
  }
  return <div ref={host} />;
}
