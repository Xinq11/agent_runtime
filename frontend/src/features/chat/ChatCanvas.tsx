import React, { useEffect, useRef, useState } from "react";
import { Activity, Check, Copy, Layers, ThumbsDown, ThumbsUp, X } from "lucide-react";
import { MarkdownContent } from "../../shared/components/MarkdownContent";
import { AppMeta, ApprovalTicket, ChatTurnItem, ModelConfig } from "../../shared/types/runtime";
import type { InspectorTab } from "../trace/TraceDrawer";
import { ActivityAccordion } from "./ActivityAccordion";
import { ApprovalCard } from "./ApprovalCard";
import { Composer } from "./Composer";

async function copyTextToClipboard(text: string): Promise<void> {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  document.execCommand("copy");
  document.body.removeChild(ta);
}

interface ChatCanvasProps {
  appMeta?: AppMeta | null;
  threadTitle: string;
  items: ChatTurnItem[];
  loading: boolean;
  focusNonce?: number;
  modelConfig: ModelConfig;
  inspectorOpen: boolean;
  onOpenInspector: (tab: InspectorTab) => void;
  onSend: (text: string) => void;
  onAbort?: () => void;
  onApprovalDecision: (itemId: string, ticket: ApprovalTicket, approved: boolean) => void;
  onSetFeedback: (
    itemId: string,
    rating: "up" | "down",
    opts?: { reason?: string; keepActive?: boolean }
  ) => Promise<"up" | "down" | null>;
  onInspectTrace: (traceId: string) => void;
}

