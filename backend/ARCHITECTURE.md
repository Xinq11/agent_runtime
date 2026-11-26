# `agent_runtime` 四层架构设计图（v1.0 定稿）

> 对应包根：`backend/agent_runtime/`（对外统一导入 `from agent_runtime import ...`）
>
> 定位：**内部研发团队统一共享的核心 SDK + 完整生产级 Infra（PG / OSS / PLG / LLM）+ 按客户插拔的扩展层 + 可替换前端协议层（HTTP / SSE v1）**。

---

## 1. 四层总体架构图

```mermaid
flowchart TB
    FE["任意可插拔前端 (官方 React Studio / Vue / 企业门户 / CLI)"]

    subgraph L4["Layer 4: api/ 传输契约层 (HTTP + SSE v1 / OpenAPI / API.md)"]
        API_MAIN["main.py (/api/v1 规范路由 + /api 兼容别名 + HTTP 访问日志)"]
        API_CHAT["chat.py (/chat/sse 标准信封, /chat/abort, /runs/{id}, /threads, /approvals/resume, /state)"]
        API_SKILLS["skills.py (/skills 列表/正文/热上传/下线)"]
        API_OPS["ops.py (/ops/overview, /ops/outputs/{ref_id}, /knowledge/upload)"]
        API_SYS["system.py (/health, /meta, /config/model, /metrics)"]
    end

    subgraph L1["Layer 1: framework/ 不可变通用内核 SDK"]
        AGENT["agent/ (BaseAgent 继承基类 + AgentRuntimeApp 装配器 + load_extension)"]
        RUNS["core/runs.py (RunService 持久状态机 + 流式 Abort 注册表)"]
        WF["core/workflow.py (统一 LangGraph StateGraph 无 SQL 内核)"]
        MW["core/middleware.py + protocols.py (MiddlewareChain / AgentPlugin / RAGStrategy / LLMProvider)"]
        CTX["context/memory.py (4段前缀缓存预算切片 + 游标模型摘要压缩)"]
        TOOLS["tools/ (ToolGateway + @tool + SubAgent 只读隔离 + 内置不可变 read/write/read_tool_output)"]
    end

    subgraph L2["Layer 2: extensions/ 可扩展与客户定制层 (按需挂载)"]
        EXT_RAG["rag/ (RagExtension: 默认 ChatBI/CRAG 7阶段分层管线 + Embedding/Rerank 可插拔 + 自定义 RAGStrategy)"]
        EXT_SKILLS["skills/ (SkillsExtension: list_skills / get_skill 只读工具 + 声明式安全校验)"]
        EXT_CUST["customers/enterprise_expense/ (示例客户包: 业务工具 + 只读子代理 + 种子知识与 SOP Skill)"]
    end

    subgraph L3["Layer 3: infra/ 生产级基础设施底座 (严格四大件)"]
        INFRA_PG["pg/ (PgOssStore: PG + pgvector + agent_runs + workspace_files + agent_skills + AsyncPostgresSaver)"]
        INFRA_OSS["oss/ (MinioObjectStore: 大输出/工作文件/知识文档/Skill 版本正文)"]
        INFRA_PLG["plg/ (Telemetry: Prometheus 低基数指标 + Loki 异步批量日志 + PG Spans)"]
        INFRA_LLM["llm/ (OpenAICompatibleProvider: DeepSeek / OpenAI / vLLM + MockScriptedLLM)"]
    end

    subgraph EXT["Docker Compose 基础设施集群"]
        PG[("PostgreSQL 16 + pgvector")]
        OSS[("MinIO OSS")]
        PLG[("Prometheus + Loki + Grafana")]
        LLM(("DeepSeek / OpenAI 兼容模型"))
    end

    FE -- "HTTP REST (/api/v1/*)" --> L4
    L4 -- "SSE v1 Envelope (schema_version / run_id / seq / type / payload)" --> FE

    L4 --> AGENT
    L4 --> RUNS
    AGENT --> WF
    AGENT --> MW
    AGENT --> TOOLS
    AGENT --> L2

    WF --> CTX
    WF --> MW
    WF --> TOOLS
    WF --> INFRA_LLM

    TOOLS --> MW
    TOOLS --> INFRA_PG
    TOOLS --> INFRA_PLG

    EXT_RAG --> INFRA_PG
    EXT_RAG --> INFRA_LLM
    EXT_SKILLS --> INFRA_PG
    EXT_CUST --> TOOLS

    RUNS --> INFRA_PG
    CTX --> INFRA_PG
    CTX --> INFRA_LLM

    INFRA_PG --> PG
    INFRA_PG --> INFRA_OSS
    INFRA_OSS --> OSS
    INFRA_PLG --> PG
    INFRA_PLG --> PLG
    INFRA_LLM --> LLM
```

---

## 2. Run 生命周期与流式 Abort 状态机

```mermaid
stateDiagram-v2
    [*] --> running: POST /api/v1/chat/sse (短事务写 agent_runs)
    running --> paused: 命中 HIGH_RISK 工具 interrupt()
    running --> completed: 正常执行完毕 (finish)
    running --> cancelled: POST /api/v1/chat/abort
    running --> failed: 异常终止
    paused --> running: POST /api/v1/approvals/{id}/resume
    paused --> cancelled: POST /api/v1/chat/abort (不可再被旧审批复活)
    completed --> [*]
    cancelled --> [*]
    failed --> [*]
```

---

## 3. 核心设计约束与边界说明

1. **内置工具防误覆盖边界（非恶意代码沙箱）**：`read`、`write`、`read_tool_output` 为云原生内置工具（全跑在 PG + OSS 上，零本地磁盘依赖）；启动后冻结 `ToolRegistry` 防止同进程扩展意外覆写或运行期篡改。注意：同进程 Python 插件共享宿主进程内存，属于**工程解耦与防误覆盖边界**，不构成针对不可信恶意 Python 代码的 OS 沙箱。
2. **默认身份约定**：当前基础服务默认采用统一基础权限，并信任上游内网网关传入的身份头（`X-User-Id` / `X-Tenant-Id`）；若对外暴露服务，需在接入层网关统一完成 JWT / OAuth 鉴权再透传身份头。
3. **单动作幂等键**：写工具 `op_key` 绑定持久化的 `tool_call.id`，一轮合法多次 `write` 互不撞键，同动作篡改参数重放立即触发 `op_key_conflict`。
4. **Agent as Tool 多智能体**：默认单主 Agent 开箱即用；通过 `SubAgentProfile` 可按需挂接任意数量只读子代理（`max_depth=1`、`allow_write=False`、私有窗口隔离、并发执行）。
5. **声明式 Skills 安全红线**：Skills 存 `agent_skills` + 不可变 `agent_skill_versions` + OSS；热上传先写 OSS 再切 PG 指针，严禁上传可执行脚本文件（可执行能力必须走受审 Python 插件）。
6. **CRAG 7 阶段检索管线（对齐 ChatBI/rag-retrieval）**：严格分层类型（`SparseSearchResult → DenseSearchResult → RrfFusionResult → 相邻 Child 扩展 → CandidateDiversifier(每文档上限2) → RerankService + FinalRankFusionService(k=60, w=0.8) → ParentEvidenceResult`）；`retrieve_chunks()` 输出 4 阶段独立得分供诊断评估，`retrieve_evidence()` 输出无分数的父级证据供 LLM 消费；支持可插拔 `EmbeddingClient` / `RerankClient` 或传入自定义 `RAGStrategy`。
