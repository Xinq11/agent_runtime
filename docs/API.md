# Agent Runtime HTTP & SSE 接口契约文档（v1）

> 本文档面向**前端工程师与二次开发团队**：本框架的 React 前端仅为官方参考实现，任意自定义前端（React / Vue / 门户 / 小程序 / CLI）均只需对接本文档的 **HTTP + SSE v1 协议**，无需阅读后端代码。
>
> 在线交互式 OpenAPI / Swagger 文档：`http://<host>:8000/docs`（JSON Schema：`http://<host>:8000/openapi.json`）。

---

## 1. 基础约定与身份认证

- **标准前缀**：`/api/v1`（同时保留 `/api` 兼容别名；Prometheus 抓取端点位于 `/metrics`）。
- **认证与归属隔离 Header**（每个客户独立部署，内核仅需可信 `user_id`；遗留 `X-Tenant-Id` 头将被静默忽略；切勿在未受保护公网直接信任客户端自填头）：
  | Header | 说明 | 示例 |
  |---|---|---|
  | `X-User-Id` | 操作人用户 ID（存在此 Header 时按网关头解析身份，否则回退默认身份；所有会话、`run_id`、`trace_id`、`ref_id` 均按此归属鉴权） | `u_alice` |
  | `X-User-Roles` | 逗号分隔角色列表（配合可拔插 `AccessController` 进行工具/审批鉴权，并透传给客户 `RagProvider` / `SkillProvider`） | `employee,exec` |
  | `X-Gateway-Signature` | 可选；开启 `AGENT_IDENTITY_RESOLVER=signed_header`（配置 `AGENT_GATEWAY_HMAC_SECRET`）时必传，对 `["v1", user_id, roles]` 验签，缺失或失败返 `401` | `...` |
- **标准错误码**：
  - `401`：身份凭证缺失、过期或签名校验失败；
  - `403`：权限不足（如非平台运维修改模型配置、禁止自批或审批角色不符）；
  - `404`：会话 / Run / Trace / 审批单 / 工件不存在或非创建者越权访问；
  - `409`：同一会话已有运行中/待审批 Run、或审批单状态冲突/已取消；
  - `413`：会话附件超过 10 MiB 体积上限；
  - `422`：请求体校验失败（如空消息、不支持的附件类型或非法路径）；
  - `503`：外部/自定义权限控制器异常（fail-closed 拒绝服务）。

---

## 2. 系统与能力发现接口

### 2.1 健康检查
- `GET /api/v1/health`
- 响应示例：
```json
{
  "ok": true,
  "status": "ok",
  "sdk_version": "1.0.0",
  "protocol_version": "1.0",
  "postgres": "ok",
  "minio": "ok",
  "mode": "deepseek",
  "base_url": "https://api.deepseek.com/v1",
  "model": "deepseek-chat",
  "has_api_key": true
}
```

### 2.2 框架与插件元信息（供自定义前端动态适配）
- `GET /api/v1/meta`
- 返回协议版本、已启用客户插件、SSE 事件类型、工具、当前用户可见的 Skills（仅当挂载了 `SkillProvider` 时非空）及工作台文案。以下响应示例来自启用了费用插件的部署。
```json
{
  "sdk_version": "1.0.0",
  "protocol_version": "1.0",
  "extensions": ["enterprise_expense"],
  "sse_events": ["meta", "token", "tool_status", "subagent_status", "approval_required", "done", "cancelled", "error"],
  "app_name": "Agent Runtime",
  "subtitle": "Workspace",
  "plugin_name": "enterprise_expense",
  "welcome_title": "...",
  "welcome_description": "...",
  "quick_prompts": [{"title": "...", "hint": "...", "prompt": "..."}],
  "tools": [{"name": "read", "description": "...", "risk_level": "read", "is_subagent": false}],
  "skills": [{"slug": "expense-audit-report", "version": 1, "description": "...", "tags": ["finance"], "resources": []}]
}
```

### 2.3 模型配置查询与热更新
- `GET /api/v1/config/model`
- `POST /api/v1/config/model`（当前仅对本后端进程生效，且端点受白名单约束；生产部署应限制为平台运维权限）
  - 请求体：`{ "api_key"?: string, "base_url"?: string, "model"?: string, "mode"?: "deepseek" | "openai" | "qwen" | "compatible" }`（自建 OpenAI 兼容网关可通过 `AGENT_TRUSTED_LLM_HOSTS` 放行域名）

---

## 3. 对话、流式 SSE、中止（Abort）与审批恢复

