import {
  AppMeta,
  DEFAULT_IDENTITY,
  ModelConfig,
  OpsOverview,
  RuntimeStateSnapshot,
  SkillSummary,
  ThreadSummary,
} from "../types/runtime";

const API_BASE = "/api/v1";

function identityHeaders(): Record<string, string> {
  return {
    "X-Tenant-Id": DEFAULT_IDENTITY.tenantId,
    "X-User-Id": DEFAULT_IDENTITY.userId,
    "X-User-Roles": DEFAULT_IDENTITY.roles.join(","),
  };
}

function defaultHeaders(): Record<string, string> {
  return {
    "Content-Type": "application/json",
    ...identityHeaders(),
  };
}

export async function fetchModelConfig(): Promise<ModelConfig> {
  const res = await fetch(`${API_BASE}/config/model`);
  if (!res.ok) throw new Error("无法获取模型配置");
  return res.json();
}

export async function fetchAppMeta(): Promise<AppMeta> {
  const res = await fetch(`${API_BASE}/meta`, { headers: defaultHeaders() });
  if (!res.ok) throw new Error("无法获取应用元信息");
  return res.json();
}

export async function updateModelConfig(payload: {
  api_key?: string;
  base_url?: string;
  model?: string;
  mode?: "deepseek" | "mock";
}): Promise<ModelConfig> {
  const res = await fetch(`${API_BASE}/config/model`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || "更新模型配置失败");
  }
  return res.json();
}

export async function fetchThreads(): Promise<ThreadSummary[]> {
  const res = await fetch(`${API_BASE}/threads`, { headers: defaultHeaders() });
  if (!res.ok) return [];
  const data = await res.json();
  return data.threads || [];
}

