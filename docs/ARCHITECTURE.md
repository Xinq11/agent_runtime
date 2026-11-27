# Agent Runtime · 后端架构

客户代码决定 Agent 能办什么事。共享 SDK 负责一轮对话怎样运行、调用工具后怎样继续，以及中途暂停后从哪里恢复。Python 应用可以直接装配 SDK，参考 HTTP 服务则把同一条执行路径开放给 Studio 和客户门户。

[项目首页](../README.md) · [HTTP / SSE v1 契约](API.md) · [后端源码](../backend/agent_runtime/)

---

## 1. 总图、分层边界与装配生命周期

![SDK 和 HTTP 入口共用 AgentRuntimeApp，客户扩展接入执行循环与工具网关](assets/architecture-overview.png)

### 四层职责与依赖约定

| 层 | 负责什么 | 从哪里读代码 |
| --- | --- | --- |
| 接口 (`api/`) | 接收 HTTP 请求，解析身份，启动/恢复运行，通过 SSE v1 推流；可选参考运维路由 `ops.py` 仅在 `AGENT_ENABLE_OPS=1` 挂载 | [api/main.py](../backend/agent_runtime/api/main.py) · [chat_stream.py](../backend/agent_runtime/api/chat_stream.py) |
| 内核 (`framework/`) | 装配模型与扩展，构建执行图，管理上下文预算、Run 状态机、安全契约与工具网关（零 RAG/Skills/pgvector 依赖） | [agent/app.py](../backend/agent_runtime/framework/agent/app.py) · [core/workflow.py](../backend/agent_runtime/framework/core/workflow.py) |
| 扩展 (`extensions/`) | 内置标准可选扩展（RAG 7 阶段检索、版本化 Skills），各自独立拥有 `schema.sql`、仓储与管理路由 | [RAG](../backend/agent_runtime/extensions/rag/) · [Skills](../backend/agent_runtime/extensions/skills/) |
| 基础设施 (`infra/`) | 适配核心 PostgreSQL、MinIO 对象存储、OpenAI 兼容 LLM 服务与 PLG 观测 | [PG](../backend/agent_runtime/infra/pg/) · [OSS](../backend/agent_runtime/infra/oss/) · [LLM](../backend/agent_runtime/infra/llm/) · [PLG](../backend/agent_runtime/infra/plg/) |

**依赖方向与客户隔离原则：**
- 依赖方向严格单向 `api -> extensions -> framework <- infra`（由 AST 验收测试守护）：内核 `framework/`、`infra/` 与核心 `api/` 严禁反向引用任何 `extensions.*`，纯 SDK 安装启动不要求 `pgvector`，也不创建 RAG/Skills 表。
- [费用示例](../examples/enterprise_expense/) 作为独立 Python 包依赖 SDK。插件通过 `register_tools()` 声明业务工具、通过 `subagents()` 声明 `SubAgentProfile`（由 SDK 自动装配模型与网关），并在 `bootstrap_store()` 创建 OA 表。
- 扩展按需提供 `routers()` 与 `bootstrap_store()`；核心 `deploy/sql/` 只包含执行底座表（并内置幂等迁移 `006_drop_tenant_id.sql`，带冲突预检，安全保留历史审批与 OSS 路径）。

### 启动装配 vs 每次执行

1. **启动装配（一次）**：`AgentRuntimeApp` 构造 → 注册不可变内置工具（`read` / `write` / `read_tool_output`）→ 加载自定义工具、`subagents` 与 `extensions`（自动装配 `ext.register_tools()`、`ext.subagents()`、`ext.middlewares()`；HTTP 层按需挂载 `ext.routers()`）→ **冻结 `ToolRegistry`**（运行期禁止篡改工具表）→ 执行幂等 `bootstrap_extensions()`。
2. **每次执行（按请求）**：SDK 与 HTTP 共用 `prepare_turn_run` / `execute_run_state` / `abort_run` / `resume_approval` → 校验创建者会话归属 → 登记持久化 `agent_runs`（每次 Run 独立占用以 `run_id` 为键的 LangGraph 检查点，取消待审批 Run 后原会话立即可继续新对话）→ 流式推送 SSE v1 事件并按步落库。

> **同进程运行边界**：客户 Python 插件与后端运行在同一进程内，应按业务代码同等级评审、打包与发布，更新后重启服务生效。

---

## 2. 一轮请求：模型决定下一步，Runtime 负责执行

以费用示例“核查差旅超标并发起审批”为例。Python SDK（`app.run_turn` / `app.resume_approval`）与 HTTP 服务共用以下单一 LangGraph `StateGraph` 执行图：

![模型请求工具后接收结果继续推理，高危调用等待审批后恢复](assets/turn-execution.png)

