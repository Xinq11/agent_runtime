# Agent Runtime

内部研发团队为不同客户开发 Agent 应用时共用的 Python SDK。

给客户 A 做费用助手，要接 BI 和 OA；给客户 B 做售后助手，要接订单和工单系统。两个项目各自开发工具、提示词、检索策略和页面，都可以沿用同一个 Agent 执行循环、同一套上下文管理，以及已经接好的 PostgreSQL、对象存储和观测服务。

[后端架构](docs/ARCHITECTURE.md) · [HTTP / SSE 接口](docs/API.md)

## 新客户从哪里开始

| 接到的需求 | 客户项目开发 | Runtime 已经提供 |
| --- | --- | --- |
| 查数据、办理业务 | 把 ERP、工单或 OA 接口封装成 Python 工具，定义业务规则 | 工具注册、参数检查、执行超时、审批暂停与恢复、写动作幂等 |
| 让 Agent 理解业务 | 编写系统提示词，选用内置检索或接入客户知识库 | LangGraph 执行循环、原子上下文裁剪、历史摘要、RAG 与 Skills 扩展 |
| 接入客户门户 | 开发对话界面和业务交互，消费 HTTP / SSE v1 事件 | 会话管理、流式回复、工具进度、取消接口，以及参考版 React Studio |
| 排查线上问题 | 用同一次运行的 `trace_id` / `run_id` 定位模型、工具或业务接口 | PG 会话与运行记录、MinIO 大输出、Prometheus 指标、Loki 日志和 Grafana 看板 |

### 最小装配方式

本地开发在项目根目录执行 `python -m pip install -e "./backend[server]"`（纯 SDK 嵌入式使用仅需 `pip install -e ./backend`）。Python 应用可直接装配共享内核与内置标准扩展（依赖已初始化表结构的 PostgreSQL / pgvector 与 MinIO，`PgOssStore` 读取 `PG_DSN` 与 `MINIO_*`）：

```python
import asyncio
from agent_runtime import AgentRuntimeApp, RagExtension, SkillsExtension

app = AgentRuntimeApp(extensions=[RagExtension(), SkillsExtension()])
app.bootstrap_extensions()
result = asyncio.run(app.run_turn("帮我查一下最近一份制度说明"))
```

参考 HTTP 服务通过环境变量 `AGENT_EXTENSIONS` 加载镜像内可导入的 `module.path:Symbol` 列表，客户包独立封装并随服务启动装配，无需修改底座代码。详见 [后端架构](docs/ARCHITECTURE.md) 与 [HTTP / SSE 接口](docs/API.md)。

运行通用版服务可执行 `docker compose up -d --build`。已有部署若显式配置了旧费用插件导入路径，请切换到下面的示例配置。

## 跑一次完整对话

默认启动共享服务，加载 RAG 和 Skills。要体验费用助手，在仓库根目录叠加示例的 Compose 配置。它会安装独立的 Python 包，并将 `enterprise_expense:EnterpriseExpensePlugin` 加入启动列表。

```bash
cp -n .env.example .env
docker compose -f docker-compose.yml -f examples/enterprise_expense/compose.yml up -d --build
```