### 3.1 发起流式对话（SSE）
- `POST /api/v1/chat/sse`
- 请求体：
```json
{
  "thread_id": "th_optional_client_id",
  "message": "查华东区 Q3 差旅超标数据并画一张柱状图",
  "request_id": "req_optional_correlation_id"
}
```
- 响应：`Content-Type: text/event-stream`

#### 标准 SSE v1 事件信封规范
每一帧 SSE 均包含单调递增的帧序号 `id`（`seq`）与统一 JSON 信封：
```text
id: 1
event: meta
data: {"schema_version":"1.0","run_id":"run_xxx","seq":1,"type":"meta","timestamp":1764140000000,"payload":{"run_id":"run_xxx","thread_id":"th_xxx","trace_id":"tr_xxx","mode":"deepseek","model":"deepseek-chat"}}
```

| `type`（事件名） | 触发时机 | `payload` 核心字段 | 是否终态 |
|---|---|---|---|
| `meta` | Run 登记成功后首帧 | `run_id`, `thread_id`, `trace_id`, `mode`, `model` | 否 |
| `token` | LLM 流式增量文本 | `text` | 否 |
| `tool_status` | 工具开始/结束/暂停 | `tool`, `tool_call_id`, `phase` (`started`/`done`/`paused`), `status`, `args`, `ok`, `truncated`, `ref_id`, `oss_key`, `preview` | 否 |
| `subagent_status` | 只读子 Agent 启动/完成 | `role`, `phase` (`started`/`done`), `goal` / `chars` | 否 |
| `approval_required` | 命中 `HIGH_RISK` 写工具暂停 | `run_id`, `thread_id`, `trace_id`, `interrupts`（审批票据数组，含 `review_id`, `tool_name`, `summary`） | **暂停态**（流关闭，等人工恢复或 Abort） |
| `done` | 正常完成 | `run_id`, `thread_id`, `trace_id`, `answer` | **终态** |
| `cancelled` | 收到 Abort 中止并收敛 | `run_id`, `thread_id`, `trace_id` | **终态** |
| `error` | 执行异常 | `error`, `trace_id`, `run_id` | **终态** |

### 3.2 主动中止正在执行或待审批的 Run（Abort）
- `POST /api/v1/chat/abort` → `202 Accepted`
- 请求体（`run_id` 与 `thread_id` 二选一）：
```json
{ "run_id": "run_xxx" }
```
- 响应：`{ "ok": true, "run": { "run_id": "...", "status": "cancelled", ... } }`
- 说明：中止活跃运行后，底层 `agent_runs` 不可逆进入 `cancelled`，旧审批票据无法再恢复该 Run。按 `run_id` 重复调用已完成/已失败/已取消的 Run 时返回其原终态；按 `thread_id` 查询不到活跃 Run 时返回 `404`。

### 3.3 查询单次 Run 状态与 SSE 断点续接补帧
- `GET /api/v1/runs/{run_id}` → 返回 `RunRecord`（`status`: `running | paused | completed | cancelled | failed`）。
- `GET /api/v1/runs/{run_id}/events?after_seq={n}`（同时支持标准 `Last-Event-ID` 请求头）→ 校验创建者归属后，从内存环形缓冲中重放 `seq > cursor` 的全部遗漏帧；若 Run 仍在执行则无缝续接实时推流直至终态，缓冲已过期时返回 `410`。

### 3.4 高危审批同意/拒绝并恢复线程
- `POST /api/v1/approvals/{review_id}/resume`
- 请求体：
```json
{
  "approved": true,
  "thread_id": "th_xxx",
  "approver_id": "mgr_bob",
  "reason": "同意专项审批"
}
```
- 响应：`{ "ok": true, "run_id": "...", "thread_id": "...", "trace_id": "...", "final_answer": "...", "status": "completed" | "interrupted", "interrupts": [] }`
- 说明：
  - 若恢复执行后再次命中新的高危工具，接口将返回 `status: "interrupted"` 及下一批待确认的 `interrupts` 数组，客户端需继续按新的 `review_id` 依次调用本接口或调用 Abort 结束运行。
  - `approver_id` 在 HTTP 接口中始终由服务端已解析的调用方主体（`scope.user_id`）生成，请求体中传入的 `approver_id` 仅保留字段兼容而不作为审计依据；开启 `RbacAccessController` 时按 `approval:decide` 校验审批人角色及是否允许自批。

