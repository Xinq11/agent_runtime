import React from "react";
import {
  Activity,
  AlertCircle,
  Clock,
  Cpu,
  GitBranch,
  Layers,
  PauseCircle,
  Wrench,
} from "lucide-react";
import { compactAttrSummary, formatMs, WaterfallModel } from "./timeline";

interface WaterfallListProps {
  waterfall: WaterfallModel;
  hasSpans: boolean;
  selectedSpanId: string | null;
  onToggleSpan: (spanId: string) => void;
}

function spanVisual(op: string, status: string) {
  if (status === "error") {
    return { color: "#B83B3B", bg: "var(--danger-soft)", tag: "ERROR", Icon: AlertCircle };
  }
  if (status === "paused") {
    return { color: "#B36B14", bg: "var(--warn-soft)", tag: "PAUSED", Icon: PauseCircle };
  }
  if (op.startsWith("model")) {
    return { color: "#D97757", bg: "var(--accent-soft)", tag: "LLM", Icon: Cpu };
  }
  if (op.startsWith("subagent:")) {
    return { color: "#1F7A5A", bg: "var(--ok-soft)", tag: "SUB", Icon: GitBranch };
  }
  if (op.startsWith("tool:")) {
    return { color: "#2F6F9F", bg: "rgba(47, 111, 159, 0.12)", tag: "TOOL", Icon: Wrench };
  }
  if (op === "before_model" || op === "after_agent") {
    return { color: "#7D786E", bg: "var(--surface-subtle)", tag: "HOOK", Icon: Layers };
  }
  return { color: "#5E5B54", bg: "var(--surface-subtle)", tag: "ROOT", Icon: Activity };
}

