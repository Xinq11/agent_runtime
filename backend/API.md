# Agent Runtime HTTP & SSE 接口契约文档（v1）

> 本文档面向**前端工程师与二次开发团队**：本框架的 React 前端仅为官方参考实现，任意自定义前端（React / Vue / 门户 / 小程序 / CLI）均只需对接本文档的 **HTTP + SSE v1 协议**，无需阅读后端代码。
>
> 在线交互式 OpenAPI / Swagger 文档：`http://<host>:8000/docs`（JSON Schema：`http://<host>:8000/openapi.json`）。

---

## 1. 基础约定与身份认证

- **标准前缀**：`/api/v1`（同时保留 `/api` 兼容别名；Prometheus 抓取端点位于 `/metrics`）。
- **认证与租户隔离 Header**（生产环境由 API 网关校验 JWT 后透传，切勿在请求体中伪造）：
  | Header | 说明 | 示例 |
  |---|---|---|
  | `X-Tenant-Id` | 租户 ID | `t_acme` |
  | `X-User-Id` | 操作人用户 ID | `u_alice` |
  | `X-User-Roles` | 逗号分隔角色列表（用于 RAG / Skill ACL 前置过滤） | `employee,exec` |
- **标准错误码**：
  - `403`：权限变更或无权访问；
  - `404`：会话 / Run / 审批单 / 工件 / Skill 不存在或跨租户越权；
  - `409`：同一会话已有运行中/待审批 Run、或审批单状态冲突/已取消；
  - `413`：文档或 Skill 超过 2 MiB 体积上限；
  - `422`：请求体校验失败（如空消息、非法 slug、Skill 上传可执行脚本）。

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
- 返回当前协议版本、已启用扩展列表、支持的 SSE 事件类型、工具清单、当前用户可见 Skills 及客户自定义标题/欢迎语/快捷提示：
```json
{
  "sdk_version": "1.0.0",
  "protocol_version": "1.0",
  "extensions": ["rag", "skills", "enterprise_expense"],
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
- `POST /api/v1/config/model`
  - 请求体：`{ "api_key"?: string, "base_url"?: string, "model"?: string, "mode"?: "auto" | "deepseek" | "mock" }`

---

## 3. 对话、流式 SSE、中止（Abort）与审批恢复

### 3.1 发起流式对话（SSE）
- `POST /api/v1/chat/sse`
- 请求体：
```json
{
  "thread_id": "th_optional_client_id",
  "message": "查华东区 Q3 差旅超标数据并画一张柱状图",
  "request_id": "req_optional_idempotency_key"
}
```
- 响应：`Content-Type: text/event-stream`

#### 标准 SSE v1 事件信封规范
每一帧 SSE 均包含单调递增 `id` 与统一 JSON 信封：
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
- `POST /api/v1/chat/abort` → `202 Accepted`（幂等，重复调用安全）
- 请求体（`run_id` 与 `thread_id` 二选一）：
```json
{ "run_id": "run_xxx" }
```
- 响应：`{ "ok": true, "run": { "run_id": "...", "status": "cancelled", ... } }`
- 说明：中止后底层 `agent_runs` 状态不可逆进入 `cancelled`，旧审批票据无法再恢复该 Run。

### 3.3 查询单次 Run 状态
- `GET /api/v1/runs/{run_id}` → 返回 `RunRecord`（`status`: `running | paused | completed | cancelled | failed`）。

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

### 3.5 会话列表、历史消息与 Trace 状态快照
- `GET /api/v1/threads` → 列出当前用户所有非空会话（按创建时间倒序，不含空草稿）。
- `POST /api/v1/threads` → 显式创建会话。
- `GET /api/v1/threads/{thread_id}/messages` → 返回该会话完整 `session_messages`。
- `GET /api/v1/state?thread_id={tid}&trace_id={tr}` → 返回单会话 Trace 瀑布图 `spans`、压缩摘要游标 `summary`、当前 `active_run`、`workspace_files`、待审批单、OSS 工件与 Loki 日志。

---

## 4. Skills 技能库管理接口（PG + OSS 版本化）

- `GET /api/v1/skills?query={q}` → 列出当前租户与角色可见的全部活跃 Skill 元信息。
- `GET /api/v1/skills/{slug}?resource={path}` → 从 OSS 读取 `SKILL.md` 正文或指定声明式文本附件。
- `POST /api/v1/skills/upload` → 热发布新版本 Skill（先写不可变 OSS 版本对象，再在 PG 原子切指针；**禁止上传 `.py`/`.sh`/`.exe` 等可执行脚本**）：
```json
{
  "slug": "weekly-report-sop",
  "description": "研发团队周报汇总与风险提炼标准流程",
  "content": "# 周报汇总 SOP\n1. ...",
  "acl_roles": ["employee"],
  "tags": ["ops", "report"],
  "resources": {
    "references/template.md": "## 本周完成\n- ..."
  }
}
```
- `DELETE /api/v1/skills/{slug}` → 下线/禁用指定 Skill。

---

## 5. 知识库与全局运维观测接口

- `POST /api/v1/knowledge/upload` → 支持 `application/json`（`{title, content, section, acl_roles}`）或 `multipart/form-data` 文件上传，原文落 MinIO OSS，切块向量化后入 PG `pgvector`。
- `GET /api/v1/ops/overview` → 全局监控总览（健康状态、各实体计数、会话与摘要、OSS 工件、知识切片、Skills、最近 Trace、Prometheus `/metrics` 快照、Loki 最近 80 条结构化日志）。
- `GET /api/v1/ops/outputs/{ref_id}?offset=0&limit=1200` → 分页读取 MinIO OSS 落盘的大工具输出。
- `GET /metrics` → 标准 Prometheus Exposition Format 指标端点。

---

## 6. 最小前端接入示例（TypeScript / Fetch）

```ts
const res = await fetch("/api/v1/chat/sse", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "X-Tenant-Id": "t_acme",
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
  for (const frame of buf.split("\n\n").slice(0, -1)) {
    const dataLine = frame.split("\n").find((l) => l.startsWith("data:"));
    if (!dataLine) continue;
    const env = JSON.parse(dataLine.slice(5).trim()); // { schema_version, run_id, seq, type, timestamp, payload }
    if (env.type === "meta") currentRunId = env.run_id;
    if (env.type === "token") console.log(env.payload.text);
  }
  buf = buf.slice(buf.lastIndexOf("\n\n") + 2);
}

// 随时中止：
// await fetch("/api/v1/chat/abort", { method: "POST", headers: {...}, body: JSON.stringify({ run_id: currentRunId }) });
```
