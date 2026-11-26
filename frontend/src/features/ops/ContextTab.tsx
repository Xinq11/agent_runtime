import React, { useEffect, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { fetchThreadMessages } from "../../shared/api/client";
import { MarkdownContent } from "../../shared/components/MarkdownContent";
import { ThreadSummary } from "../../shared/types/runtime";

interface ContextTabProps {
  threads: ThreadSummary[];
  selectedThreadId: string | null;
  onSelectThread: (threadId: string) => void;
  onOpenInChat: (threadId: string) => void;
}

export const ContextTab: React.FC<ContextTabProps> = ({
  threads,
  selectedThreadId,
  onSelectThread,
  onOpenInChat,
}) => {
  const [threadMsgs, setThreadMsgs] = useState<Array<Record<string, unknown>>>([]);
  const [loadingMsgs, setLoadingMsgs] = useState(false);
  const [expandedMsgs, setExpandedMsgs] = useState<Record<string, boolean>>({});
  const [msgsSectionOpen, setMsgsSectionOpen] = useState(true);

  useEffect(() => {
    if (!selectedThreadId) {
      setThreadMsgs([]);
      return;
    }
    let alive = true;
    setLoadingMsgs(true);
    fetchThreadMessages(selectedThreadId)
      .then((msgs) => {
        if (alive) setThreadMsgs(msgs);
      })
      .finally(() => {
        if (alive) setLoadingMsgs(false);
      });
    return () => {
      alive = false;
    };
  }, [selectedThreadId]);

  const activeThreadMeta = threads.find((t) => t.thread_id === selectedThreadId);
  const cursorId = activeThreadMeta?.covered_up_to_msg_id ?? 0;
  const compactedCount = threadMsgs.filter(
    (m) => cursorId > 0 && Number(m.msg_id ?? 0) <= cursorId
  ).length;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-12 gap-4">
      <div
        className="lg:col-span-4 rounded-xl border p-3 space-y-1.5 max-h-[72vh] overflow-y-auto"
        style={{ background: "var(--surface)", borderColor: "var(--border)" }}
      >
        <div className="px-2 py-1 text-[12px] font-semibold" style={{ color: "var(--ink-secondary)" }}>
          全部有效会话 ({threads.length})
        </div>
        {threads.map((t) => {
          const active = t.thread_id === selectedThreadId;
          return (
            <button
              key={t.thread_id}
              onClick={() => onSelectThread(t.thread_id)}
              className="w-full text-left px-3 py-2.5 rounded-lg border transition-colors"
              style={{
                background: active ? "var(--accent-soft)" : "var(--bg)",
                borderColor: active ? "var(--accent-border)" : "var(--border)",
              }}
            >
              <div className="text-[13px] font-medium truncate">{t.title}</div>
              <div
                className="flex items-center justify-between mt-1 font-mono text-[10.5px]"
                style={{ color: "var(--ink-muted)" }}
              >
                <span>{t.thread_id.slice(0, 14)}</span>
                <span>
                  {t.message_count ?? 0} 条 · {(t.covered_up_to_msg_id ?? 0) > 0 ? "已压缩" : "未压缩"}
                </span>
              </div>
            </button>
          );
        })}
      </div>

      <div className="lg:col-span-8 space-y-4">
        {activeThreadMeta && (
          <div
            className="rounded-xl border p-4 space-y-3"
            style={{ background: "var(--surface)", borderColor: "var(--border)" }}
          >
            <div className="flex items-center justify-between gap-2">
              <div className="min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-[14px] font-semibold truncate">{activeThreadMeta.title}</span>
                  <span
                    className="font-mono text-[10.5px] px-2 py-0.5 rounded-full"
                    style={{
                      background: cursorId > 0 ? "var(--ok-soft)" : "var(--surface-subtle)",
                      color: cursorId > 0 ? "var(--ok)" : "var(--ink-muted)",
                    }}
                  >
                    {cursorId > 0
                      ? `已压缩前 ${compactedCount} 条 / 共 ${threadMsgs.length} 条`
                      : "未触发压缩"}
                  </span>
                </div>
                <div className="font-mono text-[11px] mt-0.5" style={{ color: "var(--ink-muted)" }}>
                  thread_id: {activeThreadMeta.thread_id}
                </div>
              </div>
              <button
                onClick={() => onOpenInChat(activeThreadMeta.thread_id)}
                className="interactive-btn shrink-0 px-3 py-1.5 rounded-lg border text-[12px] font-medium"
                style={{ background: "var(--bg)", borderColor: "var(--border-strong)" }}
              >
                在对话页打开
              </button>
            </div>

            <div
              className="rounded-lg border p-3.5 space-y-2"
              style={{ background: "var(--bg)", borderColor: "var(--border)" }}
            >
              <div className="text-[11.5px] font-semibold" style={{ color: "var(--ink-secondary)" }}>
                模型压缩后的结构化摘要（注入 before_model 的长期记忆）
              </div>
              {activeThreadMeta.summary_text ? (
                <MarkdownContent content={activeThreadMeta.summary_text} />
              ) : (
                <div className="text-[12px]" style={{ color: "var(--ink-muted)" }}>
                  尚未触发自动压缩（会话消息达到 8 条时在 after_agent 阶段由模型自动提炼压缩）。
                </div>
              )}
            </div>
          </div>
        )}

        <div
          className="rounded-xl border p-4 space-y-2.5"
          style={{ background: "var(--surface)", borderColor: "var(--border)" }}
        >
          <div className="flex items-center justify-between gap-2 flex-wrap">
            <button
              type="button"
              onClick={() => setMsgsSectionOpen((o) => !o)}
              className="inline-flex items-center gap-1.5 text-[13px] font-semibold"
            >
              {msgsSectionOpen ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
              session_messages 完整上下文 ({threadMsgs.length} 条)
            </button>
            {msgsSectionOpen && threadMsgs.length > 0 && (
              <div className="flex items-center gap-1.5">
                <button
                  type="button"
                  onClick={() => {
                    const next: Record<string, boolean> = {};
                    threadMsgs.forEach((m, i) => {
                      next[String(m.msg_id ?? i)] = true;
                    });
                    setExpandedMsgs(next);
                  }}
                  className="px-2 py-1 rounded-md border text-[11.5px]"
                  style={{ background: "var(--bg)", borderColor: "var(--border)", color: "var(--ink-secondary)" }}
                >
                  全部展开
                </button>
                <button
                  type="button"
                  onClick={() => setExpandedMsgs({})}
                  className="px-2 py-1 rounded-md border text-[11.5px]"
                  style={{ background: "var(--bg)", borderColor: "var(--border)", color: "var(--ink-secondary)" }}
                >
                  全部收起
                </button>
              </div>
            )}
          </div>

          {msgsSectionOpen &&
            (loadingMsgs ? (
              <div className="text-[12.5px] py-4" style={{ color: "var(--ink-muted)" }}>
                加载中...
              </div>
            ) : (
              <div className="space-y-1.5 max-h-[54vh] overflow-y-auto">
                {threadMsgs.map((m, idx) => {
                  const key = String(m.msg_id ?? idx);
                  const isCompacted = cursorId > 0 && Number(m.msg_id ?? 0) <= cursorId;
                  const isOpen = Boolean(expandedMsgs[key]);
                  const callsCount = Array.isArray(m.tool_calls) ? m.tool_calls.length : 0;
                  const preview = String(m.content || "").replace(/\s+/g, " ").trim().slice(0, 72);
                  return (
                    <div
                      key={key}
                      className="rounded-lg border overflow-hidden text-[12px]"
                      style={{ background: "var(--bg)", borderColor: "var(--border)" }}
                    >
                      <button
                        type="button"
                        onClick={() => setExpandedMsgs((prev) => ({ ...prev, [key]: !prev[key] }))}
                        className="w-full px-3 py-2 flex items-center justify-between gap-2 text-left"
                      >
                        <div className="flex items-center gap-2 min-w-0 flex-1">
                          {isOpen ? (
                            <ChevronDown size={14} className="shrink-0" style={{ color: "var(--ink-muted)" }} />
                          ) : (
                            <ChevronRight size={14} className="shrink-0" style={{ color: "var(--ink-muted)" }} />
                          )}
                          <span className="font-mono text-[11px] font-semibold shrink-0" style={{ color: "var(--accent)" }}>
                            #{idx + 1} · {String(m.role)}
                            {m.name ? `(${String(m.name)})` : ""}
                          </span>
                          <span
                            className="font-mono text-[10px] px-1.5 py-0.5 rounded shrink-0"
                            style={{
                              background: isCompacted ? "var(--surface-subtle)" : "var(--ok-soft)",
                              color: isCompacted ? "var(--ink-muted)" : "var(--ok)",
                            }}
                          >
                            {isCompacted ? "已压缩" : "活跃"}
                          </span>
                          {callsCount > 0 && (
                            <span
                              className="font-mono text-[10px] px-1.5 py-0.5 rounded shrink-0"
                              style={{ background: "var(--surface-subtle)", color: "var(--ink-secondary)" }}
                            >
                              {callsCount} tool_call
                            </span>
                          )}
                          {!isOpen && preview && (
                            <span className="truncate text-[11.5px]" style={{ color: "var(--ink-muted)" }}>
                              {preview}
                            </span>
                          )}
                        </div>
                        {Boolean(m.tool_call_id) && (
                          <span className="font-mono text-[10.5px] shrink-0" style={{ color: "var(--ink-muted)" }}>
                            {String(m.tool_call_id)}
                          </span>
                        )}
                      </button>

                      {isOpen && (
                        <div className="px-3 pb-3 pt-2 border-t space-y-2" style={{ borderColor: "var(--border)" }}>
                          {Boolean(m.content) && (
                            <div
                              className="whitespace-pre-wrap break-words leading-relaxed"
                              style={{ color: "var(--ink-secondary)" }}
                            >
                              {String(m.content)}
                            </div>
                          )}
                          {callsCount > 0 && (
                            <pre
                              className="rounded p-2.5 font-mono text-[11px] overflow-x-auto"
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
    </div>
  );
};
