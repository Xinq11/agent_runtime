# Agent Runtime

内部研发团队为不同客户开发 Agent 应用时共用的 Python SDK。

给客户 A 做费用助手，要接 BI 和 OA；给客户 B 做售后助手，要接订单和工单系统。两个项目各自开发工具、提示词、检索策略和页面，都可以沿用同一个 Agent 执行循环、同一套上下文管理，以及已经接好的 PostgreSQL、对象存储和观测服务。

[后端架构](docs/ARCHITECTURE.md) · [HTTP / SSE 接口](docs/API.md)

## 新客户从哪里开始

| 接到的需求 | 客户项目开发 | Runtime 已经提供 |
| --- | --- | --- |
| 查数据、办理业务 | 把 ERP、工单或 OA 接口封装成 Python 工具，定义业务规则 | 工具注册、参数检查、执行超时、审批暂停与恢复、写动作幂等 |
| 让 Agent 理解业务 | 编写系统提示词，实现 `RagProvider` / `SkillProvider` 对接客户知识库与 SOP（含客户自定义权限） | LangGraph 执行循环、原子上下文裁剪、历史摘要、Skills 两层渐进披露与 RAG 引文协议 |
| 接入客户门户 | 开发对话界面和业务交互，消费 HTTP / SSE v1 事件 | 会话管理、流式回复、工具进度、取消接口，以及参考版 React Studio |
| 排查线上问题 | 用同一次运行的 `trace_id` / `run_id` 定位模型、工具或业务接口 | PG 会话与运行记录、MinIO 大输出、Prometheus 指标、Loki 日志和 Grafana 看板 |

### 最小装配方式

本地开发在项目根目录执行 `python -m pip install -e "./backend[server]"`（纯 SDK 嵌入式使用仅需 `pip install -e ./backend`）。纯 SDK 内核**不内置任何 RAG/Skills 存储、不依赖 `pgvector`、不启动管理后台**，只需要 PostgreSQL 与 MinIO（`PgOssStore` 读取 `PG_DSN` 与 `MINIO_*`）：

```python
import asyncio
from agent_runtime import AgentRuntimeApp, AuthScope

app = AgentRuntimeApp(
    # 按需插入任意自研或三方实现（SDK 自动接好渐进披露、工具注册、引文格式化与预算控制）
    # skill_provider=MySkillProvider(),
    # rag_provider=MyRagProvider(),
)
result = asyncio.run(app.run_turn("帮我梳理待办清单", scope=AuthScope("u_alice")))
```

SDK 只保留两个极薄的 Harness 口子，**不管具体存储、检索引擎与文档级权限实现**：
- **`SkillProvider`**：实现 `list_skills(scope, query)` 与 `get_skill(scope, slug, resource)`；SDK 自动注册 `list_skills` / `get_skill` 工具并完成 Layer-1 目录注入（Prefix Cache 稳定）。
- **`RagProvider`**：实现 `retrieve(scope, query, top_k, ctx)`；SDK 自动注册 `search_knowledge` 工具（可选自动注入，受 `ContextBudget` 约束）与 `[1]` 编号引文规范。

参考 HTTP 服务默认纯 SDK 模式（`AGENT_EXTENSIONS=""`），不内置任何管理后台页面或知识库/Skill 运维接口；通过环境变量 `AGENT_EXTENSIONS` 按需加载客户包（`module.path:Symbol`）。详见 [后端架构](docs/ARCHITECTURE.md) 与 [HTTP / SSE 接口](docs/API.md)。

运行通用版服务可执行 `docker compose up -d --build`。

## 跑一次完整对话

默认启动纯 SDK 核心服务。要体验费用助手，在仓库根目录叠加示例的 Compose 配置。它会安装独立的 Python 包 `enterprise_expense`，并注入示例工具、子 Agent、以及客户自实现的 `ExpenseRagProvider` / `ExpenseSkillProvider`（含示例角色鉴权）。

```bash
cp -n .env.example .env
docker compose -f docker-compose.yml -f examples/enterprise_expense/compose.yml up -d --build
```

