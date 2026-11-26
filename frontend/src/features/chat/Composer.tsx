import React, { useEffect, useRef, useState } from "react";
import { ArrowUp, Square } from "lucide-react";
import { ModelConfig } from "../../shared/types/runtime";

interface ComposerProps {
  loading: boolean;
  focusNonce?: number;
  modelConfig: ModelConfig;
  onSend: (text: string) => void;
  onAbort?: () => void;
}

export const Composer: React.FC<ComposerProps> = ({
  loading,
  focusNonce,
  modelConfig,
  onSend,
  onAbort,
}) => {
  const [input, setInput] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (focusNonce) {
      textareaRef.current?.focus();
    }
  }, [focusNonce]);

  const submit = () => {
    const trimmed = input.trim();
    if (!trimmed || loading) return;
    setInput("");
    onSend(trimmed);
  };

  return (
    <div className="px-5 pb-4 pt-2 shrink-0">
      <div
        className="max-w-3xl mx-auto rounded-2xl border p-2.5 shadow-sm"
        style={{ background: "var(--surface)", borderColor: "var(--border-strong)" }}
      >
        <textarea
          ref={textareaRef}
          rows={2}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          placeholder="输入问题，Enter 发送 · Shift+Enter 换行..."
          className="w-full resize-none bg-transparent px-2 py-1 text-[14px] outline-none leading-relaxed"
          style={{ color: "var(--ink)" }}
        />
        <div className="flex items-center justify-between pt-1.5 px-1">
          <span className="text-[11.5px] font-mono" style={{ color: "var(--ink-muted)" }}>
            {modelConfig.mode === "deepseek" ? modelConfig.model : "mock-runtime"} ·
            支持 Markdown 表格与交互图表
          </span>
          {loading ? (
            <button
              type="button"
              onClick={onAbort}
              title="停止生成"
              aria-label="停止生成"
              className="interactive-btn w-8 h-8 rounded-lg flex items-center justify-center text-white"
              style={{ background: "var(--danger)" }}
            >
              <Square size={13} fill="currentColor" />
            </button>
          ) : (
            <button
              type="button"
              onClick={submit}
              disabled={!input.trim()}
              aria-label="发送消息"
              className="interactive-btn w-8 h-8 rounded-lg flex items-center justify-center text-white disabled:opacity-40"
              style={{ background: "var(--accent)" }}
            >
              <ArrowUp size={16} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
};