1. **组装上下文（`before_model`）**：读出历史摘要与摘要游标之后的消息，拼接当前轮尚未持久化的临时消息。预算超限时以**原子工具组**（`assistant(tool_calls)` + 对应全部 `tool` 结果）为单位从旧到新连续裁剪；若最新一组自身超窗则截断工具正文而非丢弃配对，避免模型看到孤立的 `tool_call`。[上下文组装](../backend/agent_runtime/framework/context/memory.py)
2. **执行工具（`tools_node` → `ToolGateway`）**：按 `RiskLevel` 分流（只读工具受限并行，写/高危工具串行），依次执行权限收窄校验、JSON Schema 参数校验、循环熔断、高危审批拦截、幂等抢占与超时控制。返回过大时将原文落 MinIO，仅把预览与 `ref_id` 放回上下文，由 `read_tool_output` 分页续读。[工具网关](../backend/agent_runtime/framework/tools/gateway.py)
3. **按步记录进度**：每批工具完成后，图节点以 `(thread_id, trace_id, step_key)` 幂等写入本批消息并推进 `persisted_count`。最终答复在 `finish` 落库后即可返回。后台按 `H = 模型窗口 − 固定指令与工具定义 − 动态注入预留 − 提问缓冲 − 输出预留` 计算历史预算。旧摘要与未压缩消息超过 70% × H 时，按完整用户轮次切分，目标保留约 20% × H 的近期原文，仅推进摘要游标。模型调用前若估算超限，先复用在途摘要、移除动态注入、折叠旧工具预览，再按需同步压缩历史；始终保留当前提问，单次提问过长会提示分段。短生命周期的 Python 程序可在退出事件循环前 `await app.compactor.drain()` 等待回写。[执行图](../backend/agent_runtime/framework/core/workflow.py)
4. **跨轮审批恢复**：`HIGH_RISK` 工具通过 LangGraph `interrupt()` 暂停，前端收到 `approval_required`。提交决定后，服务校验检查点挂起票据与运行状态，记录决定并以 `Command(resume=review_id)` 恢复同一检查点继续执行。[审批入口](../backend/agent_runtime/api/approvals.py) · [运行状态](../backend/agent_runtime/framework/core/runs.py)

---

## 3. 状态归属、存储分工与标识串联

核心标识关联：`request_id`（客户端请求关联标识，并参与写动作 `op_key` 计算）→ `run_id`（一次运行生命周期，受同会话活跃唯一约束保护，同时作为 LangGraph 检查点隔离键）→ `trace_id`（单轮观测链路，校验所属会话创建者）→ `op_key`（基于会话、用户、请求、工具名与 `tool_call.id` 计算的写动作幂等键）→ `thread_id`（会话归属与消息历史主键）。

| 问题 | 去哪里查（存储层） | 关联键 / 前缀 |
| --- | --- | --- |
| 这次对话写入了哪些消息与摘要？ | PG：`agent_threads`、`session_messages`、`session_summaries` | `thread_id` |
| 当前在执行、等审批还是已取消？ | PG 业务状态：`agent_runs`、`tool_approvals`；LangGraph 断点：`checkpoints*` | `run_id` / `review_id` / `thread_id` |
| 同一个写动作是否执行过？ | PG：`tool_runs`（记录 `running`/`done`/`reconcile`、回执与结果引用） | `op_key` |
| 原始大输出、工作区（及可选扩展）原文在哪？ | 核心 PG 存 `tool_outputs`、`workspace_files`；可选扩展自持 `rag_*`、`agent_skills*`；MinIO 存不可变原文 | `outputs/`、`workspaces/`、`knowledge/`、`skills/` |
| 哪一步拖慢了响应？ | Prometheus（首 token / 工具状态）、Loki（结构化日志）、PG `agent_spans`（步骤耗时）、Grafana 看板 | `trace_id` |

**检查点与连接生命周期**：
- FastAPI `lifespan` 启动时打开 `psycopg_pool.ConnectionPool`（`PgOssStore`）、初始化 `AsyncPostgresSaver`、装配 `OpenAICompatibleProvider` 与后台日志推送协程，关闭时优雅刷盘并释放连接池。[启动与关闭](../backend/agent_runtime/api/main.py) · [PG 仓储](../backend/agent_runtime/infra/pg/postgres.py)
- 直接嵌入 Python SDK 时，默认检查点为进程内 `InMemorySaver`；需要跨进程或跨重启恢复审批时，应显式传入持久化 checkpointer（如 `AsyncPostgresSaver`）。

---

## 4. 断连、取消、重放与故障窗口