打开 [对话工作台](http://localhost:8080)，输入：

> 核查华东区 Q3 差旅超标情况，并发起专项审批。

费用示例需配置 `DEEPSEEK_API_KEY`（在 `.env` 中设置，或在页面左下角设置弹窗中热配置；后续将扩展更多模型 API）。PostgreSQL 和 MinIO 全程参与执行，完整流程是**查询 BI 和差旅制度 → 暂停等待人工确认 → 确认后生成示例 OA 回执**。同一次运行的 Trace 与上下文压缩状态可在对话右侧栏展开查看。示例 BI 数据、提示词和 OA 回执仅属于演示客户包，接入新客户时替换为自己的扩展包即可。

| 想看什么 | 本地入口 |
| --- | --- |
| 对话工作台 / 管理知识库、Skills 和会话 | [对话工作台](http://localhost:8080) · [管理后台](http://localhost:8080/ops) |
| 调用接口、核对请求与响应结构 | [OpenAPI](http://localhost:8000/docs) |
| 查指标、日志和步骤耗时 | [Grafana](http://localhost:3000) · [Prometheus](http://localhost:9090) |
| 查看上传文件和工具输出原文 | [MinIO 控制台](http://localhost:9001) |

## 仓库目录与关键配置

| 目录 | 职责 |
| --- | --- |
| [backend/agent_runtime/api](backend/agent_runtime/api/) | FastAPI 路由、SSE v1 推流、审批恢复与管理后台接口 |
| [backend/agent_runtime/framework](backend/agent_runtime/framework/) | 核心契约、上下文预算与裁剪、LangGraph 执行图、Run 状态机、安全 ACL 与工具网关 |
| [backend/agent_runtime/extensions](backend/agent_runtime/extensions/) | 内置标准可选扩展（RAG 7 阶段检索、版本化 Skills），运行期按需装配或替换 |
| [backend/agent_runtime/infra](backend/agent_runtime/infra/) | PostgreSQL + pgvector 仓储、MinIO 对象存储、OpenAI 兼容 LLM 适配、PLG 观测 |
| [examples/enterprise_expense](examples/enterprise_expense/) | 独立客户交付包样板，包含业务工具、子 Agent、提示词、OA 表定义与独立测试 |
| [frontend](frontend/) | React + TypeScript 参考工作台与管理后台（仅通过 HTTP / SSE v1 对接后端） |
| [deploy](deploy/) | PostgreSQL / pgvector 迁移脚本（`deploy/sql/`）与 Prometheus / Loki / Grafana 运维配置 |
| [docs](docs/) | 后端架构文档、HTTP / SSE v1 接口契约与架构配图 |

常用环境变量（完整模板见 [.env.example](.env.example)）：

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | DeepSeek API Key（必填后方可对话；支持页面热配置，仅对当前后端进程生效） | 空（未配置） |
| `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | OpenAI 兼容端点与模型名 | `https://api.deepseek.com/v1` / `deepseek-chat` |
| `AGENT_EXTENSIONS` | 启动加载的扩展列表（逗号分隔 `module:Symbol`，设为 `none` 关闭扩展） | RAG + Skills |
| `AGENT_ACCESS_CONTROLLER` | 可拔插权限控制器：`default`（本地单权限管理 + 资源角色过滤）\| `noop` \| `rbac` \| `module:Symbol` | `default` |
| `AGENT_IDENTITY_RESOLVER` | 可拔插身份解析器：`header` \| `default` \| `signed_header`（配合 `AGENT_GATEWAY_HMAC_SECRET`）\| `module:Symbol` | `header` |
| `PG_DSN` / `MINIO_*` | PostgreSQL 连接串与 MinIO 凭证/桶名（Compose 自动注入） | 见 [docker-compose.yml](docker-compose.yml) |

## 安全边界与可拔插 ACL

- **已实现（Phase 1）**：底座内置可拔插 `IdentityResolver`（支持固定默认、网关头、HMAC 签名头与自定义实现）与 `AccessController`（支持 `default` / `noop` / `rbac` 与自定义实现，企业模式 fail-closed），并在 PG 侧通过白名单结构化 `AccessFilter` 前置下推 SQL、子 Agent 权限单调收窄、服务端可信审批人与 `policy_rev` 挂起票据失效保护。
- **后续演进（Phase 2–3）**：部门/项目组映射表、知识库增量撤权同步与外部 OPA / Casbin / 企业 IAM 适配器，详见 [后端架构 · 安全边界与可拔插 ACL](docs/ARCHITECTURE.md#6-安全边界与可拔插-acl-架构现状与演进)。

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
