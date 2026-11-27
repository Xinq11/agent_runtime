import React, { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Layers, Sparkles } from "lucide-react";
import { fetchThreadMessages } from "../../shared/api/client";
import { MarkdownContent } from "../../shared/components/MarkdownContent";
import { RuntimeStateSnapshot } from "../../shared/types/runtime";

interface ContextPanelProps {
  threadId: string;
  threadTitle: string;
  isDraftThread: boolean;
  state: RuntimeStateSnapshot | null;
  refreshNonce: number;
}

export const ContextPanel: React.FC<ContextPanelProps> = ({
  threadId,
  threadTitle,
  isDraftThread,
  state,
  refreshNonce,
}) => {
  const [messages, setMessages] = useState<Array<Record<string, unknown>>>([]);
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [listOpen, setListOpen] = useState(true);

  const cursorId = state?.summary?.covered_up_to_msg_id ?? 0;
  const summaryText = state?.summary?.text || "";
  const latestTraceId = state?.trace_id || "";

  useEffect(() => {
    if (isDraftThread || !threadId) {
      setMessages([]);
      setExpanded({});
      return;
    }
    let alive = true;
    setLoading(true);
    fetchThreadMessages(threadId)
      .then((rows) => {
        if (alive) setMessages(rows);
      })
      .catch(() => {
        if (alive) setMessages([]);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [threadId, isDraftThread, latestTraceId, cursorId, refreshNonce]);

  if (isDraftThread) {
    return (
      <div
        className="rounded-xl border border-dashed px-4 py-10 text-center space-y-2"
        style={{ borderColor: "var(--border-strong)", background: "var(--surface)" }}
      >
        <div
          className="w-9 h-9 rounded-lg mx-auto flex items-center justify-center"
          style={{ background: "var(--accent-soft)", color: "var(--accent)" }}
        >
          <Layers size={17} />
        </div>
        <div className="text-[13px] font-medium">暂无活跃上下文</div>
        <p className="text-[12px] leading-relaxed" style={{ color: "var(--ink-muted)" }}>
          发送首条消息后，将在此实时透视当前会话的消息窗口、压缩游标与历史压缩摘要。
        </p>
      </div>
    );
  }

  const compactedCount = messages.filter(
    (m) => cursorId > 0 && Number(m.msg_id ?? 0) <= cursorId
  ).length;
  const activeCount = Math.max(0, messages.length - compactedCount);
  const orderedMessages = messages
    .map((m, idx) => ({ msg: m, seq: idx + 1, key: String(m.msg_id ?? idx) }))
    .reverse();

  return (
    <div className="space-y-3">
      {/* Compact thread identity & status header */}
      <div
        className="rounded-xl border px-3 py-2.5 space-y-1"
        style={{ background: "var(--surface)", borderColor: "var(--border)" }}
      >
        <div className="flex items-center justify-between gap-2">
          <span className="text-[12.5px] font-semibold truncate">{threadTitle}</span>
          <span
            className="font-mono text-[10px] px-2 py-0.5 rounded-full shrink-0"
            title={cursorId > 0 ? `covered_up_to_msg_id = ${cursorId}` : undefined}
            style={{
              background: cursorId > 0 ? "var(--ok-soft)" : "var(--surface-subtle)",
              color: cursorId > 0 ? "var(--ok)" : "var(--ink-muted)",
            }}
          >
            {compactedCount > 0 ? `已压缩至 #${compactedCount}` : cursorId > 0 ? "已压缩" : "未触发压缩"}
          </span>
        </div>
        <div className="font-mono text-[10.5px] truncate" style={{ color: "var(--ink-muted)" }}>
          {threadId}
        </div>
      </div>

      {/* 3-stat strip */}
      <div className="grid grid-cols-3 gap-2">
        {[
          ["总消息", String(messages.length), "var(--ink)"],
          ["已压缩", String(compactedCount), compactedCount > 0 ? "var(--accent)" : "var(--ink-muted)"],
          ["活跃窗口", String(activeCount), "var(--ok)"],
        ].map(([label, val, color]) => (
          <div
            key={label}
            className="rounded-lg border px-2.5 py-2"
            style={{ background: "var(--surface)", borderColor: "var(--border)" }}
          >
            <div className="text-[10.5px]" style={{ color: "var(--ink-muted)" }}>
              {label}
            </div>
            <div className="font-mono text-[14px] font-semibold mt-0.5" style={{ color }}>
              {val}
            </div>
          </div>
        ))}
      </div>

      {/* Compressed structured summary card */}
      <div
        className="rounded-xl border p-3 space-y-2"
        style={{ background: "var(--surface)", borderColor: "var(--border)" }}
      >
        <div className="flex items-center gap-1.5 text-[11.5px] font-semibold" style={{ color: "var(--ink-secondary)" }}>
          <Sparkles size={13} style={{ color: "var(--accent)" }} />
          历史压缩摘要（before_model 注入）
        </div>
        {summaryText ? (
          <div
            className="rounded-lg border p-2.5 text-[12px]"
            style={{ background: "var(--bg)", borderColor: "var(--border)" }}
          >
            <MarkdownContent content={summaryText} />
          </div>
        ) : (
          <div
            className="rounded-lg border px-3 py-2.5 text-[11.5px] leading-relaxed"
            style={{ background: "var(--bg)", borderColor: "var(--border)", color: "var(--ink-muted)" }}
          >
            尚未触发自动压缩（会话消息达到 8 条时会在 after_agent 阶段由模型自动提炼结构化摘要并推进游标）。
          </div>
        )}
      </div>

      {/* Full session_messages inspector */}
      <div
        className="rounded-xl border p-3 space-y-2"
        style={{ background: "var(--surface)", borderColor: "var(--border)" }}
      >
        <div className="flex items-center justify-between gap-2">
          <button
            type="button"
            onClick={() => setListOpen((o) => !o)}
            className="inline-flex items-center gap-1 text-[12px] font-semibold"
          >
            {listOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            上下文消息 ({messages.length})
          </button>
          {listOpen && messages.length > 0 && (
            <div className="flex items-center gap-1">
              <button
                type="button"
                onClick={() => {
                  const next: Record<string, boolean> = {};
                  orderedMessages.forEach(({ key }) => {
                    next[key] = true;
                  });
                  setExpanded(next);
                }}
                className="px-1.5 py-0.5 rounded border text-[10.5px]"
                style={{ background: "var(--bg)", borderColor: "var(--border)", color: "var(--ink-secondary)" }}
              >
                展开
              </button>
              <button
                type="button"
                onClick={() => setExpanded({})}
                className="px-1.5 py-0.5 rounded border text-[10.5px]"
                style={{ background: "var(--bg)", borderColor: "var(--border)", color: "var(--ink-secondary)" }}
              >
                收起
              </button>
            </div>
          )}
        </div>

        {listOpen &&
          (loading ? (
            <div className="text-[12px] py-3" style={{ color: "var(--ink-muted)" }}>
              加载上下文中...
            </div>
          ) : messages.length === 0 ? (
            <div className="text-[12px] py-2" style={{ color: "var(--ink-muted)" }}>
              当前会话暂无已持久化消息。
            </div>
          ) : (
            <div className="space-y-1.5">
              {orderedMessages.map(({ msg: m, seq, key }) => {
                const isCompacted = cursorId > 0 && Number(m.msg_id ?? 0) <= cursorId;
                const isOpen = Boolean(expanded[key]);
                const callsCount = Array.isArray(m.tool_calls) ? m.tool_calls.length : 0;
                const preview = String(m.content || "").replace(/\s+/g, " ").trim().slice(0, 48);
                return (
                  <div
                    key={key}
                    className="rounded-lg border overflow-hidden text-[11.5px]"
                    style={{ background: "var(--bg)", borderColor: "var(--border)" }}
                  >
                    <button
                      type="button"
                      onClick={() => setExpanded((prev) => ({ ...prev, [key]: !prev[key] }))}
                      className="w-full px-2.5 py-1.5 flex items-center justify-between gap-1.5 text-left"
                    >
                      <div className="flex items-center gap-1.5 min-w-0 flex-1">
                        {isOpen ? (
                          <ChevronDown size={12} className="shrink-0" style={{ color: "var(--ink-muted)" }} />
                        ) : (
                          <ChevronRight size={12} className="shrink-0" style={{ color: "var(--ink-muted)" }} />
                        )}
                        <span className="font-mono text-[10.5px] font-semibold shrink-0" style={{ color: "var(--accent)" }}>
                          #{seq} {String(m.role)}
                          {m.name ? `(${String(m.name)})` : ""}
                        </span>
                        <span
                          className="font-mono text-[9.5px] px-1.5 py-0.5 rounded shrink-0"
                          style={{
                            background: isCompacted ? "var(--surface-subtle)" : "var(--ok-soft)",
                            color: isCompacted ? "var(--ink-muted)" : "var(--ok)",
                          }}
                        >
                          {isCompacted ? "已压缩" : "活跃"}
                        </span>
                        {callsCount > 0 && (
                          <span
                            className="font-mono text-[9.5px] px-1 py-0.5 rounded shrink-0"
                            style={{ background: "var(--surface-subtle)", color: "var(--ink-secondary)" }}
                          >
                            {callsCount} call
                          </span>
                        )}
                        {!isOpen && preview && (
                          <span className="truncate text-[11px]" style={{ color: "var(--ink-muted)" }}>
                            {preview}
                          </span>
                        )}
                      </div>
                    </button>

                    {isOpen && (
                      <div className="px-2.5 pb-2.5 pt-2 border-t space-y-2" style={{ borderColor: "var(--border)" }}>
                        {Boolean(m.tool_call_id) && (
                          <div className="font-mono text-[10px]" style={{ color: "var(--ink-muted)" }}>
                            tool_call_id: {String(m.tool_call_id)}
                          </div>
                        )}
                        {Boolean(m.content) && (
                          <div
                            className="whitespace-pre-wrap break-words leading-relaxed text-[11.5px]"
                            style={{ color: "var(--ink-secondary)" }}
                          >
                            {String(m.content)}
                          </div>
                        )}
                        {callsCount > 0 && (
                          <pre
                            className="rounded p-2 font-mono text-[10.5px] overflow-x-auto"
                            style={{ background: "var(--code-bg)", color: "var(--code-ink)" }}
                          >
                            {JSON.stringify(m.tool_calls, null, 2)}
                          </pre>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          ))}
      </div>
    </div>
  );
};
