import { ActivityBlock, ApprovalTicket, ChatTurnItem } from "../../shared/types/runtime";

function shortId(prefix: string) {
  return `${prefix}_${Math.random().toString(16).slice(2, 10)}`;
}

/**
 * Pure reducer applying a single SSE v1 event payload to the assistant turn item.
 */
export function applyStreamEventToItem(
  items: ChatTurnItem[],
  asstId: string,
  event: string,
  data: Record<string, unknown>
): ChatTurnItem[] {
  if (event === "meta") {
    const traceId = typeof data.trace_id === "string" ? data.trace_id : undefined;
    if (!traceId) return items;
    return items.map((it) => (it.id === asstId ? { ...it, traceId } : it));
  }

  if (event === "subagent_status") {
    const role = String(data.role || "subagent");
    const phase = data.phase === "done" ? "done" : "started";
    return items.map((it) => {
      if (it.id !== asstId) return it;
      const acts = [...(it.activities || [])];
      const idx = acts.findIndex((a) => a.kind === "subagent" && a.name === role);
      const nextBlock: ActivityBlock = {
        id: `sub_${role}`,
        kind: "subagent",
        name: role,
        phase,
        ok: true,
        detail:
          phase === "started"
            ? String(data.goal || "独立窗口调研中")
            : `完成 (${data.chars ?? 0} 字)`,
      };
      if (idx >= 0) acts[idx] = { ...acts[idx], ...nextBlock };
      else acts.push(nextBlock);
      return { ...it, activities: acts };
    });
  }

  if (event === "tool_status") {
    const name = String(data.tool || "tool");
    const phase = data.phase === "started" ? "started" : data.ok === false ? "error" : "done";
    return items.map((it) => {
      if (it.id !== asstId) return it;
      const acts = [...(it.activities || [])];
      const idx = acts.findIndex((a) => a.kind === "tool" && a.name === name && a.phase === "started");
      const block: ActivityBlock = {
        id: shortId("act"),
        kind: "tool",
        name,
        phase,
        args: (data.args as Record<string, unknown>) || undefined,
        ok: typeof data.ok === "boolean" ? data.ok : undefined,
        truncated: Boolean(data.truncated),
        refId: data.ref_id ? String(data.ref_id) : undefined,
        ossKey: data.oss_key ? String(data.oss_key) : undefined,
        preview: data.preview ? String(data.preview) : undefined,
      };
      if (idx >= 0 && phase !== "started") {
        acts[idx] = { ...acts[idx], ...block, id: acts[idx].id };
      } else {
        acts.push(block);
      }
      const paddedContent =
        phase === "started" && it.content && !it.content.endsWith("\n\n")
          ? it.content.trimEnd() + "\n\n"
          : it.content;
      return { ...it, content: paddedContent, activities: acts };
    });
  }

  if (event === "token") {
    const chunk = String(data.text || "");
    return items.map((it) => (it.id === asstId ? { ...it, content: it.content + chunk } : it));
  }

  if (event === "approval_required") {
    const interrupts = (data.interrupts as ApprovalTicket[]) || [];
    if (!interrupts.length) return items;
    return items.map((it) =>
      it.id === asstId ? { ...it, approval: interrupts[0], approvalStatus: "pending" } : it
    );
  }

  if (event === "cancelled") {
    return items.map((it) =>
      it.id === asstId
        ? { ...it, content: it.content ? `${it.content}\n\n*（已中止生成）*` : "已中止生成。" }
        : it
    );
  }

  if (event === "error") {
    return items.map((it) =>
      it.id === asstId ? { ...it, content: `请求出错：${String(data.error || "unknown")}` } : it
    );
  }

  return items;
}
