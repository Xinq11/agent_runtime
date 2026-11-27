import { useCallback, useEffect, useRef, useState } from "react";
import {
  abortChatRun,
  cancelMessageFeedback,
  deleteThread,
  downloadThreadFile,
  fetchAppMeta,
  fetchModelConfig,
  fetchRuntimeState,
  fetchThreadMessages,
  fetchThreads,
  resumeApproval,
  streamChatSSE,
  submitMessageFeedback,
  updateModelConfig,
  uploadThreadFile,
} from "../../shared/api/client";
import {
  ActivityBlock,
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

export function useChatSession() {
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
    mode: "deepseek",
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
    refreshAll();
  }, [refreshAll]);

  const selectThread = async (threadId: string) => {
    activeThreadIdRef.current = threadId;
    setActiveThreadId(threadId);
    setIsDraftThread(false);
    setLoading(false);
    setSelectedTraceId(null);
    const msgs = await fetchThreadMessages(threadId);
    if (activeThreadIdRef.current !== threadId) return;

    // Group raw session_messages by user turn so multi-step tool calls render as
    // ONE unified assistant response with ONE bottom action bar (matching live SSE).
    const rebuilt: ChatTurnItem[] = [];
    let pendingGroup: Array<Record<string, unknown>> = [];

    const flushAssistantGroup = (group: Array<Record<string, unknown>>) => {
      if (group.length === 0) return;
      const toolResultsById = new Map<string, string>();
      for (const m of group) {
        if (m.role === "tool" && m.tool_call_id) {
          toolResultsById.set(String(m.tool_call_id), String(m.content || ""));
        }
      }

      const activities: ActivityBlock[] = [];
      const assistantMsgs = group.filter((m) => m.role === "assistant");
      for (const am of assistantMsgs) {
        const calls = Array.isArray(am.tool_calls)
          ? (am.tool_calls as Array<Record<string, unknown>>)
          : [];
        for (const tc of calls) {
          const tcId = String(tc.id || uid("tc"));
          const tcName = String(tc.name || "tool");
          const rawResult = toolResultsById.get(tcId) || "";
          const isError =
            rawResult.includes('"ok": false') || rawResult.startsWith("[cancelled]");
          activities.push({
            id: `hist_act_${tcId}`,
            kind: tcName.startsWith("call_") ? "subagent" : "tool",
            name: tcName,
            phase: isError ? "error" : "done",
            args:
              tc.args && typeof tc.args === "object"
                ? (tc.args as Record<string, unknown>)
                : undefined,
            ok: !isError,
            preview: rawResult ? rawResult.slice(0, 320) : undefined,
          });
        }
      }

      const finalMsgs = assistantMsgs.filter((m) => {
        const calls = Array.isArray(m.tool_calls) ? m.tool_calls : [];
        return calls.length === 0 && Boolean(String(m.content || "").trim());
      });
      const fallbackTexts = assistantMsgs
        .map((m) => String(m.content || "").trim())
        .filter(Boolean);
      const primaryMsg =
        finalMsgs[finalMsgs.length - 1] || assistantMsgs[assistantMsgs.length - 1];
      const content =
        finalMsgs.length > 0
          ? finalMsgs.map((m) => String(m.content || "").trim()).join("\n\n")
          : fallbackTexts.join("\n\n");

      if (!content && activities.length === 0) return;

      let traceId: string | undefined;
      let feedbackRating: "up" | "down" | undefined;
      let feedbackReason: string | undefined;
      for (let i = assistantMsgs.length - 1; i >= 0; i -= 1) {
        const am = assistantMsgs[i];
        const meta =
          am.metadata && typeof am.metadata === "object"
            ? (am.metadata as Record<string, unknown>)
            : null;
        if (!traceId && meta && typeof meta.trace_id === "string" && meta.trace_id) {
          traceId = meta.trace_id;
        }
        const fb =
          am.feedback && typeof am.feedback === "object"
            ? (am.feedback as Record<string, unknown>)
            : null;
        if (!feedbackRating && fb && (fb.rating === "up" || fb.rating === "down")) {
          feedbackRating = fb.rating as "up" | "down";
          feedbackReason = typeof fb.reason === "string" ? fb.reason : undefined;
        }
      }

      const primaryId = primaryMsg?.msg_id ?? rebuilt.length;
      rebuilt.push({
        id: `hist_a_${primaryId}_${rebuilt.length}`,
        role: "assistant",
        content,
        activities: activities.length > 0 ? activities : undefined,
        msgId: typeof primaryMsg?.msg_id === "number" ? primaryMsg.msg_id : undefined,
        traceId,
        feedbackRating,
        feedbackReason,
        ts: Date.now() + rebuilt.length,
      });
    };

    for (const m of msgs) {
      if (m.role === "user") {
        flushAssistantGroup(pendingGroup);
        pendingGroup = [];
        rebuilt.push({
          id: `hist_u_${m.msg_id ?? rebuilt.length}`,
          role: "user",
          content: String(m.content || ""),
          msgId: typeof m.msg_id === "number" ? m.msg_id : undefined,
          ts: Date.now() + rebuilt.length,
        });
      } else if (m.role === "assistant" || m.role === "tool") {
        pendingGroup.push(m);
      }
    }
    flushAssistantGroup(pendingGroup);
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

  const setFeedback = async (
    itemId: string,
    rating: "up" | "down",
    opts?: { reason?: string; keepActive?: boolean }
  ): Promise<"up" | "down" | null> => {
    const target = items.find((it) => it.id === itemId);
    if (!target || !target.traceId) return null;
    const tid = activeThreadIdRef.current;
    const shouldSave = Boolean(opts?.keepActive) || target.feedbackRating !== rating;
    if (shouldSave) {
      const reason =
        rating === "down"
          ? opts?.reason !== undefined
            ? opts.reason
            : target.feedbackReason || ""
          : "";
      await submitMessageFeedback(tid, {
        trace_id: target.traceId,
        rating,
        reason,
        msg_id: target.msgId,
        answer_excerpt: target.content.slice(0, 400),
      });
      setItems((prev) =>
        prev.map((it) =>
          it.id === itemId
            ? { ...it, feedbackRating: rating, feedbackReason: reason.trim() }
            : it
        )
      );
      return rating;
    }
    await cancelMessageFeedback(tid, target.traceId);
    setItems((prev) =>
      prev.map((it) =>
        it.id === itemId ? { ...it, feedbackRating: undefined, feedbackReason: "" } : it
      )
    );
    return null;
  };

  const uploadAttachment = async (file: File) => {
    const tid = activeThreadIdRef.current;
    const uploaded = await uploadThreadFile(tid, file);
    if (activeThreadIdRef.current === tid) {
      setIsDraftThread(false);
      await refreshAll(tid, selectedTraceId, false);
    }
    return uploaded;
  };

  const downloadFile = async (path: string) => {
    await downloadThreadFile(activeThreadIdRef.current, path);
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
    setFeedback,
    uploadAttachment,
    downloadFile,
    saveModelConfig,
  };
}
