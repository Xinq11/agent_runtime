import React, { useMemo, useState } from "react";
import { Activity, CheckCircle2, Layers, RefreshCw, X } from "lucide-react";
import { RuntimeStateSnapshot } from "../../shared/types/runtime";
import { ContextPanel } from "./ContextPanel";
import { SpanDetails } from "./SpanDetails";
import { buildWaterfallModel, formatMs } from "./timeline";
import { TraceSelector } from "./TraceSelector";
import { WaterfallList } from "./WaterfallList";

export type InspectorTab = "trace" | "context";

interface TraceDrawerProps {
  open: boolean;
  activeTab: InspectorTab;
  onChangeTab: (tab: InspectorTab) => void;
  onClose: () => void;
  threadId: string;
  threadTitle: string;
  isDraftThread: boolean;
  state: RuntimeStateSnapshot | null;
  onRefresh: () => void;
  onSelectTrace: (traceId: string) => void;
}

export const TraceDrawer: React.FC<TraceDrawerProps> = ({
  open,
  activeTab,
  onChangeTab,
  onClose,
  threadId,
  threadTitle,
  isDraftThread,
  state,
  onRefresh,
  onSelectTrace,
}) => {
  const [selectedSpanId, setSelectedSpanId] = useState<string | null>(null);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const spans = state?.spans || [];
  const waterfall = useMemo(() => buildWaterfallModel(spans), [spans]);
  const activeSpan = useMemo(
    () => spans.find((s) => s.span_id === selectedSpanId) || null,
    [spans, selectedSpanId]
  );

  if (!open) return null;

  const handleRefresh = () => {
    setRefreshNonce((n) => n + 1);
    onRefresh();
  };

  return (
    <aside
      className="w-[428px] shrink-0 border-l flex flex-col min-h-0"
      style={{ background: "var(--rail)", borderColor: "var(--border)" }}
    >
      <div
        className="h-[52px] shrink-0 px-3.5 border-b flex items-center justify-between gap-2"
        style={{ borderColor: "var(--border)" }}
      >
        <div
          className="inline-flex items-center p-0.5 rounded-lg border"
          style={{ background: "var(--bg)", borderColor: "var(--border)" }}
        >
          {(
            [
              ["trace", "Trace 瀑布图", Activity],
              ["context", "上下文与压缩", Layers],
            ] as const
          ).map(([id, label, Icon]) => {
            const active = activeTab === id;
            return (
              <button
                key={id}
                type="button"
                onClick={() => onChangeTab(id)}
                className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[12px] font-medium transition-colors"
                style={{
                  background: active ? "var(--surface)" : "transparent",
                  color: active ? "var(--ink)" : "var(--ink-muted)",
                  boxShadow: active ? "0 1px 2px rgba(0,0,0,0.06)" : "none",
                }}
              >
                <Icon size={13} style={{ color: active ? "var(--accent)" : "var(--ink-muted)" }} />
                {label}
              </button>
            );
          })}
        </div>

        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={handleRefresh}
            aria-label="刷新"
            title="刷新"
            className="interactive-btn p-1.5 rounded-md border"
            style={{ background: "var(--surface)", borderColor: "var(--border)", color: "var(--ink-secondary)" }}
          >
            <RefreshCw size={13} />
          </button>
          <button
            type="button"
            onClick={onClose}
            aria-label="收起侧栏"
            title="收起侧栏"
            className="interactive-btn p-1.5 rounded-md border"
            style={{ background: "var(--surface)", borderColor: "var(--border)", color: "var(--ink-secondary)" }}
          >
            <X size={13} />
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-3.5 space-y-3">
        {activeTab === "context" ? (
          <ContextPanel
            threadId={threadId}
            threadTitle={threadTitle}
            isDraftThread={isDraftThread}
            state={state}
            refreshNonce={refreshNonce}
          />
        ) : (
          <>
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
          </>
        )}
      </div>
    </aside>
  );
};
