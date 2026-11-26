import { useEffect, useRef, useState } from "react";
import { buildEChartsOption, readThemeTokens } from "./chartOptions";
import { ChartSpec } from "./chartSpec";

let echartsPromise: Promise<typeof import("echarts/core")> | null = null;

function loadECharts() {
  if (!echartsPromise) {
    echartsPromise = Promise.all([
      import("echarts/core"),
      import("echarts/charts"),
      import("echarts/components"),
      import("echarts/renderers"),
    ]).then(([core, charts, components, renderers]) => {
      core.use([
        charts.BarChart,
        charts.LineChart,
        charts.PieChart,
        charts.ScatterChart,
        charts.RadarChart,
        charts.FunnelChart,
        components.GridComponent,
        components.TooltipComponent,
        components.LegendComponent,
        components.TitleComponent,
        components.DataZoomComponent,
        renderers.CanvasRenderer,
      ]);
      return core;
    });
  }
  return echartsPromise;
}

interface EChartsHandle {
  resize: () => void;
  dispose: () => void;
  setOption: (opt: Record<string, unknown>, notMerge?: boolean) => void;
  getDataURL: (opts: Record<string, unknown>) => string;
}

export function useECharts(spec: ChartSpec) {
  const containerRef = useRef<HTMLDivElement>(null);
  const instanceRef = useRef<EChartsHandle | null>(null);
  const [themeTick, setThemeTick] = useState(0);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const observer = new MutationObserver(() => setThemeTick((t) => t + 1));
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    let alive = true;
    const el = containerRef.current;
    if (!el) return;
    let ro: ResizeObserver | null = null;

    loadECharts().then((echarts) => {
      if (!alive || !containerRef.current) return;
      if (!instanceRef.current) {
        instanceRef.current = echarts.init(containerRef.current, undefined, { renderer: "canvas" });
      }
      instanceRef.current.setOption(buildEChartsOption(spec), true);
      setReady(true);

      ro = new ResizeObserver(() => instanceRef.current?.resize());
      ro.observe(containerRef.current);
    });

    return () => {
      alive = false;
      ro?.disconnect();
    };
  }, [spec, themeTick]);

  useEffect(() => {
    return () => {
      instanceRef.current?.dispose();
      instanceRef.current = null;
    };
  }, []);

  const exportPng = () => {
    const chart = instanceRef.current;
    if (!chart) return;
    const theme = readThemeTokens();
    const url = chart.getDataURL({ type: "png", pixelRatio: 2, backgroundColor: theme.surface });
    const a = document.createElement("a");
    a.href = url;
    a.download = `${(spec.title || "chart").replace(/[^\w\u4e00-\u9fff-]+/g, "_")}.png`;
    a.click();
  };

  return { containerRef, ready, exportPng };
}
