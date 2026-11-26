import React, { useEffect, useState } from "react";
import { Check, CheckCircle2, Code2, Copy, Eye, Power, Sparkles, X } from "lucide-react";
import { MarkdownContent } from "../../shared/components/MarkdownContent";

export interface SkillDrawerData {
  slug: string;
  version: number;
  status: "active" | "disabled";
  description?: string;
  content: string;
  loading?: boolean;
}

interface SkillDetailDrawerProps {
  skill: SkillDrawerData | null;
  toggling?: boolean;
  onToggleStatus?: (slug: string, nextStatus: "active" | "disabled") => void;
  onClose: () => void;
}

export const SkillDetailDrawer: React.FC<SkillDetailDrawerProps> = ({
  skill,
  toggling = false,
  onToggleStatus,
  onClose,
}) => {
  const [viewMode, setViewMode] = useState<"preview" | "source">("preview");
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    setViewMode("preview");
    setCopied(false);
  }, [skill?.slug]);

  useEffect(() => {
    if (!skill) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [skill, onClose]);

  if (!skill) return null;

  const isActive = skill.status === "active";
  const lineCount = skill.content ? skill.content.split("\n").length : 0;

  const handleCopy = async () => {
    if (!skill.content) return;
    try {
      await navigator.clipboard.writeText(skill.content);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      // ignore
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      {/* Frosted warm backdrop */}
      <div
        className="drawer-backdrop fixed inset-0 backdrop-blur-[2px]"
        style={{ background: "rgba(24, 22, 19, 0.28)" }}
        onClick={onClose}
      />

      {/* Floating editorial side sheet */}
      <aside
        role="dialog"
        aria-modal="true"
        className="drawer-sheet relative z-10 flex flex-col w-full sm:max-w-[660px] h-full sm:h-[calc(100vh-24px)] sm:m-3 sm:rounded-2xl border overflow-hidden"
        style={{
          background: "var(--surface)",
          borderColor: "var(--border-strong)",
          boxShadow: "0 24px 64px -16px rgba(20, 19, 17, 0.26), 0 4px 16px -4px rgba(20, 19, 17, 0.1)",
        }}
      >
        {/* Top Hero Header */}
        <div
          className="px-6 pt-5 pb-4 border-b shrink-0 space-y-3"
          style={{ borderColor: "var(--border)", background: "var(--surface)" }}
        >
          <div className="flex items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <span
                className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md font-mono text-[10.5px] font-semibold tracking-wide"
                style={{ background: "var(--accent-soft)", color: "var(--accent)" }}
              >
                <Sparkles size={11} />
                SKILL.md
              </span>
              {skill.version > 0 && (
                <span
                  className="font-mono text-[11px] px-2 py-0.5 rounded-md"
                  style={{ background: "var(--surface-subtle)", color: "var(--ink-secondary)" }}
                >
                  v{skill.version}
                </span>
              )}
              <span
                className="inline-flex items-center gap-1.5 font-mono text-[11px] px-2 py-0.5 rounded-full"
                style={{
                  background: isActive ? "var(--ok-soft)" : "var(--surface-subtle)",
                  color: isActive ? "var(--ok)" : "var(--ink-muted)",
                }}
              >
                <span
                  className="w-1.5 h-1.5 rounded-full"
                  style={{ background: isActive ? "var(--ok)" : "var(--ink-muted)" }}
                />
                {isActive ? "生效中" : "已下架"}
              </span>
            </div>

            <button
              type="button"
              onClick={onClose}
              aria-label="关闭侧边栏"
              className="interactive-btn w-8 h-8 rounded-full flex items-center justify-center border"
              style={{
                background: "var(--bg)",
                borderColor: "var(--border)",
                color: "var(--ink-secondary)",
              }}
            >
              <X size={15} />
            </button>
          </div>

          <div className="space-y-1">
            <h2 className="font-mono text-[18px] font-semibold tracking-tight leading-snug">
              {skill.slug}
            </h2>
            {skill.description && (
              <p className="text-[13px] leading-relaxed" style={{ color: "var(--ink-secondary)" }}>
                {skill.description}
              </p>
            )}
          </div>

          {/* Clean Action Toolbar */}
          <div className="flex items-center justify-between gap-3 pt-1">
            <div
              className="inline-flex items-center p-0.5 rounded-lg border"
              style={{ background: "var(--bg)", borderColor: "var(--border)" }}
            >
              <button
                type="button"
                onClick={() => setViewMode("preview")}
                className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[12px] font-medium transition-colors"
                style={{
                  background: viewMode === "preview" ? "var(--surface)" : "transparent",
                  color: viewMode === "preview" ? "var(--ink)" : "var(--ink-muted)",
                  boxShadow: viewMode === "preview" ? "0 1px 2px rgba(0,0,0,0.06)" : "none",
                }}
              >
                <Eye size={12.5} />
                渲染预览
              </button>
              <button
                type="button"
                onClick={() => setViewMode("source")}
                className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[12px] font-medium transition-colors"
                style={{
                  background: viewMode === "source" ? "var(--surface)" : "transparent",
                  color: viewMode === "source" ? "var(--ink)" : "var(--ink-muted)",
                  boxShadow: viewMode === "source" ? "0 1px 2px rgba(0,0,0,0.06)" : "none",
                }}
              >
                <Code2 size={12.5} />
                原文
              </button>
            </div>

            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={handleCopy}
                disabled={skill.loading || !skill.content}
                className="interactive-btn inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border text-[12px] disabled:opacity-40"
                style={{
                  background: "var(--bg)",
                  borderColor: "var(--border)",
                  color: copied ? "var(--ok)" : "var(--ink-secondary)",
                }}
              >
                {copied ? <Check size={12.5} /> : <Copy size={12.5} />}
                {copied ? "已复制" : "复制"}
              </button>

              {onToggleStatus && (
                <button
                  type="button"
                  disabled={toggling}
                  onClick={() => onToggleStatus(skill.slug, isActive ? "disabled" : "active")}
                  className="interactive-btn inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-[12px] font-medium disabled:opacity-40"
                  style={{
                    background: isActive ? "var(--bg)" : "var(--ok-soft)",
                    borderColor: isActive ? "var(--border-strong)" : "var(--ok)",
                    color: isActive ? "var(--danger)" : "var(--ok)",
                  }}
                >
                  {isActive ? <Power size={12.5} /> : <CheckCircle2 size={12.5} />}
                  {toggling ? "处理中..." : isActive ? "下架" : "重新生效"}
                </button>
              )}
            </div>
          </div>
        </div>

        {/* Warm Editorial Reading Canvas */}
        <div className="flex-1 overflow-y-auto px-7 py-6" style={{ background: "var(--bg)" }}>
          {skill.loading ? (
            <div className="space-y-3 py-4 animate-pulse">
              <div className="h-5 w-1/3 rounded" style={{ background: "var(--surface-subtle)" }} />
              <div className="h-3.5 w-full rounded" style={{ background: "var(--surface-subtle)" }} />
              <div className="h-3.5 w-5/6 rounded" style={{ background: "var(--surface-subtle)" }} />
              <div className="h-3.5 w-4/6 rounded" style={{ background: "var(--surface-subtle)" }} />
            </div>
          ) : viewMode === "source" ? (
            <pre
              className="rounded-xl border p-4 font-mono text-[12.2px] leading-relaxed overflow-x-auto whitespace-pre-wrap break-words"
              style={{
                background: "var(--code-bg)",
                borderColor: "var(--border)",
                color: "var(--code-ink)",
              }}
            >
              {skill.content}
            </pre>
          ) : (
            <div
              className="rounded-xl border px-6 py-5"
              style={{ background: "var(--surface)", borderColor: "var(--border)" }}
            >
              <MarkdownContent content={skill.content} />
            </div>
          )}
        </div>

        {/* Quiet Metadata Footer */}
        <div
          className="px-6 py-2.5 border-t flex items-center justify-between font-mono text-[11px] shrink-0"
          style={{
            borderColor: "var(--border)",
            background: "var(--surface)",
            color: "var(--ink-muted)",
          }}
        >
          <span>按 Esc 或点击空白处关闭</span>
          <span>
            {skill.content.length} 字符 · {lineCount} 行
          </span>
        </div>
      </aside>
    </div>
  );
};
