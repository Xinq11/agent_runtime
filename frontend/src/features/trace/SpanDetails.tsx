import React from "react";
import { RuntimeStateSnapshot, SpanRecord } from "../../shared/types/runtime";
import { formatMs } from "./timeline";

interface SpanDetailsProps {
  activeSpan: SpanRecord | null;
  shareLabel?: string;
  state: RuntimeStateSnapshot | null;
}

export const SpanDetails: React.FC<SpanDetailsProps> = ({ activeSpan, shareLabel, state }) => {
  return (
    <>
      {activeSpan && (
        <div
          className="rounded-xl border p-3 space-y-2 text-[12px]"
          style={{ background: "var(--surface)", borderColor: "var(--accent-border)" }}
        >
          <div className="flex items-center justify-between">
            <span className="font-mono font-semibold text-[12px]">
              Span 详情 · {activeSpan.operation_name}
            </span>
            <span className="font-mono text-[10.5px]" style={{ color: "var(--ink-muted)" }}>
              {activeSpan.span_id}
            </span>
          </div>
          <div className="grid grid-cols-2 gap-1.5 font-mono text-[11px]">
            <div style={{ color: "var(--ink-muted)" }}>
              status: <strong style={{ color: "var(--ink)" }}>{activeSpan.status}</strong>
            </div>
            <div style={{ color: "var(--ink-muted)" }}>
              duration:{" "}
              <strong style={{ color: "var(--ink)" }}>
                {formatMs(activeSpan.duration_ms)} ({shareLabel || "0.0%"})
              </strong>
            </div>
            <div className="col-span-2 truncate" style={{ color: "var(--ink-muted)" }}>
              parent:{" "}
              <span style={{ color: "var(--ink-secondary)" }}>{activeSpan.parent_span_id || "(root)"}</span>
            </div>
          </div>
          <pre
            className="rounded-lg p-2.5 font-mono text-[11px] overflow-x-auto max-h-40"
            style={{ background: "var(--code-bg)", color: "var(--code-ink)" }}
          >
            {JSON.stringify(activeSpan.attributes || {}, null, 2)}
          </pre>
        </div>
      )}

      {state && (state.loki_logs.length > 0 || state.summary.covered_up_to_msg_id > 0) && (
        <div
          className="rounded-xl border p-3 space-y-2 text-[11.5px]"
          style={{ background: "var(--surface)", borderColor: "var(--border)" }}
        >
          <div className="flex items-center justify-between">
            <span className="font-semibold text-[12px]">本次事件流水与上下文状态</span>
            <span className="font-mono text-[10.5px]" style={{ color: "var(--ink-muted)" }}>
              压缩游标 #{state.summary.covered_up_to_msg_id}
            </span>
          </div>
          <div className="space-y-1 font-mono text-[10.8px]">
            {state.loki_logs.slice(-6).map((lg, i) => (
              <div
                key={i}
                className="flex items-center justify-between gap-2 px-2 py-1 rounded"
                style={{ background: "var(--bg)" }}
              >
                <span style={{ color: "var(--accent)" }}>{String(lg.event || "log")}</span>
                <span className="truncate" style={{ color: "var(--ink-secondary)" }}>
                  {String(
                    lg.tool ||
                      (Array.isArray(lg.tool_calls) && lg.tool_calls.length
                        ? `calls=${lg.tool_calls.join(",")}`
                        : lg.status || lg.model || "ok")
                  )}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </>
  );
};
