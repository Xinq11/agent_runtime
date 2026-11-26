# Agent Runtime SDK (`agent_runtime`)

面向内部研发团队的**统一核心 Agent SDK + 生产级基础设施底座（PG / OSS / PLG / LLM）+ 按客户插拔扩展体系 + 可替换前端契约（HTTP / SSE v1）**。

- 详细架构图：[backend/ARCHITECTURE.md](backend/ARCHITECTURE.md)
- 完整 HTTP / SSE v1 前端接入文档：[backend/API.md](backend/API.md)（线上 Swagger：`http://localhost:8000/docs`）

## 10 行代码快速开始（Python SDK）

```python
import asyncio
from agent_runtime import AgentRuntimeApp, BaseAgent, RagExtension, SkillsExtension, RiskLevel, tool

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

### 环境变量装配扩展（无需改内核或前端）

```bash
# 纯净核心 SDK 模式（仅内置不可变 read / write / read_tool_output + 上下文压缩 + Abort + PG/OSS/PLG）
AGENT_EXTENSIONS=none

# 叠加标准能力扩展 + 指定客户包（默认）
AGENT_EXTENSIONS=agent_runtime.extensions.rag.extension:RagExtension,agent_runtime.extensions.skills.extension:SkillsExtension,agent_runtime.extensions.customers.enterprise_expense:EnterpriseExpensePlugin
```

## 目录结构

```text
agent-runtime/
├── backend/
│   ├── ARCHITECTURE.md           # 四层架构与状态机设计图
│   ├── API.md                    # 面向可插拔前端的 HTTP / SSE v1 完整协议文档
│   ├── pyproject.toml            # 可打包分发包名: agent-runtime (import agent_runtime)
│   ├── agent_runtime/
│   │   ├── framework/            # Layer 1: 通用内核 SDK (core/agent/context/tools，启动冻结防误覆盖)
│   │   ├── extensions/           # Layer 2: 可插拔扩展层 (rag CRAG / skills PG+OSS / customers)
│   │   ├── infra/                # Layer 3: 生产级四大底座 (pg 连接池 / oss / plg / llm)
│   │   └── api/                  # Layer 4: HTTP + SSE v1 传输契约层（默认信任内网网关身份头）
│   └── tests/                    # 核心红线、生产回归与真实 LangGraph 集成测试
├── frontend/                     # 官方可插拔参考 Studio 前端 (React 18 + ECharts + Tailwind)
├── infra/                        # Prometheus / Loki / Grafana 配置与预置大盘
├── sql/                          # PostgreSQL + pgvector 初始化与增量迁移 (001/002/003)
└── docker-compose.yml            # 7 容器全栈生产环境
```

## 测试与部署

```bash
# 运行后端 12 项单元测试（零第三方依赖）
PYTHONPATH=backend python3 -m unittest discover -s backend/tests -v

# 启动 7 容器全栈生产环境
cp .env.example .env
docker compose up -d --build
```
