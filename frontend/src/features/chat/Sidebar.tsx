import React, { useMemo, useState } from "react";
import {
  Activity,
  ArrowUpRight,
  Check,
  ChevronDown,
  ChevronRight,
  MessageSquare,
  Moon,
  Plus,
  Settings,
  Sun,
  Trash2,
  X,
} from "lucide-react";
import { AppMeta, ModelConfig, ThreadSummary } from "../../shared/types/runtime";

function getThreadGroupLabel(createdAt?: string): string {
  if (!createdAt) return "更早";
  const dt = new Date(createdAt);
  if (Number.isNaN(dt.getTime())) return "更早";

  const now = new Date();
  const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const itemStart = new Date(dt.getFullYear(), dt.getMonth(), dt.getDate()).getTime();
  const diffDays = Math.floor((todayStart - itemStart) / 86400000);

  if (diffDays > 7) return "更早";

  const yyyy = dt.getFullYear();
  const mm = String(dt.getMonth() + 1).padStart(2, "0");
  const dd = String(dt.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

interface SidebarProps {
  appMeta?: AppMeta | null;
  threads: ThreadSummary[];
  activeThreadId: string;
  isDraftThread: boolean;
  onSelectThread: (id: string) => void;
  onNewThread: () => void;
  onDeleteThread: (id: string) => Promise<void>;
  modelConfig: ModelConfig;
  onOpenSettings: () => void;
  onOpenOpsPlatform: () => void;
  dark: boolean;
  onToggleTheme: () => void;
}

export const Sidebar: React.FC<SidebarProps> = ({
  appMeta,
  threads,
  activeThreadId,
  isDraftThread,
  onSelectThread,
  onNewThread,
  onDeleteThread,
  modelConfig,
  onOpenSettings,
  onOpenOpsPlatform,
  dark,
  onToggleTheme,
}) => {
  const [collapsedGroups, setCollapsedGroups] = useState<Record<string, boolean>>({});
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const handleConfirmDelete = async (id: string) => {
    setDeletingId(id);
    try {
      await onDeleteThread(id);
    } finally {
      setDeletingId(null);
      setConfirmDeleteId(null);
    }
  };

  const toggleGroup = (label: string) => {
    setCollapsedGroups((prev) => ({ ...prev, [label]: !prev[label] }));
  };

  const groupedThreads = useMemo(() => {
    const groups: Array<{ label: string; items: ThreadSummary[] }> = [];
    const indexByLabel = new Map<string, number>();
    for (const t of threads) {
      const label = getThreadGroupLabel(t.created_at);
      const existing = indexByLabel.get(label);
      if (existing !== undefined) {
        groups[existing].items.push(t);
      } else {
        indexByLabel.set(label, groups.length);
        groups.push({ label, items: [t] });
      }
    }
    return groups;
  }, [threads]);

  return (
    <aside
      className="w-[258px] shrink-0 flex flex-col border-r select-none"
      style={{ background: "var(--rail)", borderColor: "var(--border)" }}
    >
      {/* Top brand + New Draft */}
      <div className="px-4 pt-4 pb-3 border-b" style={{ borderColor: "var(--border)" }}>
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2.5">
            <div
              className="w-7 h-7 rounded-lg flex items-center justify-center font-mono text-xs font-bold text-white"
              style={{ background: "var(--accent)" }}
            >
              AR
            </div>
            <div>
              <div className="text-[13.5px] font-semibold tracking-tight leading-tight">
                {appMeta?.app_name || "Agent Runtime"}
              </div>
              <div className="text-[11px] font-mono" style={{ color: "var(--ink-muted)" }}>
                {appMeta?.subtitle || "Workspace"}
              </div>
            </div>
          </div>
          <button
            onClick={onToggleTheme}
            aria-label="切换浅色或深色主题"
            className="interactive-btn p-1.5 rounded-md border"
            style={{
              background: "var(--surface)",
              borderColor: "var(--border)",
              color: "var(--ink-secondary)",
            }}
          >
            {dark ? <Sun size={14} /> : <Moon size={14} />}
          </button>
        </div>

        <button
          type="button"
          onClick={onNewThread}
          title="新建对话"
          className="interactive-btn w-full flex items-center justify-center gap-1.5 py-2 px-3 rounded-lg text-[13px] font-medium text-white"
          style={{ background: "var(--accent)" }}
        >
          <Plus size={15} />
          新建对话
        </button>
      </div>

      {/* Persisted Non-Empty Threads */}
      <div className="flex-1 overflow-y-auto px-3 py-3">
        <div
          className="flex items-center justify-between text-[11px] font-medium uppercase tracking-wider px-1 mb-1.5"
          style={{ color: "var(--ink-muted)" }}
        >
          <span>历史对话</span>
          <span className="font-mono">{threads.length}</span>
        </div>
        <div className="space-y-3">
          {threads.length === 0 ? (
            <div className="px-2 py-4 text-[12px]" style={{ color: "var(--ink-muted)" }}>
              发送首条消息后自动保存
            </div>
          ) : (
            groupedThreads.map((group) => {
              const collapsed = Boolean(collapsedGroups[group.label]);
              return (
                <div key={group.label} className="space-y-0.5">
                  <button
                    type="button"
                    onClick={() => toggleGroup(group.label)}
                    className="w-full flex items-center justify-between gap-1.5 px-2 py-1 rounded-md text-left hover:bg-black/5 dark:hover:bg-white/5 transition-colors"
                    style={{ color: "var(--ink-muted)" }}
                  >
                    <span className="inline-flex items-center gap-1.5 font-mono text-[10.5px] font-medium">
                      {collapsed ? (
                        <ChevronRight size={12} className="shrink-0" />
                      ) : (
                        <ChevronDown size={12} className="shrink-0" />
                      )}
                      {group.label}
                    </span>
                    {collapsed && (
                      <span className="font-mono text-[10px] opacity-70">
                        {group.items.length}
                      </span>
                    )}
                  </button>

                  {!collapsed &&
                    group.items.map((t) => {
                      const active = !isDraftThread && t.thread_id === activeThreadId;
                      const confirming = confirmDeleteId === t.thread_id;
                      const deleting = deletingId === t.thread_id;
                      return (
                        <div
                          key={t.thread_id}
                          className="group relative flex items-center rounded-lg transition-colors"
                          style={{
                            background: active ? "var(--surface)" : "transparent",
                            border: active ? "1px solid var(--border)" : "1px solid transparent",
                          }}
                        >
                          <button
                            type="button"
                            onClick={() => {
                              setConfirmDeleteId(null);
                              onSelectThread(t.thread_id);
                            }}
                            className="flex-1 min-w-0 flex items-center gap-2 px-2.5 py-2 text-left text-[12.5px]"
                            style={{
                              color: active ? "var(--ink)" : "var(--ink-secondary)",
                              fontWeight: active ? 600 : 400,
                            }}
                          >
                            <MessageSquare size={13} className="shrink-0 opacity-65" />
                            <span className="truncate">{t.title || t.thread_id}</span>
                          </button>

                          {confirming ? (
                            <div className="flex items-center gap-0.5 pr-1.5 shrink-0">
                              <button
                                type="button"
                                disabled={deleting}
                                onClick={(e) => {
                                  e.stopPropagation();
                                  handleConfirmDelete(t.thread_id);
                                }}
                                title="确认删除"
                                aria-label="确认删除对话"
                                className="p-1 rounded-md transition-colors disabled:opacity-40"
                                style={{ background: "var(--danger-soft)", color: "var(--danger)" }}
                              >
                                <Check size={12.5} />
                              </button>
                              <button
                                type="button"
                                disabled={deleting}
                                onClick={(e) => {
                                  e.stopPropagation();
                                  setConfirmDeleteId(null);
                                }}
                                title="取消"
                                aria-label="取消删除"
                                className="p-1 rounded-md transition-colors"
                                style={{ color: "var(--ink-muted)" }}
                              >
                                <X size={12.5} />
                              </button>
                            </div>
                          ) : (
                            <button
                              type="button"
                              onClick={(e) => {
                                e.stopPropagation();
                                setConfirmDeleteId(t.thread_id);
                              }}
                              title="删除对话"
                              aria-label="删除对话"
                              className="opacity-0 group-hover:opacity-100 focus:opacity-100 p-1.5 mr-1 rounded-md transition-opacity shrink-0 hover:bg-black/5"
                              style={{ color: "var(--ink-muted)" }}
                            >
                              <Trash2 size={12.5} />
                            </button>
                          )}
                        </div>
                      );
                    })}
                </div>
              );
            })
          )}
        </div>
      </div>

      {/* Bottom: Single Unified Ops Platform Portal + Model Settings */}
      <div className="p-3 border-t space-y-2" style={{ borderColor: "var(--border)" }}>
        <button
          onClick={onOpenOpsPlatform}
          className="interactive-btn w-full flex items-center justify-between px-3 py-2.5 rounded-lg border text-left"
          style={{
            background: "var(--surface)",
            borderColor: "var(--border-strong)",
          }}
        >
          <div className="flex items-center gap-2 min-w-0">
            <Activity size={14} style={{ color: "var(--accent)" }} />
            <div className="min-w-0">
              <div className="text-[12.5px] font-semibold leading-tight">
                管理后台
              </div>
              <div className="text-[10.5px] truncate mt-0.5" style={{ color: "var(--ink-muted)" }}>
                知识库 · Skills
              </div>
            </div>
          </div>
          <ArrowUpRight size={14} style={{ color: "var(--ink-muted)" }} />
        </button>

        <button
          onClick={onOpenSettings}
          className="interactive-btn w-full flex items-center justify-between px-3 py-2 rounded-lg border text-left"
          style={{
            background: "var(--surface)",
            borderColor: "var(--border)",
          }}
        >
          <div className="min-w-0">
            <div className="text-[12px] font-semibold flex items-center gap-1.5">
              <span
                className="w-2 h-2 rounded-full shrink-0"
                style={{
                  background: modelConfig.has_api_key ? "var(--ok)" : "var(--warn)",
                }}
              />
              <span className="truncate font-mono">
                {modelConfig.has_api_key ? modelConfig.model : "未配置 API Key"}
              </span>
            </div>
            <div
              className="text-[11px] truncate mt-0.5"
              style={{ color: "var(--ink-muted)" }}
            >
              {modelConfig.has_api_key ? "DeepSeek 已接入" : "点击配置 DeepSeek Key"}
            </div>
          </div>
          <Settings size={14} style={{ color: "var(--ink-muted)" }} />
        </button>
      </div>
    </aside>
  );
};