export const ChatCanvas: React.FC<ChatCanvasProps> = ({
  appMeta,
  threadTitle,
  items,
  loading,
  focusNonce,
  modelConfig,
  inspectorOpen,
  onOpenInspector,
  onSend,
  onAbort,
  onApprovalDecision,
  onSetFeedback,
  onInspectTrace,
}) => {
  const endRef = useRef<HTMLDivElement>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [reasonTargetId, setReasonTargetId] = useState<string | null>(null);
  const [reasonDraft, setReasonDraft] = useState("");
  const [busyFeedbackId, setBusyFeedbackId] = useState<string | null>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [items, loading]);

  const handleCopy = async (item: ChatTurnItem) => {
    if (!item.content) return;
    try {
      await copyTextToClipboard(item.content);
      setCopiedId(item.id);
      window.setTimeout(() => {
        setCopiedId((cur) => (cur === item.id ? null : cur));
      }, 1800);
    } catch {
      // ignore clipboard denial
    }
  };

  const handleFeedbackClick = async (item: ChatTurnItem, rating: "up" | "down") => {
    if (!item.traceId || busyFeedbackId === item.id) return;
    setBusyFeedbackId(item.id);
    try {
      const next = await onSetFeedback(item.id, rating);
      if (next === "down") {
        setReasonTargetId(item.id);
        setReasonDraft(item.feedbackReason || "");
      } else if (reasonTargetId === item.id) {
        setReasonTargetId(null);
        setReasonDraft("");
      }
    } finally {
      setBusyFeedbackId(null);
    }
  };

  const handleSaveReason = async (item: ChatTurnItem) => {
    if (!item.traceId || busyFeedbackId === item.id) return;
    setBusyFeedbackId(item.id);
    try {
      await onSetFeedback(item.id, "down", { reason: reasonDraft, keepActive: true });
      setReasonTargetId(null);
    } finally {
      setBusyFeedbackId(null);
    }
  };

  return (
    <main className="flex-1 flex flex-col min-w-0 min-h-0" style={{ background: "var(--bg)" }}>
      <header
        className="h-[52px] shrink-0 px-5 border-b flex items-center justify-between gap-3"
        style={{ borderColor: "var(--border)", background: "var(--bg)" }}
      >
        <h1 className="text-[14px] font-semibold truncate">{threadTitle}</h1>
        {!inspectorOpen && (
          <div
            className="inline-flex items-center p-0.5 rounded-lg border shrink-0"
            style={{ background: "var(--rail)", borderColor: "var(--border)" }}
          >
            {(
              [
                ["trace", "Trace", Activity],
                ["context", "上下文", Layers],
              ] as const
            ).map(([id, label, Icon]) => (
              <button
                key={id}
                type="button"
                onClick={() => onOpenInspector(id)}
                className="interactive-btn inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[12px] font-medium transition-colors"
                style={{
                  background: "transparent",
                  color: "var(--ink-secondary)",
                }}
              >
                <Icon size={13} />
                {label}
              </button>
            ))}
          </div>
        )}
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
                  {modelConfig.has_api_key
                    ? `DeepSeek · ${modelConfig.model}`
                    : "未配置 DeepSeek API Key · 请点击左下角设置"}
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
            items.map((item, idx) => {
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

              const isStreamingCurrent = loading && idx === items.length - 1;
              const showActions = !isStreamingCurrent && Boolean(item.content || item.traceId);

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

                  {showActions && (
                    <div className="pt-0.5 space-y-2">
                      <div className="flex items-center flex-wrap gap-1.5">
                        {item.content && (
                          <button
                            type="button"
                            onClick={() => handleCopy(item)}
                            className="interactive-btn inline-flex items-center gap-1 px-2 py-1 rounded-md border text-[11.5px]"
                            style={{
                              background: copiedId === item.id ? "var(--ok-soft)" : "var(--surface)",
                              borderColor: "var(--border)",
                              color: copiedId === item.id ? "var(--ok)" : "var(--ink-muted)",
                            }}
                          >
                            {copiedId === item.id ? <Check size={12} /> : <Copy size={12} />}
                            {copiedId === item.id ? "已复制" : "复制"}
                          </button>
                        )}

                        {item.traceId && (
                          <button
                            type="button"
                            disabled={busyFeedbackId === item.id}
                            onClick={() => handleFeedbackClick(item, "up")}
                            title={item.feedbackRating === "up" ? "再次点击取消点赞" : "点赞"}
                            className="interactive-btn inline-flex items-center gap-1 px-2 py-1 rounded-md border text-[11.5px] disabled:opacity-50"
                            style={{
                              background: item.feedbackRating === "up" ? "var(--ok-soft)" : "var(--surface)",
                              borderColor: item.feedbackRating === "up" ? "var(--ok)" : "var(--border)",
                              color: item.feedbackRating === "up" ? "var(--ok)" : "var(--ink-muted)",
                            }}
                          >
                            <ThumbsUp size={12} />
                            {item.feedbackRating === "up" ? "已点赞" : "点赞"}
                          </button>
                        )}

                        {item.traceId && (
                          <button
                            type="button"
                            disabled={busyFeedbackId === item.id}
                            onClick={() => handleFeedbackClick(item, "down")}
                            title={item.feedbackRating === "down" ? "再次点击取消点踩" : "点踩并记录到后端"}
                            className="interactive-btn inline-flex items-center gap-1 px-2 py-1 rounded-md border text-[11.5px] disabled:opacity-50"
                            style={{
                              background: item.feedbackRating === "down" ? "var(--danger-soft)" : "var(--surface)",
                              borderColor: item.feedbackRating === "down" ? "var(--danger)" : "var(--border)",
                              color: item.feedbackRating === "down" ? "var(--danger)" : "var(--ink-muted)",
                            }}
                          >
                            <ThumbsDown size={12} />
                            {item.feedbackRating === "down" ? "已点踩" : "点踩"}
                          </button>
                        )}

                        {item.traceId && (
                          <button
                            type="button"
                            onClick={() => onInspectTrace(item.traceId!)}
                            className="inline-flex items-center gap-1 px-1.5 py-1 font-mono text-[11px] hover:underline"
                            style={{ color: "var(--ink-muted)" }}
                          >
                            <Activity size={11} />
                            {item.traceId}
                          </button>
                        )}
                      </div>

                      {reasonTargetId === item.id && item.feedbackRating === "down" && (
                        <div
                          className="flex items-center gap-1.5 max-w-md px-2.5 py-1.5 rounded-lg border text-[12px]"
                          style={{ background: "var(--surface)", borderColor: "var(--border)" }}
                        >
                          <input
                            type="text"
                            value={reasonDraft}
                            onChange={(e) => setReasonDraft(e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") {
                                e.preventDefault();
                                handleSaveReason(item);
                              }
                            }}
                            placeholder="已记录点踩，可选补充原因（Enter 保存）..."
                            className="flex-1 bg-transparent outline-none text-[12px]"
                            style={{ color: "var(--ink)" }}
                          />
                          <button
                            type="button"
                            disabled={busyFeedbackId === item.id}
                            onClick={() => handleSaveReason(item)}
                            className="interactive-btn px-2 py-0.5 rounded text-[11.5px] font-medium text-white shrink-0"
                            style={{ background: "var(--accent)" }}
                          >
                            保存
                          </button>
                          <button
                            type="button"
                            aria-label="关闭原因输入"
                            onClick={() => setReasonTargetId(null)}
                            className="p-0.5 rounded shrink-0"
                            style={{ color: "var(--ink-muted)" }}
                          >
                            <X size={13} />
                          </button>
                        </div>
                      )}

                      {item.feedbackRating === "down" && item.feedbackReason && reasonTargetId !== item.id && (
                        <div className="text-[11px] px-0.5" style={{ color: "var(--ink-muted)" }}>
                          已记录点踩原因：{item.feedbackReason}
                        </div>
                      )}
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