export async function deleteThread(threadId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/threads/${encodeURIComponent(threadId)}`, {
    method: "DELETE",
    headers: defaultHeaders(),
  });
  if (!res.ok && res.status !== 404) {
    const err = await res.text();
    throw new Error(err || "删除对话失败");
  }
}

export async function fetchThreadMessages(
  threadId: string
): Promise<Array<Record<string, unknown>>> {
  const res = await fetch(`${API_BASE}/threads/${encodeURIComponent(threadId)}/messages`, {
    headers: defaultHeaders(),
  });
  if (!res.ok) return [];
  const data = await res.json();
  return data.messages || [];
}

export async function fetchRuntimeState(
  threadId: string,
  traceId?: string | null
): Promise<RuntimeStateSnapshot> {
  const qs = new URLSearchParams({ thread_id: threadId });
  if (traceId) qs.set("trace_id", traceId);
  const res = await fetch(`${API_BASE}/state?${qs.toString()}`, {
    headers: defaultHeaders(),
  });
  if (!res.ok) throw new Error("拉取运行状态失败");
  return res.json();
}

export async function fetchOpsOverview(): Promise<OpsOverview> {
  const res = await fetch(`${API_BASE}/ops/overview`, { headers: defaultHeaders() });
  if (!res.ok) throw new Error("拉取管理后台数据失败");
  return res.json();
}

export async function fetchSkillContent(
  slug: string,
  resource = "",
  includeDisabled = true
): Promise<{ skill: SkillSummary; resource: string; content: string }> {
  const qs = new URLSearchParams();
  if (resource) qs.set("resource", resource);
  if (includeDisabled) qs.set("include_disabled", "true");
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  const res = await fetch(`${API_BASE}/skills/${encodeURIComponent(slug)}${suffix}`, {
    headers: defaultHeaders(),
  });
  if (!res.ok) throw new Error("读取 Skill 正文失败");
  return res.json();
}

export async function updateSkillStatus(
  slug: string,
  status: "active" | "disabled"
): Promise<{ ok: boolean; slug: string; status: "active" | "disabled" }> {
  const res = await fetch(`${API_BASE}/skills/${encodeURIComponent(slug)}/status`, {
    method: "PATCH",
    headers: defaultHeaders(),
    body: JSON.stringify({ status }),
  });
  if (!res.ok) {
    const err = await res.text();
    throw new Error(err || "更新 Skill 状态失败");
  }
  return res.json();
}

export async function uploadSkill(payload: {
  slug: string;
  description: string;
  content?: string;
  file?: File | null;
}): Promise<{ ok: boolean; skill: SkillSummary }> {
  let res: Response;
  if (payload.file) {
    const form = new FormData();
    form.append("file", payload.file);
    form.append("slug", payload.slug);
    form.append("description", payload.description);
    res = await fetch(`${API_BASE}/skills/upload`, {
      method: "POST",
      headers: identityHeaders(),
      body: form,
    });
  } else {
    res = await fetch(`${API_BASE}/skills/upload`, {
      method: "POST",
      headers: defaultHeaders(),
      body: JSON.stringify({
        slug: payload.slug,
        description: payload.description,
        content: payload.content || "",
      }),
    });
  }
  if (!res.ok) {
    const err = await res.text();
    throw new Error(err || "发布 Skill 失败");
  }
  return res.json();
}

export async function abortChatRun(payload: {
  runId?: string | null;
  threadId?: string | null;
}): Promise<{ ok: boolean; run: Record<string, unknown> }> {
  const res = await fetch(`${API_BASE}/chat/abort`, {
    method: "POST",
    headers: defaultHeaders(),
    body: JSON.stringify({
      run_id: payload.runId || undefined,
      thread_id: payload.threadId || undefined,
    }),
  });
  if (!res.ok) {
    const err = await res.text();
    throw new Error(err || "中止请求失败");
  }
  return res.json();
}

export async function resumeApproval(payload: {
  reviewId: string;
  threadId: string;
  approved: boolean;
  approverId?: string;
  reason?: string;
}): Promise<{ status: string; final_answer: string; trace_id?: string }> {
  const res = await fetch(`${API_BASE}/approvals/${encodeURIComponent(payload.reviewId)}/resume`, {
    method: "POST",
    headers: defaultHeaders(),
    body: JSON.stringify({
      thread_id: payload.threadId,
      approved: payload.approved,
      approver_id: payload.approverId || "mgr_bob",
      reason: payload.reason || (payload.approved ? "人工审核通过" : "人工审核拒绝"),
    }),
  });
  if (!res.ok) {
    const err = await res.text();
    throw new Error(err || "审批恢复失败");
  }
  return res.json();
}

export async function uploadKnowledgeDoc(payload: {
  title: string;
  section?: string;
  file: File;
}): Promise<{ ok: boolean; oss_key: string; chunks_indexed: number }> {
  const form = new FormData();
  form.append("file", payload.file);
  form.append("title", payload.title);
  form.append("section", payload.section || "正文");
  const res = await fetch(`${API_BASE}/knowledge/upload`, {
    method: "POST",
    headers: identityHeaders(),
    body: form,
  });
  if (!res.ok) {
    const err = await res.text();
    throw new Error(err || "上传知识文档失败");
  }
  return res.json();
}

export async function updateKnowledgeStatus(
  parentId: string,
  status: "active" | "disabled"
): Promise<{ ok: boolean; parent_id: string; status: "active" | "disabled" }> {
  const res = await fetch(`${API_BASE}/knowledge/${encodeURIComponent(parentId)}/status`, {
    method: "PATCH",
    headers: defaultHeaders(),
    body: JSON.stringify({ status }),
  });
  if (!res.ok) {
    const err = await res.text();
    throw new Error(err || "更新知识文档状态失败");
  }
  return res.json();
}

export async function streamChatSSE(params: {
  threadId: string;
  message: string;
  onEvent: (event: string, data: Record<string, unknown>) => void;
}): Promise<void> {
  const res = await fetch(`${API_BASE}/chat/sse`, {
    method: "POST",
    headers: defaultHeaders(),
    body: JSON.stringify({
      thread_id: params.threadId,
      message: params.message,
    }),
  });
  if (!res.ok || !res.body) {
    const detail = await res.text().catch(() => "");
    throw new Error(detail || `对话请求失败 (${res.status})`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() || "";

    for (const chunk of chunks) {
      let eventName = "message";
      let dataStr = "";
      for (const line of chunk.split("\n")) {
        if (line.startsWith("event:")) eventName = line.slice(6).trim();
        if (line.startsWith("data:")) dataStr += line.slice(5).trim();
      }
      if (!dataStr) continue;
      try {
        const parsed = JSON.parse(dataStr);
        // Unwrap standard v1 SSE envelope: { schema_version, run_id, seq, type, timestamp, payload }
        if (parsed && typeof parsed === "object" && parsed.payload && typeof parsed.payload === "object") {
          params.onEvent(parsed.type || eventName, {
            ...parsed.payload,
            run_id: parsed.run_id || parsed.payload.run_id,
            seq: parsed.seq,
          });
        } else {
          params.onEvent(eventName, parsed);
        }
      } catch {
        params.onEvent(eventName, { raw: dataStr });
      }
    }
  }
}
