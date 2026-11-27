"""Official sample customer extension pack: Enterprise Travel & Expense Analyst."""

from __future__ import annotations

from importlib.resources import files
from typing import Any, Dict, List, Optional, Sequence

from .tools import create_expense_tools
from agent_runtime.extensions.rag.repo import PgRagRepository
from agent_runtime.extensions.skills.repo import PgSkillsRepository
from agent_runtime.framework.core.contracts import AuthScope
from agent_runtime.framework.core.protocols import AgentPlugin, AppMetadata, QuickPromptSpec
from agent_runtime.framework.tools.subagent import SubAgentProfile

DEFAULT_EXPENSE_SKILL_MD = """# 差旅超标分析与报告 SOP

当用户要求核查部门差旅超标、生成分析报告或发起超标审批时，请严格按以下步骤执行：

1. **数据核查**：调用 `bi_query`（或委派只读子代理 `call_bi_analyst`）查询对应大区和季度的超标笔数、金额、分月明细及费用构成。
2. **制度核验**：调用 `search_knowledge` 查询最新差旅住宿标准与超标审批红线，并在结论中以 `[1]` 标注制度出处。
3. **可视化呈现**：
   - 使用标准 Markdown 表格呈现关键汇总指标；
   - 使用 `chart` 代码块输出 ECharts 柱状图（分月趋势）或环形饼图（费用构成）。
4. **文档沉淀（按需）**：若用户需要归档报告，调用内置 `write` 工具写入如 `reports/expense_summary.md`。
5. **高危审批（严格按需）**：仅在用户明确要求「发起/提交审批」时调用 `submit_approval` 触发人工确认；若用户仅要求分析数据或撰写报告，严禁主动调用 `submit_approval`，仅在报告末尾说明是否满足超标红线并询问用户是否需要发起审批。
"""


class EnterpriseExpensePlugin(AgentPlugin):
    name = "enterprise_expense"
    system_prompt = (
        "你是企业助手。查证后作答；仅在引用企业知识库制度条款时标注来源 [1]（回答其他话题无需提及来源或制度规则）。业务写入必须调用相应工具。\n"
        "前端内置 Apache ECharts 交互式图表与 Markdown 表格渲染！当用户要求画图、走势图、柱状图、堆叠图、双轴图、饼图、雷达图或漏斗图时，"
        "请直接输出 ```chart 代码块（严格合法 JSON，不要注释），格式示例：\n"
        "```chart\n"
        "{\"type\":\"bar\",\"title\":\"图表标题\",\"unit\":\"元\",\"x\":[\"第1月\",\"第2月\",\"第3月\"],\"series\":[{\"name\":\"超标金额\",\"data\":[21000,28500,32500]}]}\n"
        "```\n"
        "type 支持 bar（柱状图）、stacked_bar（堆叠柱状图）、line（折线图）、area（面积图）、combo（柱线双轴混合图，series 可带 type/yAxisIndex）、"
        "pie（环形占比图，用 data:[{\"name\":\"...\",\"value\":123}]）、funnel（漏斗图）、radar（雷达图）与 scatter（散点图）。"
    )

    def __init__(self, *, approval_writes: Optional[List[Dict[str, Any]]] = None) -> None:
        self.approval_writes = approval_writes if approval_writes is not None else []

    def default_identity(self) -> AuthScope:
        return AuthScope(user_id="u_alice", roles=("employee", "exec"))

    def metadata(self) -> AppMetadata:
        return AppMetadata(
            app_name="Agent Runtime",
            subtitle="Workspace",
            plugin_name=self.name,
            welcome_title="今天查点什么，或者一起做哪件事？",
            welcome_description=(
                "统一核心 SDK + 生产级 PG/OSS/PLG 底座：支持 ECharts 交互图表、云原生文件读写、RAG 混合检索、PG+OSS 技能库、只读子 Agent 与高危审批恢复。"
            ),
            quick_prompts=(
                QuickPromptSpec(
                    title="查分月超标并画 ECharts 图表",
                    hint="调用 BI 工具核查分月明细与费用构成，输出表格和交互图表",
                    prompt="查一下华东区 Q3 差旅超标分月数据和费用构成，用表格展示并画一张柱状图",
                ),
                QuickPromptSpec(
                    title="并行子 Agent + RAG + 高危审批",
                    hint="并行派发只读 BI 子代理与制度检索，发起高危写审批暂停恢复",
                    prompt="查华东区 Q3 差旅超标明细，结合最新报销制度帮我发起专项审批",
                ),
                QuickPromptSpec(
                    title="按技能 SOP 分析并写入报告文件",
                    hint="通过 list_skills/get_skill 读取平台 SOP，并用内置 write 工具沉淀报告到 OSS",
                    prompt="先查一下平台里有没有差旅分析相关的 Skill，按 SOP 查华东区 Q3 数据并把报告写到 reports/q3.md",
                ),
                QuickPromptSpec(
                    title="大输出截断落 OSS + 分页续读",
                    hint="超限原文自动落 MinIO OSS 返回 ref_id，按页续读防爆上下文",
                    prompt="帮我拉取全量明细并预览前 300 字",
                ),
            ),
        )

    def register_tools(self, *, registry, store, model, gateway, obs) -> None:
        for spec in create_expense_tools(store, approval_writes=self.approval_writes):
            registry.register(spec)

    def subagents(self) -> Sequence[SubAgentProfile]:
        return (
            SubAgentProfile(
                subagent_name="bi_analyst",
                description="委派只读 BI 分析师",
                system_prompt="你负责核验差旅数字并汇总结果。",
                allowed_tools=("bi_query",),
            ),
        )

    def bootstrap_store(self, store: Any) -> None:
        ddl = files(__package__).joinpath("schema.sql").read_text(encoding="utf-8")
        with store.transaction() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('enterprise_expense.schema'))")
            cur.execute(ddl)
        scope = AuthScope("seed", ("employee", "exec"))
        if store.rows("SELECT to_regclass('rag_parents') AS tbl")[0]["tbl"]:
            rag_repo = PgRagRepository(store)
            rag_repo.index_document(
                scope,
                "2026 差旅与费用报销制度",
                "一线城市差旅住宿标准上限 650 元/晚；季度部门累计超标超过 10% 必须发起专项审批。",
                ["employee", "finance"],
                parent="travel_2026",
                section="住宿标准与超标审批红线",
            )
            rag_repo.index_document(
                scope,
                "高管薪酬方案（绝密）",
                "高管年度奖金系数为 3.5x，仅限 exec 角色查看。",
                ["exec"],
                parent="exec_comp",
                section="奖金池",
            )
        if store.rows("SELECT to_regclass('agent_skills') AS tbl")[0]["tbl"]:
            skills_repo = PgSkillsRepository(store)
            if not skills_repo.list_skills(scope, query="expense-audit-report", include_disabled=True, bypass_acl=True):
                skills_repo.publish_skill(
                    scope=scope,
                    slug="expense-audit-report",
                    description="差旅超标分析、制度核验、ECharts 图表输出与工作区报告归档 SOP。",
                    content=DEFAULT_EXPENSE_SKILL_MD,
                    acl_roles=["employee", "finance", "exec"],
                    tags=["finance", "report", "sop"],
                )
