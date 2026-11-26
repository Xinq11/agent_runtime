# Agent Runtime (`agent_runtime`)

企业级通用 **Agent Runtime SDK** 与全栈参考工作台：内置 LangGraph 1.x 状态图引擎、生产级基础设施底座（PostgreSQL/pgvector · MinIO OSS · Prometheus/Loki/Grafana · OpenAI 兼容流式 LLM）、可插拔扩展体系（RAG 7 阶段混合检索 · PG+OSS 版本化 Skills · 只读子 Agent）以及可替换前端协议层（HTTP + SSE v1）。

- 后端四层架构与可靠性设计：[backend/ARCHITECTURE.md](backend/ARCHITECTURE.md)
- HTTP & SSE v1 前端接入契约：[backend/API.md](backend/API.md)（启动后在线 Swagger：`http://localhost:8000/docs`）

---

## 核心特性

- **通用 Agent 内核（`framework/`）**
  - 统一 LangGraph 1.x `StateGraph` 引擎，支持流式对话、随时中止（`Abort`）、高危写操作 `interrupt()` 人工审批与断点恢复。
  - **上下文窗口治理**：前缀缓存友好组装、按「`assistant(tool_calls)` + 全部 `tool`」原子组连续裁剪（杜绝孤立 `tool_call` 触发模型 400）、游标式模型摘要压缩。
  - **高可靠工具网关**：基于 JSON Schema 的参数类型校验、单动作 `op_key`（绑定 `tool_call.id`）防重放、超长输出自动截断落 OSS 并支持 `read_tool_output` 分页续读、内置云原生 `read`/`write` 工作区文件工具、只读子 Agent（`max_depth=1`）。
  - **按步原子持久化**：每步工具执行完毕即时幂等落库，同批次中断自动补齐 `[cancelled]` 记录，防止写操作副作用丢失；SSE 断连非阻塞摘除，杜绝线程 409 死锁。
- **可插拔扩展层（`extensions/`）**
  - **RAG 7 阶段检索**：Sparse + Dense 双路召回 → RRF 融合 → 邻接 Child 扩展 → 每文档上限 2 多样化 → 可插拔 Rerank（异常自动降级）→ 终排融合 → 无分数父级证据聚合；支持替换 `EmbeddingClient`/`RerankClient` 或接入自定义 `RAGStrategy`。
  - **云原生声明式 Skills**：PG 目录 + 不可变 OSS 版本存储，支持热上传 `.md`/`.txt` 文件（严禁可执行脚本）与实时生效/下架切换。
- **生产级底座与参考 Studio 前端**
  - `psycopg_pool.ConnectionPool` 连接池、MinIO S3 对象存储、Prometheus/Loki/Grafana 预置观测大盘、DeepSeek / OpenAI / vLLM 流式接入（无 Key 自动降级本地 Mock）。
  - 参考 React 18 + Tailwind + Apache ECharts 工作台：支持历史会话分组与删除、实时 Trace 瀑布图（Hamilton 100.0% 占比分配）、ECharts 交互图表导出 PNG、以及精简管理后台（会话上下文压缩透视、知识库文件上传与上下架、Skill 文件上传与右侧抽屉预览）。

---

## 快速启动（Docker Compose 全栈）

```bash
cp .env.example .env
# 如需接入真实模型，在 .env 中填入 DEEPSEEK_API_KEY（留空则自动使用本地 Mock 模式）
docker compose up -d --build
```

启动后访问入口：

| 服务 | 访问地址 | 说明 |
| :--- | :--- | :--- |
| **对话工作台** | `http://localhost:8080` | 流式对话、工具折叠、高危审批、ECharts 图表、Trace 瀑布图、历史会话删除 |
| **管理后台** | `http://localhost:8080/ops` | 会话上下文与压缩透视、知识库文件上传与生效/下架、Skills 上传/抽屉预览/上下架 |
| **后端 OpenAPI** | `http://localhost:8000/docs` | 标准 `/api/v1/*` 接口交互文档（任意前端可直接对接） |
| **Grafana 大盘** | `http://localhost:3000` | 预置 Agent Runtime Overview 看板（默认 `admin` / `admin`） |
| **MinIO 控制台** | `http://localhost:9001` | 对象存储浏览（默认 `minioadmin` / `minioadmin`） |
| **Prometheus** | `http://localhost:9090` | 实时指标抓取与 PromQL 查询 |