export const WaterfallList: React.FC<WaterfallListProps> = ({
  waterfall,
  hasSpans,
  selectedSpanId,
  onToggleSpan,
}) => {
  return (
    <div
      className="rounded-xl border overflow-hidden"
      style={{ background: "var(--surface)", borderColor: "var(--border)" }}
    >
      {!hasSpans ? (
        <div className="px-4 py-10 text-center space-y-1.5">
          <Clock size={20} className="mx-auto opacity-40" />
          <div className="text-[13px] font-medium">暂无执行轨迹</div>
          <div className="text-[12px]" style={{ color: "var(--ink-muted)" }}>
            发送一条消息后，这里将实时展开 before_model、LLM、子 Agent 与工具的完整耗时瀑布图
          </div>
        </div>
      ) : (
        <>
          <div
            className="px-3 py-2 border-b flex items-center justify-between font-mono text-[10px]"
            style={{ borderColor: "var(--border)", background: "var(--surface-subtle)", color: "var(--ink-muted)" }}
          >
            <span>0ms</span>
            <span>{formatMs(Math.round(waterfall.totalMs * 0.25))}</span>
            <span>{formatMs(Math.round(waterfall.totalMs * 0.5))}</span>
            <span>{formatMs(Math.round(waterfall.totalMs * 0.75))}</span>
            <span>{formatMs(waterfall.totalMs)}</span>
          </div>

          {waterfall.rootSpan && (
            <button
              type="button"
              onClick={() => onToggleSpan(waterfall.rootSpan!.span_id)}
              className="w-full text-left px-3 py-2 border-b flex items-center justify-between gap-2 transition-colors"
              style={{
                borderColor: "var(--border)",
                background: selectedSpanId === waterfall.rootSpan.span_id ? "var(--surface-hover)" : "var(--bg)",
              }}
            >
              <div className="flex items-center gap-1.5 min-w-0">
                <Activity size={12} style={{ color: "var(--ink-secondary)" }} className="shrink-0" />
                <span className="font-mono text-[11.5px] font-semibold truncate">
                  {waterfall.rootSpan.operation_name}
                </span>
                <span
                  className="font-mono text-[9.5px] px-1.5 py-0.5 rounded shrink-0"
                  style={{ background: "var(--surface-subtle)", color: "var(--ink-muted)" }}
                >
                  总链路
                </span>
              </div>
              <div className="flex items-center gap-2 shrink-0 font-mono text-[11px]">
                <span className="font-semibold" style={{ color: "var(--ink)" }}>
                  {formatMs(waterfall.totalMs)}
                </span>
                <span
                  className="px-1.5 py-0.5 rounded text-[10px] font-semibold"
                  style={{ background: "var(--surface-subtle)", color: "var(--ink-secondary)" }}
                >
                  合计 100.0%
                </span>
              </div>
            </button>
          )}

          <div className="divide-y" style={{ borderColor: "var(--border)" }}>
            {waterfall.rows.map(({ sp, depth, sharePct, shareLabel, isSubOfParent }) => {
              const vis = spanVisual(sp.operation_name, sp.status);
              const Icon = vis.Icon;
              const offsetMs = Math.max(0, sp.start_ms - waterfall.minStart);
              const leftPct = Math.min(96, (offsetMs / waterfall.totalMs) * 100);
              const widthPct = Math.max(
                3.5,
                Math.min(100 - leftPct, (Math.max(1, sp.duration_ms) / waterfall.totalMs) * 100)
              );
              const isHot = !isSubOfParent && sharePct >= 40;
              const selected = selectedSpanId === sp.span_id;
              const pills = compactAttrSummary(sp.attributes || {});

              return (
                <button
                  key={sp.span_id}
                  type="button"
                  onClick={() => onToggleSpan(sp.span_id)}
                  className="w-full text-left px-3 py-2.5 transition-colors block"
                  style={{
                    background: selected ? "var(--surface-hover)" : "transparent",
                    borderColor: "var(--border)",
                  }}
                >
                  <div
                    className="flex items-center justify-between gap-2 mb-1.5"
                    style={{ paddingLeft: depth ? `${depth * 14}px` : "0px" }}
                  >
                    <div className="flex items-center gap-1.5 min-w-0">
                      {depth > 0 && (
                        <span className="font-mono text-[10.5px] shrink-0" style={{ color: "var(--ink-muted)" }}>
                          ↳
                        </span>
                      )}
                      <span
                        className="inline-flex items-center justify-center w-4 h-4 rounded shrink-0"
                        style={{ background: vis.bg, color: vis.color }}
                      >
                        <Icon size={10.5} />
                      </span>
                      <span className="font-mono text-[11.8px] font-medium truncate">{sp.operation_name}</span>
                      <span
                        className="font-mono text-[9.5px] px-1 py-0.5 rounded shrink-0"
                        style={{ background: vis.bg, color: vis.color }}
                      >
                        {vis.tag}
                      </span>
                    </div>
                    <div className="flex items-center gap-1.5 shrink-0 font-mono text-[11px]">
                      <span style={{ color: "var(--ink-muted)" }}>+{formatMs(offsetMs)}</span>
                      <span className="font-semibold" style={{ color: "var(--ink)" }}>
                        {formatMs(sp.duration_ms)}
                      </span>
                      <span
                        className="px-1.5 py-0.5 rounded text-[10px] font-semibold"
                        style={{
                          background: isHot ? "var(--accent-soft)" : "var(--surface-subtle)",
                          color: isHot
                            ? "var(--accent)"
                            : isSubOfParent
                            ? "var(--ink-muted)"
                            : "var(--ink-secondary)",
                        }}
                      >
                        {shareLabel}
                      </span>
                    </div>
                  </div>

                  <div className="h-2.5 rounded-sm relative overflow-hidden" style={{ background: "var(--surface-subtle)" }}>
                    {[25, 50, 75].map((pct) => (
                      <div
                        key={pct}
                        className="absolute top-0 bottom-0 w-px"
                        style={{ left: `${pct}%`, background: "var(--border)" }}
                      />
                    ))}
                    <div
                      className="absolute top-0.5 bottom-0.5 rounded-sm"
                      style={{
                        left: `${leftPct.toFixed(1)}%`,
                        width: `${widthPct.toFixed(1)}%`,
                        background: vis.color,
                      }}
                    />
                  </div>

                  {pills.length > 0 && (
                    <div
                      className="flex items-center gap-1.5 flex-wrap mt-1.5"
                      style={{ paddingLeft: depth ? `${depth * 14}px` : "0px" }}
                    >
                      {pills.map((p) => (
                        <span
                          key={p}
                          className="font-mono text-[10px] px-1.5 py-0.5 rounded"
                          style={{ background: "var(--surface-subtle)", color: "var(--ink-secondary)" }}
                        >
                          {p}
                        </span>
                      ))}
                    </div>
                  )}
                </button>
              );
            })}

            {waterfall.gapRow && (
              <div className="px-3 py-2.5" style={{ borderColor: "var(--border)" }}>
                <div className="flex items-center justify-between gap-2 mb-1.5">
                  <div className="flex items-center gap-1.5 min-w-0">
                    <span
                      className="inline-flex items-center justify-center w-4 h-4 rounded shrink-0"
                      style={{ background: "var(--surface-subtle)", color: "var(--ink-muted)" }}
                    >
                      <Clock size={10.5} />
                    </span>
                    <span className="font-mono text-[11.5px] font-medium truncate" style={{ color: "var(--ink-secondary)" }}>
                      框架调度 / Checkpoint
                    </span>
                    <span
                      className="font-mono text-[9.5px] px-1 py-0.5 rounded shrink-0"
                      style={{ background: "var(--surface-subtle)", color: "var(--ink-muted)" }}
                    >
                      SYS
                    </span>
                  </div>
                  <div className="flex items-center gap-1.5 shrink-0 font-mono text-[11px]">
                    <span className="font-semibold" style={{ color: "var(--ink-secondary)" }}>
                      {formatMs(waterfall.gapRow.durationMs)}
                    </span>
                    <span
                      className="px-1.5 py-0.5 rounded text-[10px] font-semibold"
                      style={{ background: "var(--surface-subtle)", color: "var(--ink-secondary)" }}
                    >
                      {waterfall.gapRow.shareLabel}
                    </span>
                  </div>
                </div>

                <div className="h-2.5 rounded-sm relative overflow-hidden" style={{ background: "var(--surface-subtle)" }}>
                  {[25, 50, 75].map((pct) => (
                    <div
                      key={pct}
                      className="absolute top-0 bottom-0 w-px"
                      style={{ left: `${pct}%`, background: "var(--border)" }}
                    />
                  ))}
                  {waterfall.gapRow.segments.map((seg, i) => {
                    const left = Math.min(98, ((seg.startMs - waterfall.minStart) / waterfall.totalMs) * 100);
                    const width = Math.max(
                      1.2,
                      Math.min(100 - left, ((seg.endMs - seg.startMs) / waterfall.totalMs) * 100)
                    );
                    return (
                      <div
                        key={i}
                        className="absolute top-0.5 bottom-0.5 rounded-sm"
                        style={{ left: `${left.toFixed(1)}%`, width: `${width.toFixed(1)}%`, background: "#9E988E" }}
                      />
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
};
