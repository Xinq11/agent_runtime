import React from "react";
import { Download } from "lucide-react";
import { ChartSpec, tryParseChartSpec } from "./chartSpec";
import { useECharts } from "./useECharts";

export type { ChartSeries, ChartSpec } from "./chartSpec";
export { tryParseChartSpec };

export const ChartBlock: React.FC<{ spec: ChartSpec }> = ({ spec }) => {
  const { containerRef, ready, exportPng } = useECharts(spec);

  return (
    <div
      className="my-3 rounded-xl border p-4"
      style={{ background: "var(--surface)", borderColor: "var(--border)" }}
    >
      <div className="flex items-center justify-between gap-2 mb-2">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-[13.5px] font-semibold truncate">
            {spec.title || "数据可视化图表"}
          </span>
          {spec.unit && (
            <span className="font-mono text-[11px] shrink-0" style={{ color: "var(--ink-muted)" }}>
              ({spec.unit})
            </span>
          )}
        </div>
        {ready && (
          <button
            type="button"
            onClick={exportPng}
            title="导出高清 PNG 图片"
            className="interactive-btn inline-flex items-center gap-1 px-2 py-1 rounded-md border text-[11px] shrink-0"
            style={{
              background: "var(--bg)",
              borderColor: "var(--border)",
              color: "var(--ink-secondary)",
            }}
          >
            <Download size={11.5} />
            导出 PNG
          </button>
        )}
      </div>

      <div
        ref={containerRef}
        className="w-full"
        style={{ height: spec.type === "pie" || spec.type === "radar" ? 260 : 272 }}
      />
    </div>
  );
};
