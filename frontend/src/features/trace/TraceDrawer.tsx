import React, { useMemo, useState } from "react";
import { Activity, ArrowUpRight, CheckCircle2, RefreshCw } from "lucide-react";
import { RuntimeStateSnapshot } from "../../shared/types/runtime";
import { SpanDetails } from "./SpanDetails";
import { buildWaterfallModel, formatMs } from "./timeline";
import { TraceSelector } from "./TraceSelector";
import { WaterfallList } from "./WaterfallList";

interface TraceDrawerProps {
  open: boolean;
  state: RuntimeStateSnapshot | null;
  onRefresh: () => void;
  onSelectTrace: (traceId: string) => void;
  onOpenOpsPlatform: () => void;
}

export const TraceDrawer: React.FC<TraceDrawerProps> = ({
  open,
  state,
  onRefresh,
  onSelectTrace,
  onOpenOpsPlatform,
}) => {
  const [selectedSpanId, setSelectedSpanId] = useState<string | null>(null);
  const spans = state?.spans || [];
  const waterfall = useMemo(() => buildWaterfallModel(spans), [spans]);
  const activeSpan = useMemo(
    () => spans.find((s) => s.span_id === selectedSpanId) || null,
    [spans, selectedSpanId]
  );

  if (!open) return null;

  return (
    <aside
      className="w-[428px] shrink-0 border-l flex flex-col min-h-0"
      style={{ background: "var(--rail)", borderColor: "var(--border)" }}
    >
      <div
        className="h-[52px] shrink-0 px-4 border-b flex items-center justify-between gap-2"
        style={{ borderColor: "var(--border)" }}
      >
        <div className="flex items-center gap-2 min-w-0">
          <Activity size={15} style={{ color: "var(--accent)" }} />
          <span className="text-[13.5px] font-semibold">执行 Trace 瀑布图</span>
        </div>
        <div className="flex items-center gap-1.5">
          <button
            onClick={onOpenOpsPlatform}
            className="interactive-btn inline-flex items-center gap-1 px-2 py-1 rounded-md border text-[11.5px] font-medium"
            style={{ background: "var(--surface)", borderColor: "var(--border)", color: "var(--ink-secondary)" }}
          >
            监控中心
            <ArrowUpRight size={12} />
          </button>
          <button
            onClick={onRefresh}
            aria-label="刷新 Trace"
            className="interactive-btn p-1.5 rounded-md border"
            style={{ background: "var(--surface)", borderColor: "var(--border)", color: "var(--ink-secondary)" }}
          >
            <RefreshCw size={13} />
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-3.5 space-y-3">
        {state?.trace_id && (
          <TraceSelector
            traceId={state.trace_id}
            recentTraces={state.recent_traces}
            onSelectTrace={(tr) => {
              setSelectedSpanId(null);
              onSelectTrace(tr);
            }}
          />
        )}

        {spans.length > 0 && (
          <div className="grid grid-cols-4 gap-2">
            {[
              ["总耗时", formatMs(waterfall.totalMs)],
              ["LLM 轮次", String(waterfall.llmCount)],
              ["工具/子代理", String(waterfall.toolCount)],
            ].map(([label, val]) => (
              <div
                key={label}
                className="rounded-lg border px-2.5 py-2"
                style={{ background: "var(--surface)", borderColor: "var(--border)" }}
              >
                <div className="text-[10.5px]" style={{ color: "var(--ink-muted)" }}>
                  {label}
                </div>
                <div className="font-mono text-[13.5px] font-semibold mt-0.5">{val}</div>
              </div>
            ))}
            <div
              className="rounded-lg border px-2.5 py-2"
              style={{ background: "var(--surface)", borderColor: "var(--border)" }}
            >
              <div className="text-[10.5px]" style={{ color: "var(--ink-muted)" }}>
                状态
              </div>
              <div
                className="font-mono text-[12px] font-semibold mt-0.5 flex items-center gap-1"
                style={{
                  color:
                    waterfall.overall === "error"
                      ? "var(--danger)"
                      : waterfall.overall === "paused"
                      ? "var(--warn)"
                      : "var(--ok)",
                }}
              >
                <CheckCircle2 size={12} />
                {waterfall.overall.toUpperCase()}
              </div>
            </div>
          </div>
        )}

        <WaterfallList
          waterfall={waterfall}
          hasSpans={spans.length > 0}
          selectedSpanId={selectedSpanId}
          onToggleSpan={(id) => setSelectedSpanId((cur) => (cur === id ? null : id))}
        />

        <SpanDetails
          activeSpan={activeSpan}
          shareLabel={activeSpan ? waterfall.shareBySpanId.get(activeSpan.span_id) : undefined}
          state={state}
        />
      </div>
    </aside>
  );
};
