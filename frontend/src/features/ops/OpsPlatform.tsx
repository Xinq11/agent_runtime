import React, { useCallback, useEffect, useState } from "react";
import {
  ArrowLeft,
  BookOpen,
  FileText,
  MessageSquare,
  Moon,
  RefreshCw,
  Sun,
} from "lucide-react";
import { fetchOpsOverview } from "../../shared/api/client";
import { OpsOverview } from "../../shared/types/runtime";
import { ContextTab } from "./ContextTab";
import { KnowledgeTab } from "./KnowledgeTab";
import { SkillsTab } from "./SkillsTab";

interface OpsPlatformProps {
  dark: boolean;
  onToggleTheme: () => void;
  onBackToChat: (jumpThreadId?: string) => void;
}

type OpsTab = "context" | "kb" | "skills";

const TABS = [
  ["context", "会话上下文与压缩透视", MessageSquare],
  ["kb", "知识库管理", BookOpen],
  ["skills", "Skills 技能库", FileText],
] as const;

export const OpsPlatform: React.FC<OpsPlatformProps> = ({
  dark,
  onToggleTheme,
  onBackToChat,
}) => {
  const [tab, setTab] = useState<OpsTab>("context");
  const [overview, setOverview] = useState<OpsOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null);

  const loadOverview = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchOpsOverview();
      setOverview(data);
      setSelectedThreadId((cur) => cur || data.threads[0]?.thread_id || null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadOverview();
  }, [loadOverview]);

  return (
    <div
      className="h-screen w-screen flex flex-col overflow-hidden"
      style={{ background: "var(--bg)", color: "var(--ink)" }}
    >
      <header
        className="h-[56px] shrink-0 px-5 border-b flex items-center justify-between gap-4"
        style={{ background: "var(--surface)", borderColor: "var(--border)" }}
      >
        <div className="flex items-center gap-3 min-w-0">
          <button
            onClick={() => onBackToChat()}
            className="interactive-btn inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-[12.5px] font-medium"
            style={{ background: "var(--bg)", borderColor: "var(--border-strong)", color: "var(--ink)" }}
          >
            <ArrowLeft size={14} />
            返回对话工作台
          </button>
          <div className="h-5 w-px" style={{ background: "var(--border)" }} />
          <div className="flex items-center gap-2 min-w-0">
            <div
              className="w-6 h-6 rounded-md flex items-center justify-center font-mono text-[11px] font-bold text-white shrink-0"
              style={{ background: "var(--accent)" }}
            >
              OP
            </div>
            <span className="text-[14.5px] font-semibold truncate">
              Agent Runtime · 管理后台
            </span>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={loadOverview}
            className="interactive-btn inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border text-[12px]"
            style={{ background: "var(--bg)", borderColor: "var(--border)", color: "var(--ink-secondary)" }}
          >
            <RefreshCw size={13} className={loading ? "animate-spin" : ""} />
            刷新
          </button>
          <button
            onClick={onToggleTheme}
            aria-label="切换主题"
            className="interactive-btn p-1.5 rounded-lg border"
            style={{ background: "var(--bg)", borderColor: "var(--border)", color: "var(--ink-secondary)" }}
          >
            {dark ? <Sun size={14} /> : <Moon size={14} />}
          </button>
        </div>
      </header>

      <nav
        className="px-6 border-b flex items-center gap-1 shrink-0 overflow-x-auto"
        style={{ background: "var(--rail)", borderColor: "var(--border)" }}
      >
        {TABS.map(([id, label, Icon]) => {
          const active = tab === id;
          return (
            <button
              key={id}
              onClick={() => setTab(id)}
              className="inline-flex items-center gap-1.5 px-3.5 py-3 text-[13px] font-medium border-b-2 transition-colors whitespace-nowrap"
              style={{
                borderColor: active ? "var(--accent)" : "transparent",
                color: active ? "var(--ink)" : "var(--ink-muted)",
              }}
            >
              <Icon size={14} style={{ color: active ? "var(--accent)" : "var(--ink-muted)" }} />
              {label}
            </button>
          );
        })}
      </nav>

      <div className="flex-1 overflow-y-auto p-6">
        <div className="max-w-6xl mx-auto space-y-5">
          {tab === "context" && (
            <ContextTab
              threads={overview?.threads || []}
              selectedThreadId={selectedThreadId}
              onSelectThread={setSelectedThreadId}
              onOpenInChat={(tid) => onBackToChat(tid)}
            />
          )}
          {tab === "kb" && (
            <KnowledgeTab chunks={overview?.knowledge_chunks || []} onRefresh={loadOverview} />
          )}
          {tab === "skills" && (
            <SkillsTab skills={overview?.skills || []} onRefresh={loadOverview} />
          )}
        </div>
      </div>
    </div>
  );
};