---

## Python SDK 快速上手

### 1. 挂载业务 `@tool` 与标准扩展

```python
import asyncio
from agent_runtime import AgentRuntimeApp, RagExtension, SkillsExtension, RiskLevel, tool

@tool(risk_level=RiskLevel.READ)
async def query_erp_order(order_no: str) -> str:
    """按订单号查询 ERP 订单状态。"""
    return f"订单 {order_no} 已发货"

async def main():
    app = AgentRuntimeApp(
        tools=[query_erp_order],
        extensions=[RagExtension(), SkillsExtension()],
    )
    res = await app.run_turn("查一下订单 ORD-202601 状态", thread_id="th_1")
    print(res["final_answer"])

asyncio.run(main())
```

### 2. 继承 `BaseAgent` 定制主 Agent

```python
from agent_runtime import BaseAgent, SubAgentProfile

class FinanceAgent(BaseAgent):
    name = "finance_agent"
    system_prompt = "你是财务合规助手，先查证再作答。"

    def build_subagents(self):
        return (
            SubAgentProfile(
                tool_name="call_ledger_checker",
                role="ledger_checker",
                description="只读总账核对子代理",
                system_prompt="仅输出核对结论与差异金额。",
                allowed_tools=("query_ledger",),
            ),
        )

app = FinanceAgent().build_app()
```

### 3. 通过环境变量动态装配服务扩展

```bash
# 纯净核心模式（仅内置 read / write / read_tool_output + 上下文治理 + Abort + PG/OSS/PLG）
AGENT_EXTENSIONS=none

# 叠加标准 RAG / Skills 与客户业务包（默认配置）
AGENT_EXTENSIONS=agent_runtime.extensions.rag.extension:RagExtension,agent_runtime.extensions.skills.extension:SkillsExtension,agent_runtime.extensions.customers.enterprise_expense:EnterpriseExpensePlugin
```

---

## 目录结构

```text
agent-runtime/
├── backend/
│   ├── ARCHITECTURE.md                  # 后端四层架构、状态机与可靠性设计
│   ├── API.md                           # 面向可插拔前端的 HTTP + SSE v1 协议文档
│   ├── Dockerfile & requirements.txt    # Python 3.12 生产镜像与依赖
│   ├── pyproject.toml                   # SDK 分发配置 (import agent_runtime)
│   ├── agent_runtime/
│   │   ├── framework/                   # Layer 1: 通用内核 SDK (agent/core/context/tools)
│   │   ├── extensions/                  # Layer 2: 可插拔扩展 (rag / skills / customers)
│   │   ├── infra/                       # Layer 3: 生产底座 (pg 连接池 / oss / plg / llm / memory)
│   │   └── api/                         # Layer 4: FastAPI HTTP + SSE v1 路由层
│   └── tests/
│       ├── test_runtime.py              # 13 项核心红线、RAG、Skills、Abort 与一致性测试
│       ├── test_regressions.py          # 8 项生产回归测试 (SSE 断连 / 原子裁剪 / 中途 abort / 类型校验)
│       └── test_langgraph_real.py       # 2 项真实 LangGraph 中断重放幂等集成测试
├── frontend/
│   └── src/
│       ├── features/chat/               # 对话画布、侧边栏 (含删除)、审批卡、SSE 状态管理
│       ├── features/ops/                # 管理后台 (上下文透视 / 知识库上传上下架 / Skill 抽屉)
│       ├── features/trace/              # Trace 瀑布图与纯函数时间线计算
│       └── shared/                      # API 客户端、Markdown、按需加载 ECharts 图表
├── infra/                               # Prometheus / Loki / Grafana 配置与预置看板
├── sql/                                 # PostgreSQL + pgvector 初始化与增量迁移 (001~003)
└── docker-compose.yml                   # 7 容器全栈编排
```

---

## 测试与验证

```bash
# 1. 宿主机快速运行离线单测（Python 3.9+ 零三方依赖，约 0.5s）
PYTHONPATH=backend python3 -m unittest discover -s backend/tests -v

# 2. Docker 容器内运行全量 23 项测试（含真实 LangGraph 1.x 中断重放与 FastAPI SSE 断连测试）
docker compose exec -T backend python -m unittest discover -s /app/backend/tests -v
```
