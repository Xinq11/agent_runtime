import { useCallback, useEffect, useRef, useState } from "react";
import {
  abortChatRun,
  deleteThread,
  fetchAppMeta,
  fetchModelConfig,
  fetchRuntimeState,
  fetchThreadMessages,
  fetchThreads,
  resumeApproval,
  streamChatSSE,
  updateModelConfig,
} from "../../shared/api/client";
import {
  AppMeta,
  ApprovalTicket,
  ChatTurnItem,
  ModelConfig,
  RuntimeStateSnapshot,
  ThreadSummary,
} from "../../shared/types/runtime";
import { applyStreamEventToItem } from "./sseReducer";

function uid(prefix: string) {
  return `${prefix}_${Math.random().toString(16).slice(2, 10)}`;
}

export function useChatSession(route: "chat" | "ops") {
  const [threads, setThreads] = useState<ThreadSummary[]>([]);
  const [activeThreadId, setActiveThreadId] = useState<string>(() => uid("th"));
  const activeThreadIdRef = useRef(activeThreadId);
  activeThreadIdRef.current = activeThreadId;

  const [isDraftThread, setIsDraftThread] = useState(true);
  const [composerFocusNonce, setComposerFocusNonce] = useState(0);
  const [items, setItems] = useState<ChatTurnItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [selectedTraceId, setSelectedTraceId] = useState<string | null>(null);
  const [stateSnap, setStateSnap] = useState<RuntimeStateSnapshot | null>(null);
  const [appMeta, setAppMeta] = useState<AppMeta | null>(null);
  const [modelConfig, setModelConfig] = useState<ModelConfig>({
    mode: "mock",
    model: "deepseek-chat",
    base_url: "https://api.deepseek.com/v1",
    has_api_key: false,
  });

  const refreshAll = useCallback(
    async (tid = activeThreadId, trId = selectedTraceId, draft = isDraftThread) => {
      try {
        const [cfg, ths, meta] = await Promise.all([
          fetchModelConfig(),
          fetchThreads(),
          fetchAppMeta().catch(() => null),
        ]);
        setModelConfig(cfg);
        setThreads(ths);
        if (meta) setAppMeta(meta);
        if (!draft) {
          setStateSnap(await fetchRuntimeState(tid, trId));
        } else {
          setStateSnap(null);
        }
      } catch {
        // Backend may still be booting
      }
    },
    [activeThreadId, isDraftThread, selectedTraceId]
  );

  useEffect(() => {
    if (route === "chat") refreshAll();
  }, [route, refreshAll]);

  const selectThread = async (threadId: string) => {
    activeThreadIdRef.current = threadId;
    setActiveThreadId(threadId);
    setIsDraftThread(false);
    setLoading(false);
    setSelectedTraceId(null);
    const msgs = await fetchThreadMessages(threadId);
    if (activeThreadIdRef.current !== threadId) return;
    const rebuilt: ChatTurnItem[] = msgs
      .filter((m) => m.role === "user" || (m.role === "assistant" && m.content))
      .map((m, idx) => ({
        id: `hist_${idx}_${m.msg_id ?? idx}`,
        role: m.role === "user" ? "user" : "assistant",
        content: String(m.content || ""),
        traceId:
          m.role === "assistant" &&
          m.metadata &&
          typeof (m.metadata as Record<string, unknown>).trace_id === "string"
            ? String((m.metadata as Record<string, unknown>).trace_id)
            : undefined,
        ts: Date.now() + idx,
      }));
    setItems(rebuilt);
    const snap = await fetchRuntimeState(threadId, null);
    if (activeThreadIdRef.current !== threadId) return;
    setStateSnap(snap);
  };

  const newThread = () => {
    if (!isDraftThread || items.length > 0) {
      const nextId = uid("th");
      activeThreadIdRef.current = nextId;
      setActiveThreadId(nextId);
      setIsDraftThread(true);
      setLoading(false);
      setItems([]);
      setSelectedTraceId(null);
      setStateSnap(null);
    }
    setComposerFocusNonce((n) => n + 1);
  };

  const removeThread = async (threadId: string) => {
    await deleteThread(threadId);
    setThreads((prev) => prev.filter((t) => t.thread_id !== threadId));
    if (activeThreadIdRef.current === threadId) {
      const nextId = uid("th");
      activeThreadIdRef.current = nextId;
      setActiveThreadId(nextId);
      setIsDraftThread(true);
      setLoading(false);
      setActiveRunId(null);
      setItems([]);
      setSelectedTraceId(null);
      setStateSnap(null);
    }
  };

  const sendMessage = async (text: string) => {
    const targetThreadId = activeThreadId;
    const wasDraft = isDraftThread;
    setIsDraftThread(false);

    if (wasDraft) {
      const optimisticTitle = text.trim().replace(/\s+/g, " ").slice(0, 36) || "新对话";
      setThreads((prev) =>
        prev.some((t) => t.thread_id === targetThreadId)
          ? prev
          : [
              {
                thread_id: targetThreadId,
                title: optimisticTitle,
                created_at: new Date().toISOString(),
                message_count: 1,
              },
              ...prev,
            ]
      );
    }

    const asstId = uid("a");
    setItems((prev) => [
      ...prev,
      { id: uid("u"), role: "user", content: text, ts: Date.now() },
      { id: asstId, role: "assistant", content: "", activities: [], ts: Date.now() },
    ]);
    setLoading(true);
    let turnTraceId: string | null = null;

    try {
      await streamChatSSE({
        threadId: targetThreadId,
        message: text,
        onEvent: (event, data) => {
          if (activeThreadIdRef.current !== targetThreadId) return;
          if (event === "meta") {
            if (typeof data.run_id === "string" && data.run_id) setActiveRunId(data.run_id);
            if (typeof data.trace_id === "string") {
              turnTraceId = data.trace_id;
              setSelectedTraceId(data.trace_id);
            }
          }
          setItems((prev) => applyStreamEventToItem(prev, asstId, event, data));
        },
      });
    } catch (err) {
      if (activeThreadIdRef.current === targetThreadId) {
        const msg = `连接后端失败：${err instanceof Error ? err.message : String(err)}`;
        setItems((prev) => prev.map((it) => (it.id === asstId ? { ...it, content: msg } : it)));
      }
    } finally {
      if (activeThreadIdRef.current === targetThreadId) {
        setLoading(false);
        setActiveRunId(null);
        await refreshAll(targetThreadId, turnTraceId, false);
      } else {
        const ths = await fetchThreads().catch(() => null);
        if (ths) setThreads(ths);
      }
    }
  };

  const abortRun = async () => {
    try {
      await abortChatRun({ runId: activeRunId, threadId: activeThreadId });
    } catch {
      // ignore if already finished
    }
  };

  const decideApproval = async (itemId: string, ticket: ApprovalTicket, approved: boolean) => {
    setItems((prev) =>
      prev.map((it) => (it.id === itemId ? { ...it, approvalStatus: "submitting" } : it))
    );
    try {
      const res = await resumeApproval({
        reviewId: ticket.review_id,
        threadId: ticket.thread_id,
        approved,
      });
      setItems((prev) =>
        prev.map((it) =>
          it.id === itemId
            ? { ...it, approvalStatus: approved ? "approved" : "rejected", content: res.final_answer || it.content }
            : it
        )
      );
      await refreshAll(ticket.thread_id, res.trace_id || selectedTraceId, false);
    } catch (err) {
      setItems((prev) =>
        prev.map((it) =>
          it.id === itemId
            ? {
                ...it,
                approvalStatus: "pending",
                content: `审批恢复失败：${err instanceof Error ? err.message : String(err)}`,
              }
            : it
        )
      );
    }
  };

  const saveModelConfig = async (payload: Parameters<typeof updateModelConfig>[0]) => {
    const updated = await updateModelConfig(payload);
    setModelConfig(updated);
  };

  const activeThreadTitle = isDraftThread
    ? "新对话"
    : threads.find((t) => t.thread_id === activeThreadId)?.title || "新对话";

  return {
    threads,
    activeThreadId,
    activeThreadTitle,
    isDraftThread,
    composerFocusNonce,
    items,
    loading,
    selectedTraceId,
    stateSnap,
    modelConfig,
    appMeta,
    setSelectedTraceId,
    refreshAll,
    selectThread,
    newThread,
    removeThread,
    sendMessage,
    abortRun,
    decideApproval,
    saveModelConfig,
  };
}
