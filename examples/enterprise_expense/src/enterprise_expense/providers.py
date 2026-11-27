"""Customer-owned sample `RagProvider` & `SkillProvider` implementations for `enterprise_expense`.

Demonstrates the intended SDK boundary: customer plugins own their own knowledge base,
skill catalog, and business-level authorization rules, while the SDK wires `search_knowledge`,
`list_skills`, `get_skill`, and Layer-1 progressive disclosure automatically.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from agent_runtime import AuthScope, ExecutionContext, RagHit, RagResult, SkillMeta

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

_EXPENSE_DOCS: Tuple[Dict[str, object], ...] = (
    {
        "title": "2026 差旅与费用报销制度",
        "section": "住宿标准与超标审批红线",
        "url": "kb://travel_2026",
        "content": "一线城市差旅住宿标准上限 650 元/晚；季度部门累计超标超过 10% 必须发起专项审批。",
        "roles": frozenset({"employee", "finance", "exec"}),
        "keywords": ("差旅", "住宿", "报销", "超标", "审批", "650", "制度"),
    },
    {
        "title": "高管薪酬方案（绝密）",
        "section": "奖金池",
        "url": "kb://exec_comp",
        "content": "高管年度奖金系数为 3.5x，仅限 exec 角色查看。",
        "roles": frozenset({"exec"}),
        "keywords": ("高管", "薪酬", "奖金", "3.5x", "系数"),
    },
)


class ExpenseRagProvider:
    """Example customer RAG adapter enforcing customer-defined role visibility."""

    def retrieve(
        self,
        *,
        scope: AuthScope,
        query: str,
        top_k: int = 4,
        ctx: Optional[ExecutionContext] = None,
    ) -> RagResult:
        q = (query or "").strip().lower()
        if not q:
            return RagResult(status="no_hit")
        caller_roles = set(scope.roles)
        hits: List[RagHit] = []
        for doc in _EXPENSE_DOCS:
            allowed_roles = doc["roles"]
            if allowed_roles and not (caller_roles & allowed_roles):  # type: ignore[operator]
                continue
            haystack = f"{doc['title']} {doc['section']} {doc['content']}".lower()
            keywords = doc["keywords"]
            if q in haystack or any(kw in q for kw in keywords):  # type: ignore[iterable]
                hits.append(
                    RagHit(
                        title=str(doc["title"]),
                        section=str(doc["section"]),
                        source_url=str(doc["url"]),
                        content=str(doc["content"]),
                    )
                )
        if not hits:
            return RagResult(status="no_hit")
        return RagResult(status="ok", hits=tuple(hits[:top_k]))


class ExpenseSkillProvider:
    """Example customer Skill adapter enforcing customer-defined role visibility."""

    def __init__(self) -> None:
        self._skills = [
            {
                "meta": SkillMeta(
                    slug="expense-audit-report",
                    description="差旅超标分析、制度核验、ECharts 图表输出与工作区报告归档 SOP。",
                    tags=("finance", "report", "sop"),
                ),
                "roles": frozenset({"employee", "finance", "exec"}),
                "body": DEFAULT_EXPENSE_SKILL_MD,
                "resources": {},
            }
        ]

    def _visible(self, scope: AuthScope):
        caller_roles = set(scope.roles)
        return [s for s in self._skills if not s["roles"] or (caller_roles & s["roles"])]

    def list_skills(self, scope: AuthScope, query: str = "") -> Sequence[SkillMeta]:
        q = (query or "").strip().lower()
        visible = self._visible(scope)
        if not q:
            return [s["meta"] for s in visible]
        matched = []
        for s in visible:
            m: SkillMeta = s["meta"]
            text = f"{m.slug} {m.description} {' '.join(m.tags)}".lower()
            if q in text or any(tok in text for tok in q.split() if len(tok) >= 2):
                matched.append(m)
        return matched

    def get_skill(self, scope: AuthScope, slug: str, resource: Optional[str] = None) -> Optional[str]:
        clean = (slug or "").strip().lower()
        for s in self._visible(scope):
            if s["meta"].slug == clean:
                if resource:
                    return s["resources"].get(resource)
                return s["body"]
        return None
