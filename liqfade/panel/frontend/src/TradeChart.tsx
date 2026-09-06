import { useEffect, useRef } from "react";
import {
  CandlestickSeries,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type SeriesMarker,
  type Time,
} from "lightweight-charts";
import type { TradeWindow } from "./types";

const HEIGHT = 320;
const UP = "#34d399";
const DOWN = "#f87171";

/**
 * One trade, with the cascade that caused it.
 *
 * The window starts ten minutes before the entry rather than at it, because under the
 * default fixed delay entry the flow that triggered the trade is already two minutes in
 * the past by the time the order goes in. A chart that began at the fill would show the
 * trade and hide its reason.
 *
 * Liquidations are drawn as markers under the bar they happened in, so the cascade is
 * visible as a cluster rather than as a number in a table.
 */
export default function TradeChart({ window: data }: { window: TradeWindow }) {
  const host = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const node = host.current;
    if (!node || data.candles.length === 0) return;

    const chart: IChartApi = createChart(node, {
      width: node.clientWidth || 600,
      height: HEIGHT,
      layout: { background: { color: "transparent" }, textColor: "#a1a1aa" },
      grid: { vertLines: { color: "#27272a" }, horzLines: { color: "#27272a" } },
      timeScale: { timeVisible: true, secondsVisible: false },
      rightPriceScale: { borderColor: "#3f3f46" },
    });
    const candles = chart.addSeries(CandlestickSeries, {
      upColor: UP,
      downColor: DOWN,
      borderVisible: false,
      wickUpColor: UP,
      wickDownColor: DOWN,
    });
    candles.setData(
      data.candles.map((bar) => ({
        time: (bar.ts_ms / 1000) as Time,
        open: bar.o,
        high: bar.h,
        low: bar.l,
        close: bar.c,
      })),
    );

    const marks: SeriesMarker<Time>[] = [
      ...data.liquidations.map(
        (liq): SeriesMarker<Time> => ({
          time: (Math.floor(liq.ts_ms / 60000) * 60) as Time,
          position: "belowBar",
          color: "#fbbf24",
          shape: "circle",
          text: `${liq.venue} ${liq.side}`,
        }),
      ),
      {
        time: (Math.floor(data.trade.entry_ms / 60000) * 60) as Time,
        position: data.trade.side === "long" ? "belowBar" : "aboveBar",
        color: "#38bdf8",
        shape: data.trade.side === "long" ? "arrowUp" : "arrowDown",
        text: `entry ${data.trade.entry_px.toPrecision(6)}`,
      } satisfies SeriesMarker<Time>,
      {
        time: (Math.floor(data.trade.exit_ms / 60000) * 60) as Time,
        position: data.trade.side === "long" ? "aboveBar" : "belowBar",
        color: data.trade.net_pnl >= 0 ? UP : DOWN,
        shape: data.trade.side === "long" ? "arrowDown" : "arrowUp",
        text: `${data.trade.exit_reason} ${data.trade.exit_px.toPrecision(6)}`,
      } satisfies SeriesMarker<Time>,
    ].sort((a, b) => (a.time as number) - (b.time as number));
    createSeriesMarkers(candles, marks);
    chart.timeScale().fitContent();

    const resize = () => chart.applyOptions({ width: node.clientWidth || 600 });
    window.addEventListener("resize", resize);
    return () => {
      window.removeEventListener("resize", resize);
      chart.remove();
    };
  }, [data]);

  if (data.candles.length === 0) {
    return (
      <p className="text-sm text-neutral-500">
        no candles in the lake for this window. The run's trades are still listed; the chart
        needs the tape the backtest read, which has to be synced locally.
      </p>
    );
  }
  return <div ref={host} />;
}