| 情况 | 当前后端行为 | 客户开发 / 运维需要知道什么 |
| --- | --- | --- |
| 浏览器关闭或 SSE 消费过慢 | `publish_sse` 有限等待（0.5s），超时或断连后摘除推送队列，不再阻塞后台协程 | 后台运行可继续收敛并落库，不会锁死会话 409；回到页面查历史与状态，需主动停止时调用 Abort |
| 用户在一批工具执行中途取消 | `tools_node` 捕获 `CancelledError`（不吞 LangGraph 中断异常），同步刷盘本批已完成结果并为未完成调用补 `[cancelled]`；`agent_runs` 不可逆置为 `cancelled`，拒绝迟到的审批恢复 | 已被下游业务系统受理的副作用仍需按业务规则补偿或人工核对 |
| 审批恢复触发节点重放 | 网关用 `op_key` 抢占；已完成写动作直接复用已存回执，避免重复扣款或重复提单 | 对接真实 OA / 工单时，务必透传 `ctx.op_key` 给下游做幂等防重 |
| 下游已受理但超时/中途取消（状态不确定） | 网关将 `tool_runs.status` 标记为 `reconcile` | 客户连接器需约定按 `op_key` 向外部系统查单对账的恢复手段 |
| 进程崩溃或首条消息写入前故障 | PG 保存业务运行态、LangGraph 保存节点级检查点，二者覆盖不同窗口；进程异常退出后活跃 `agent_runs` 目前需人工/超时回收 | 计划在后续版本补充 `start_run` 与首条消息的事务收敛及超时僵尸 Run 自动回收机制 |

---

## 5. 扩展契约：从费用示例换成客户项目

| 扩展协议 | 定位与适用场景 | 源码入口 |
| --- | --- | --- |
| `AgentPlugin` | 统一的能力包与客户业务打包入口（声明系统提示词、工具、子 Agent、受控中间件与前端元信息），支持 `extensions=[...]` 与 `AGENT_EXTENSIONS` 挂载 | [core/protocols.py](../backend/agent_runtime/framework/core/protocols.py) |
| `AgentMiddleware` | 受控生命周期切面（`extra_context` 纳入内核预算、`transform_user_input`、`on_before_model` 只读快照、`after_model`、`wrap_tool`、`after_turn`），不可破坏上下文裁剪与工具配对 | [core/middleware.py](../backend/agent_runtime/framework/core/middleware.py) |
| `LLMProvider` | 替换模型后端（默认 OpenAI 兼容流式 Provider，当前接入 DeepSeek，预留多 API 扩展） | [infra/llm/provider.py](../backend/agent_runtime/infra/llm/provider.py) |
| `RAGStrategy` | 替换整条检索管线（默认内置 7 阶段混合检索，可无缝换接 Milvus / ES / 客户知识服务） | [extensions/rag/extension.py](../backend/agent_runtime/extensions/rag/extension.py) |

| 新客户需求 | 推荐接入方式 |
| --- | --- |
| 接入新业务系统（ERP / 工单 / OA） | 用 `@tool` 封装客户接口并标注 `RiskLevel`，通过独立客户包注册，由网关统一处理校验、超时、审批与幂等 |
| 更换知识库检索 | 保留内置 7 阶段 RAG 并替换 `EmbeddingClient` / `RerankClient`，或传入自定义 `RagExtension(strategy=...)` |
| 交付业务 SOP | 通过 `SkillsExtension` 发布版本化声明式文档与文本附件（拒绝可执行脚本），Agent 按需检索读取 |
| 接入现有门户前端 | 对接 [HTTP / SSE v1 契约](API.md)，与参考 Studio 共用同一后端与运行状态 |

---

## 6. 安全边界与可拔插 ACL 架构（现状与演进）

> 核心安全契约见 [security/](../backend/agent_runtime/framework/core/security/)，扩展专用 SQL ACL 编译器已下沉至 [extensions/_acl_sql.py](../backend/agent_runtime/extensions/_acl_sql.py)。

### 6.1 可拔插双协议与单部署边界

业务路由、LangGraph 节点、工具网关及可选扩展统一调用两个最小协议，内核已移除 `tenant_id`，每个客户独立部署只需提供可信的 `user_id`：

| 协议 | 职责与最小签名 | 内置实现与环境变量 |
| --- | --- | --- |
| `IdentityResolver`（认证 AuthN） | `resolve(context) -> AuthScope` | `StaticIdentityResolver`（`default`）、`HeaderIdentityResolver`（`header` / `signed_header`，对 `["v1", user_id, roles]` 做 HMAC 验签，忽略遗留 `X-Tenant-Id`）或自定义 `module:Symbol`（`AGENT_IDENTITY_RESOLVER`） |
| `AccessController`（授权 AuthZ） | `check(scope, action, resource) -> AccessDecision`<br>`filter_for(scope, action, kind) -> AccessFilter`<br>`policy_rev(scope) -> int` | `RbacAccessController(admin_roles=None)`（`default`）、`NoopAccessController`（`noop`）、`RbacAccessController(admin_roles=..., platform_admin_roles=...)`（`rbac`，可配 `AGENT_APPROVER_ROLES` / `AGENT_FORBID_SELF_APPROVAL`）或自定义 `module:Symbol`（`AGENT_ACCESS_CONTROLLER`） |

