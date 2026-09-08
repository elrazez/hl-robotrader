import { useEffect, useRef } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import type { Latency } from "./types";

// WS ping is the one that matters minute to minute; REST is the hourly universe refresh,
// so it has far fewer points and lives on its own axis at the right.
const WS_COLOR = "#34d399";
const REST_COLOR = "#38bdf8";
const ACK_COLOR = "#fbbf24";
const HEIGHT = 160;

function pair(p50: number | null, p95: number | null) {
  return `p50 ${p50?.toFixed(1) ?? "-"} / p95 ${p95?.toFixed(1) ?? "-"} ms`;
}

/**
 * The two series are sampled at completely different rates (a ping every 20s, a REST
 * call every 60 minutes), so they cannot share an x array. uPlot needs one, so the union
 * of both timestamps is built and each series carries nulls where it has no sample;
 * `spanGaps` then draws each as one continuous line.
 */
export function align(data: Latency) {
  const stamps = [...new Set([...data.ws_ping, ...data.rest].map(([ms]) => ms))].sort(
    (a, b) => a - b,
  );
  const at = new Map(stamps.map((ms, i) => [ms, i]));
  const series = (points: [number, number][]) => {
    const out: (number | null)[] = new Array(stamps.length).fill(null);
    for (const [ms, value] of points) out[at.get(ms)!] = value;
    return out;
  };
  return [stamps.map((ms) => ms / 1000), series(data.ws_ping), series(data.rest)] as const;
}

export default function LatencyChart({ data }: { data: Latency }) {
  const host = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const node = host.current;
    if (!node || data.ws_ping.length + data.rest.length === 0) return;
    const plotData = align(data) as unknown as uPlot.AlignedData;
    const plot = new uPlot(
      {
        width: node.clientWidth || 600,
        height: HEIGHT,
        legend: { show: false },
        cursor: { y: false },
        series: [
          {},
          { label: "ws ping", stroke: WS_COLOR, width: 1.5, spanGaps: true, points: { show: false } },
          {
            label: "rest",
            stroke: REST_COLOR,
            width: 1.5,
            spanGaps: true,
            scale: "rest",
            points: { show: true, size: 4 },
          },
        ],
        axes: [
          { stroke: "#737373", grid: { stroke: "#26262699" } },
          { stroke: WS_COLOR, grid: { stroke: "#26262699" }, values: (_u, t) => t.map((v) => `${v}`) },
          { stroke: REST_COLOR, side: 1, scale: "rest", grid: { show: false } },
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

  if (data.ws_ping.length + data.rest.length === 0) {
    return <p className="text-sm text-neutral-500">no latency samples yet</p>;
  }

  return (
    <div>
      <div ref={host} />
      <div className="mt-3 flex flex-wrap gap-4 text-sm">
        <span className="flex items-center gap-2">
          <span className="inline-block h-2 w-2 rounded-sm" style={{ backgroundColor: WS_COLOR }} />
          <span className="text-neutral-400">ws ping</span>
          <span className="tabular-nums">{pair(data.ws_ping_p50, data.ws_ping_p95)}</span>
        </span>
        <span className="flex items-center gap-2">
          <span
            className="inline-block h-2 w-2 rounded-sm"
            style={{ backgroundColor: REST_COLOR }}
          />
          <span className="text-neutral-400">rest</span>
          <span className="tabular-nums">{pair(data.rest_p50, data.rest_p95)}</span>
        </span>
        {/* No line: the protocol keeps percentiles for submit-to-ack but no sample
            series behind them, so there is nothing to plot. Two numbers beside the two
            series is the whole of what is measured, and a dash means not measured. */}
        <span className="flex items-center gap-2">
          <span
            className="inline-block h-2 w-2 rounded-sm"
            style={{ backgroundColor: ACK_COLOR }}
          />
          <span className="text-neutral-400">submit to ack</span>
          <span className="tabular-nums">{pair(data.submit_ack_p50, data.submit_ack_p95)}</span>
        </span>
      </div>
    </div>
  );
}