打开 [对话工作台](http://localhost:8080)，输入：

> 核查华东区 Q3 差旅超标情况，并发起专项审批。

费用示例需配置 `DEEPSEEK_API_KEY`（在 `.env` 中设置，或在页面左下角设置弹窗中热配置；后续将扩展更多模型 API）。PostgreSQL 和 MinIO 全程参与执行，完整流程是**查询 BI 和差旅制度 → 暂停等待人工确认 → 确认后生成示例 OA 回执**。同一次运行的 Trace 与上下文压缩状态可在对话右侧栏展开查看。示例 BI 数据、提示词和 OA 回执仅属于演示客户包，接入新客户时替换为自己的扩展包即可。

| 想看什么 | 本地入口 |
| --- | --- |
| 对话工作台（纯会话参考前端，不含管理后台） | [对话工作台](http://localhost:8080) |
| 调用接口、核对请求与响应结构 | [OpenAPI](http://localhost:8000/docs) |
| 查指标、日志和步骤耗时 | [Grafana](http://localhost:3000) · [Prometheus](http://localhost:9090) |
| 查看上传文件和工具输出原文 | [MinIO 控制台](http://localhost:9001) |

## 仓库目录与关键配置

| 目录 | 职责 |
| --- | --- |
| [backend/agent_runtime/api](backend/agent_runtime/api/) | FastAPI 核心路由、SSE v1 推流、审批恢复与大输出分页接口 |
| [backend/agent_runtime/framework](backend/agent_runtime/framework/) | 核心契约、上下文预算与裁剪、LangGraph 执行图、Run 状态机、安全契约、工具网关，以及 `SkillProvider` / `RagProvider` 薄适配层 |
| [backend/agent_runtime/infra](backend/agent_runtime/infra/) | 核心 PostgreSQL 仓储、MinIO 对象存储、OpenAI 兼容 LLM 适配、PLG 观测 |
| [examples/enterprise_expense](examples/enterprise_expense/) | 独立客户交付包样板，包含业务工具、声明式子 Agent、客户自实现 `ExpenseRagProvider` / `ExpenseSkillProvider`、OA 表定义与独立测试 |
| [frontend](frontend/) | React + TypeScript 纯对话参考工作台（仅对接核心 HTTP / SSE v1，不含管理后台） |
| [deploy](deploy/) | PostgreSQL 迁移脚本（`deploy/sql/`）与 Prometheus / Loki / Grafana 运维配置 |
| [docs](docs/) | 后端架构文档、HTTP / SSE v1 接口契约与架构配图 |

常用环境变量（完整模板见 [.env.example](.env.example)）：

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | DeepSeek API Key（必填后方可对话；支持页面热配置，仅对当前后端进程生效） | 空（未配置） |
| `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | OpenAI 兼容端点与模型名 | `https://api.deepseek.com/v1` / `deepseek-chat` |
| `AGENT_EXTENSIONS` | 启动加载的客户插件列表（逗号分隔 `module:Symbol`；纯 SDK 默认空，费用示例为 `enterprise_expense:EnterpriseExpensePlugin`） | 空 |
| `AGENT_ACCESS_CONTROLLER` | 可拔插权限控制器：`default`（创建者私有会话与审批）\| `noop` \| `rbac` \| `module:Symbol` | `default` |
| `AGENT_IDENTITY_RESOLVER` | 可拔插身份解析器：`header` \| `default` \| `signed_header`（配合 `AGENT_GATEWAY_HMAC_SECRET`）\| `module:Symbol` | `header` |
| `PG_DSN` / `MINIO_*` | PostgreSQL 连接串与 MinIO 凭证/桶名（Compose 自动注入） | 见 [docker-compose.yml](docker-compose.yml) |

## 安全边界与可拔插 ACL

- **单客户独立部署边界**：内核只需要可信的 `user_id`（已移除核心 `tenant_id`）；会话、消息、工作区、工具输出（`ref_id`）、运行记录（`run_id`）、链路轨迹（`trace_id`）及默认审批恢复统一执行创建者归属硬隔离。
- **职责清晰的权限边界**：SDK 底座只负责 Harness 层点查鉴权（`IdentityResolver` + `AccessController.check()` / `policy_rev()`，控制会话、审批、工具、工件与模型配置）；知识库文档级 / Skill 级权限过滤完全留在客户自己的 `RagProvider` / `SkillProvider` 实现中，不耦合进 SDK，详见 [后端架构 · 安全边界与可拔插 ACL](docs/ARCHITECTURE.md#6-安全边界与可拔插-acl-架构)。

## 开发与验证

后端依赖清单见 [requirements.txt](backend/requirements.txt) 与 [pyproject.toml](backend/pyproject.toml)。测试直接运行在真实 **LangGraph + PostgreSQL (pgvector) + MinIO** 上，夹具（[backend/tests/_pg_harness.py](backend/tests/_pg_harness.py)）默认连接独立测试库 `agent_runtime_test` 与测试桶 `agent-artifacts-test`（内置护栏拒绝生产库与系统库，仅允许 `*_test` / `test_*`）。

在后端容器中运行 SDK 回归测试：

```bash
docker compose exec -T backend python -m unittest discover -s /app/backend/tests -v
```

启动费用示例后，在同一环境运行插件的集成测试。

```bash
docker compose -f docker-compose.yml -f examples/enterprise_expense/compose.yml run --rm --no-deps -v ./examples/enterprise_expense/tests:/opt/expense-tests:ro backend python -m unittest discover -s /opt/expense-tests -v
```

修改前端代码后，在容器内执行 TypeScript 类型检查与构建：

```bash
docker compose up -d --build frontend
```