### 3.5 会话管理、历史消息与 Trace 状态快照
- `GET /api/v1/threads` → 列出当前用户所有非空会话（按创建时间倒序，不含空草稿）。
- `POST /api/v1/threads` → 显式创建会话。
- `GET /api/v1/threads/{thread_id}/messages` → 返回该会话完整 `session_messages`（附带当前用户已保存的 `feedback` 与 `feedback_by_trace`）。
- `GET /api/v1/threads/{thread_id}/feedback` → 查询当前用户在该会话中的助手回复点赞/点踩记录。
- `POST /api/v1/threads/{thread_id}/feedback` → 保存或更新点赞/点踩记录（入 PG `message_feedback` 表，按 `(thread_id, actor_id, trace_id)` 互斥幂等更新），请求体：`{ "trace_id": string, "rating": "up" | "down", "reason"?: string, "msg_id"?: number, "answer_excerpt"?: string }`。
- `DELETE /api/v1/threads/{thread_id}/feedback/{trace_id}` → 取消指定回复的点赞/点踩记录。
- `GET /api/v1/threads/{thread_id}/files` → 列出当前会话工作区文件（含用户上传附件与 Agent `write` 生成的报告，带版本与大小）。
- `POST /api/v1/threads/{thread_id}/files` → `multipart/form-data` 上传附件到当前会话 `uploads/<文件名>`（最大 10 MiB，支持文本/代码/表格类文件与 `.pdf` / `.docx` / `.xlsx`；原文件二进制存 MinIO 供原样下载，同步提取结构化纯文本存伴生对象供 Agent `read` 透明分页读取，内置 Magic Bytes / OOXML Zip 炸弹 / XXE DTD 防护）。
- `GET /api/v1/threads/{thread_id}/files/download?path={logical_path}` → 校验会话归属与路径安全后原样下载文件（强制 `Content-Disposition: attachment` + RFC 5987 UTF-8 文件名、`nosniff` 与 `CSP sandbox` 防 Stored XSS）。
- `DELETE /api/v1/threads/{thread_id}` → 若存在活跃 Run 先尝试中止，再级联删除该会话在 PG 中的全部记录（并同步清理工作区在 MinIO 中的实体与提取文本对象）。
- `GET /api/v1/state?thread_id={tid}&trace_id={tr}` → 严格校验创建者归属（支持按 `thread_id` 或单独按 `trace_id` 查询，非本人记录一律返 `404`），返回单会话 Trace 瀑布图 `spans`、压缩摘要游标 `summary`、`context_budget`、当前 `active_run`、`workspace_files`、待审批单、OSS 工件与 Loki 日志（不查询 RAG 表）。
- `GET /api/v1/outputs/{ref_id}?offset=0&limit=4000` → 校验创建者归属与 `artifact:read` 后，分页读取 MinIO OSS 落盘的大工具输出工件。

---

## 4. 观测指标与自定义业务路由

- `GET /metrics` → 标准 Prometheus Exposition Format 指标端点（平台观测由 Prometheus / Loki / Grafana 提供，SDK 底座不内置管理后台）。
- **知识库 / Skills / 业务管理接口**：SDK 底座只提供执行循环、会话与 `SkillProvider` / `RagProvider` 接入协议，不内置任何知识库上传或 Skill 管理 HTTP 路由；客户如需挂载自有业务接口，可通过 `AgentPlugin.routers()` 声明自定义 FastAPI `APIRouter`，或由独立业务服务直接维护。

---

## 5. 最小前端接入示例（TypeScript / Fetch）

> 以下示例中的 `X-*` 身份头仅用于本地演示或直连内部可信网关场景；企业门户接入时应通过登录态 Token / Cookie 由前置网关鉴权后向后端注入可信主体。

```ts
const res = await fetch("/api/v1/chat/sse", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    // 演示环境示例头；生产门户应改由网关验签注入
    "X-User-Id": "u_alice",
    "X-User-Roles": "employee,exec",
  },
  body: JSON.stringify({ thread_id: "th_001", message: "你好" }),
});

const reader = res.body!.getReader();
const decoder = new TextDecoder();
let buf = "";
let currentRunId = "";

while (true) {
  const { done, value } = await reader.read();
  if (done) break;
  buf += decoder.decode(value, { stream: true });
  const frames = buf.split("\n\n");
  buf = frames.pop() ?? "";
  for (const frame of frames) {
    const dataLine = frame.split("\n").find((l) => l.startsWith("data:"));
    if (!dataLine) continue;
    const env = JSON.parse(dataLine.slice(5).trim()); // { schema_version, run_id, seq, type, timestamp, payload }
    if (env.type === "meta") currentRunId = env.run_id;
    if (env.type === "token") console.log(env.payload.text);
  }
}

// 随时中止：
// await fetch("/api/v1/chat/abort", { method: "POST", headers: {...}, body: JSON.stringify({ run_id: currentRunId }) });
```
