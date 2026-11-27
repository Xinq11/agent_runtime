"""Domain tools for the official `enterprise_expense` customer delivery pack using `@tool`."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent_runtime.framework.core.config import uid
from agent_runtime.framework.core.contracts import ExecutionContext, RiskLevel, canonical_hash
from agent_runtime.framework.tools.decorator import tool


def create_expense_tools(store: Any, *, approval_writes: Optional[List[Dict[str, Any]]] = None):
    """Build domain tools bound to `PgOssStore`."""

    @tool(risk_level=RiskLevel.READ)
    async def bi_query(region: str, quarter: str, verbose: bool = False) -> str:
        """按大区及季度查询差旅超标数据；默认 verbose=False 已返回完整汇总、分月明细与费用构成，仅在用户明确要求逐条原始流水明细时再传 verbose=True。"""
        if verbose:
            return "\n".join(
                f"ROW#{i:03} | {region} {quarter} | E{i:04} | 超标 ¥{1200 + i * 37}" for i in range(1, 95)
            )
        return (
            f"{region}区 {quarter} 差旅超标共 12 笔，合计 ¥82,000，最高单笔 ¥14,200。\n"
            f"分月明细：第1月 3 笔 ¥21,000；第2月 4 笔 ¥28,500；第3月 5 笔 ¥32,500。\n"
            f"费用构成：住宿超标 ¥46,000（56.1%）、机票超标 ¥24,000（29.3%）、餐饮交通 ¥12,000（14.6%）。"
        )

    @tool(risk_level=RiskLevel.HIGH_RISK)
    async def submit_approval(
        title: str, amount: float, region: str, ctx: ExecutionContext
    ) -> Dict[str, Any]:
        """向 OA 提交超标专项审批（高危写操作：仅在用户明确要求发起/提交审批时调用；若用户仅要求分析数据或撰写报告，严禁擅自调用，只可在结论中给出审批建议）。"""
        key = ctx.op_key or canonical_hash(
            [ctx.thread_id, ctx.auth_scope.user_id, ctx.request_id, "submit_approval"]
        )
        rows = store.rows(
            """INSERT INTO oa_approvals(approval_id,thread_id,tenant_id,applicant,op_key,title,region,amount)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,op_key)
               DO UPDATE SET op_key=EXCLUDED.op_key RETURNING approval_id,title,amount::text""",
            (
                uid("APR"),
                ctx.thread_id,
                ctx.auth_scope.tenant_id,
                ctx.auth_scope.user_id,
                key,
                title,
                region,
                amount,
            ),
        )
        if approval_writes is not None:
            approval_writes.append({**rows[0], "op_key": key})
        return rows[0]

    @tool(risk_level=RiskLevel.READ)
    async def flaky_lookup(id: str) -> str:
        """演示重复查询熔断"""
        return "查询暂不可用。"

    return [bi_query, submit_approval, flaky_lookup]