**四项永不关闭的内核安全不变量**（即使在 `default` 或 `noop` 模式下也始终生效）：
1. **创建者硬归属隔离**：`thread_id`、`run_id`、`trace_id`、`ref_id` 与工作区文件统一在核心仓储和 `RunService` 按 `actor_id == scope.user_id` 强制拦截（不匹配一律 `404`）。
2. **审批默认仅限创建者，跨人审批需显式授权**：默认模式与 `noop` 模式下，其他用户即使拿到 `review_id` 也无法批准/恢复他人会话；仅当客户显式配置 `approver_roles`、显式 `admin_roles` 或自定义 `AccessController` 时才允许跨人审批。
3. **审批原子性与写幂等**：高危动作按 `review_id` 消费不可重放、写动作按 `op_key` 去重；SDK 与 HTTP 共用的 `resume_approval` 强制以可信主体 `caller.user_id` 署名。
4. **子 Agent 权限单调收窄与及时取消**：`AuthScope.allowed_tools` 区分 `None`（不限）与 `()`（零权限），父子工具求交为空时目录与执行双拒绝；子 Agent 每步入口实时响应 `check_cancelled()`。

**已落地的核心策略规则**：
- **Fail-Closed**：任意 `AccessController` 抛异常或返回非法对象时一律 fail-closed（HTTP 返回 `503`，工具返回 `acl_error`，RAG 返回 `status="acl_error"`）。
- **扩展行级 ACL 下推与混合父文防泄漏**：`extensions/_acl_sql.py` 将白名单 `AccessFilter` 编译为参数化 SQL 下推至可选 RAG 与 Skills 仓储（遇到未支持的 `owner_id`/`any_depts` 谓词直接 fail-closed 拒绝）；空角色主体仅命中 `acl_roles = '{}'` 的公开资源，混合权限父文档自动跳过未授权切片。
- **动态 `policy_rev` 失效保护**：`workflow.run_context` 每次节点进入实时读取 `access_controller.policy_rev(scope)`，暂停期间策略版本递增后，恢复消费自动因 `approval_mismatch` 拒绝执行。

### 6.2 全链路受控资源矩阵与后续演进

| 资源域 | 覆盖动作 | 当前状态 |
| --- | --- | --- |
| HTTP / SDK 入口 | 主体解析与验签 | 已支持 `default` / `header` / `signed_header`（`["v1", user_id, roles]` HMAC 验签失败返 `401`）及自定义解析器 |
| Thread / Run / Trace | 创建、列表、历史、状态快照、中止、删除 | 已在 PG 查询与 `RunService` 强制 `actor_id == scope.user_id` 归属（含仅传 `trace_id` 访问 `/state`） |
| Approval | 审批决定、恢复消费 | SDK 与 HTTP 均按 `caller.user_id` 署名，默认仅创建者可批，支持 `approver_roles`、`forbid_self_approval` 与 `policy_rev` 复核 |
| Tool | `/meta`、主图与子 Agent 可见目录及网关执行 | 目录过滤与执行强拦双一致，主循环与子 Agent 共用 `model_step.py` 单步调用助手 |
| RAG（可选扩展） | 检索、邻块、父文、上传与上下架 | 自持 `extensions/rag/schema.sql`、仓储与管理路由；统一通过 `AccessFilter` 下推 SQL |
| Skills（可选扩展） | 列表、正文、附件、上传、上下架 | 自持 `extensions/skills/schema.sql`、仓储与管理路由；管理与下架预览统一走 `skill:manage` |
| Artifact / Workspace | HTTP 与 `read_tool_output` 分页续读、文件读写 | 统一校验 `resource_owner == scope.user_id`、`artifact:read` 与 TTL |
| Ops / 模型配置 | `/ops/overview`（`AGENT_ENABLE_OPS=1` 可选）、`/config/model` | 拆分运维总览（`ops:view`）与平台运维（`platform:view_telemetry`/`system:update_model`） |

**后续演进路线（Phase 2–3 规划）**：
- **Phase 2（企业部门与增量同步）**：扩展部门/项目组映射表（`EXISTS` 关联大组授权）与文档级 ACL 增量同步。
- **Phase 3（外部策略中心集成）**：提供 OPA / Casbin / 企业 IAM 适配器，支持无法下推时的“有界候选召回 → 批量鉴权 → 读取重排”（显式不承诺凑满 `top_k`）、多副本共享策略版本源与结构化决策审计日志。
