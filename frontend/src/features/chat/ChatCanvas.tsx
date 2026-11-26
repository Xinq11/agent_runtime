import React, { useEffect, useRef } from "react";
import { Activity, PanelRightClose, PanelRightOpen } from "lucide-react";
import { MarkdownContent } from "../../shared/components/MarkdownContent";
import { AppMeta, ApprovalTicket, ChatTurnItem, ModelConfig } from "../../shared/types/runtime";
import { ActivityAccordion } from "./ActivityAccordion";
import { ApprovalCard } from "./ApprovalCard";
import { Composer } from "./Composer";

interface ChatCanvasProps {
  appMeta?: AppMeta | null;
  threadTitle: string;
  items: ChatTurnItem[];
  loading: boolean;
  focusNonce?: number;
  modelConfig: ModelConfig;
  traceOpen: boolean;
  onToggleTrace: () => void;
  onSend: (text: string) => void;
  onAbort?: () => void;
  onApprovalDecision: (itemId: string, ticket: ApprovalTicket, approved: boolean) => void;
  onInspectTrace: (traceId: string) => void;
}

export const ChatCanvas: React.FC<ChatCanvasProps> = ({
  appMeta,
  threadTitle,
  items,
  loading,
  focusNonce,
  modelConfig,
  traceOpen,
  onToggleTrace,
  onSend,
  onAbort,
  onApprovalDecision,
  onInspectTrace,
}) => {
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [items, loading]);

  return (
    <main className="flex-1 flex flex-col min-w-0 min-h-0" style={{ background: "var(--bg)" }}>
      <header
        className="h-[52px] shrink-0 px-5 border-b flex items-center justify-between gap-3"
        style={{ borderColor: "var(--border)", background: "var(--bg)" }}
      >
        <h1 className="text-[14px] font-semibold truncate">{threadTitle}</h1>
        <button
          onClick={onToggleTrace}
          className="interactive-btn inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border text-[12px]"
          style={{ background: "var(--surface)", borderColor: "var(--border)", color: "var(--ink-secondary)" }}
        >
          {traceOpen ? <PanelRightClose size={14} /> : <PanelRightOpen size={14} />}
          {traceOpen ? "收起 Trace" : "展开 Trace"}
        </button>
      </header>

      <div className="flex-1 overflow-y-auto px-5 py-6">
        <div className="max-w-3xl mx-auto space-y-6">
          {items.length === 0 ? (
            <div className="pt-10 pb-4">
              <div className="mb-6">
                <div
                  className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11.5px] font-mono mb-3 border"
                  style={{
                    background: "var(--accent-soft)",
                    borderColor: "var(--accent-border)",
                    color: "var(--accent)",
                  }}
                >
                  {modelConfig.mode === "deepseek" ? `DeepSeek · ${modelConfig.model}` : "Local Mock Runtime"}
                </div>
                <h2 className="text-[24px] font-semibold tracking-tight leading-snug mb-2">
                  {appMeta?.welcome_title || "今天想处理哪项任务？"}
                </h2>
                <p className="text-[14px] leading-relaxed" style={{ color: "var(--ink-secondary)" }}>
                  {appMeta?.welcome_description ||
                    "通用 Agent Runtime 底座：支持流式响应、上下文预算压缩、工具网关与高危审批、混合检索 RAG 与实时 Trace 瀑布图。"}
                </p>
              </div>

              {appMeta && appMeta.quick_prompts.length > 0 && (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  {appMeta.quick_prompts.map((q) => (
                    <button
                      key={q.title}
                      onClick={() => onSend(q.prompt)}
                      className="quiet-card interactive-btn text-left p-4 rounded-xl border"
                      style={{ background: "var(--surface)", borderColor: "var(--border)" }}
                    >
                      <div className="text-[13.5px] font-semibold mb-1">{q.title}</div>
                      <div className="text-[12.2px] leading-relaxed" style={{ color: "var(--ink-muted)" }}>
                        {q.hint}
                      </div>
                    </button>
                  ))}
                </div>
              )}
            </div>
          ) : (
            items.map((item) => {
              if (item.role === "user") {
                return (
                  <div key={item.id} className="flex justify-end">
                    <div
                      className="max-w-[82%] px-4 py-2.5 rounded-2xl text-[14px] leading-relaxed whitespace-pre-wrap"
                      style={{
                        background: "var(--surface-subtle)",
                        border: "1px solid var(--border)",
                        color: "var(--ink)",
                      }}
                    >
                      {item.content}
                    </div>
                  </div>
                );
              }

              return (
                <div key={item.id} className="space-y-2.5">
                  {item.activities && item.activities.length > 0 && (
                    <div className="space-y-1.5">
                      {item.activities.map((act) => (
                        <ActivityAccordion key={act.id} block={act} />
                      ))}
                    </div>
                  )}

                  {item.approval && (
                    <ApprovalCard
                      itemId={item.id}
                      ticket={item.approval}
                      status={item.approvalStatus}
                      onDecision={onApprovalDecision}
                    />
                  )}

                  {item.content && (
                    <div className="px-0.5">
                      <MarkdownContent content={item.content} />
                    </div>
                  )}

                  {item.traceId && (
                    <div className="pt-0.5">
                      <button
                        type="button"
                        onClick={() => onInspectTrace(item.traceId!)}
                        className="inline-flex items-center gap-1 font-mono text-[11px] hover:underline"
                        style={{ color: "var(--ink-muted)" }}
                      >
                        <Activity size={11} />
                        {item.traceId}
                      </button>
                    </div>
                  )}
                </div>
              );
            })
          )}
          <div ref={endRef} />
        </div>
      </div>

      <Composer
        loading={loading}
        focusNonce={focusNonce}
        modelConfig={modelConfig}
        onSend={onSend}
        onAbort={onAbort}
      />
    </main>
  );
};
