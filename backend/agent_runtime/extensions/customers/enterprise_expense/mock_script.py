"""Deterministic demo script for the `enterprise_expense` customer pack (used in mock/offline mode)."""

from __future__ import annotations

from typing import List, Set
import uuid

from agent_runtime.framework.core.contracts import ExecutionContext, Message, ToolCall


def route_enterprise_expense_mock(
    ctx: ExecutionContext,
    last_user: str,
    tool_msgs: List[Message],
    tool_names: Set[str],
) -> Message:
    """Scripted assistant turns driving expense BI, CRAG, approval, truncation, and loop demos."""
    if ctx.agent_name == "bi_analyst":
        if not tool_msgs and "bi_query" in tool_names:
            return Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(
                        id=f"tc_{uuid.uuid4().hex[:8]}",
                        name="bi_query",
                        args={"region": "华东", "quarter": "Q3"},
                    )
                ],
            )
        summary = tool_msgs[-1].content[:360] if tool_msgs else "无数据"
        return Message(
            role="assistant",
            content=f"【BI 子代理结论】华东区 Q3 差旅超标合计 ¥82,000（共 12 笔，最高单笔超标 ¥14,200）。摘要：{summary}",
        )

    if "LOOP_TEST" in last_user:
        if any("loop_broken" in m.content or "连续" in m.content for m in tool_msgs):
            return Message(role="assistant", content="工具连续无进展已触发熔断，停止重试。")
        return Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(
                    id=f"tc_{uuid.uuid4().hex[:8]}",
                    name="flaky_lookup",
                    args={"id": "X-999"},
                )
            ],
        )

    if "超标" in last_user and "审批" in last_user:
        called_tools = {m.name for m in tool_msgs}
        if not tool_msgs:
            calls = []
            if "call_bi_analyst" in tool_names:
                calls.append(
                    ToolCall(
                        id=f"tc_{uuid.uuid4().hex[:8]}",
                        name="call_bi_analyst",
                        args={"goal": "统计华东区 Q3 差旅超标总额和笔数"},
                    )
                )
            if "search_knowledge" in tool_names:
                calls.append(
                    ToolCall(
                        id=f"tc_{uuid.uuid4().hex[:8]}",
                        name="search_knowledge",
                        args={"query": "2026 差旅住宿报销标准与超标审批红线"},
                    )
                )
            return Message(role="assistant", content="", tool_calls=calls)

        if "submit_approval" not in called_tools and "submit_approval" in tool_names:
            return Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(
                        id=f"tc_{uuid.uuid4().hex[:8]}",
                        name="submit_approval",
                        args={
                            "title": "华东区 Q3 差旅超标整改审批",
                            "amount": 82000,
                            "region": "华东",
                        },
                    )
                ],
            )

        approval_msg = next((m.content for m in tool_msgs if m.name == "submit_approval"), "")
        if "approval_rejected" in approval_msg:
            return Message(
                role="assistant",
                content="该专项审批已被审批人拒绝，本次操作已中止，未向 OA 系统写入任何记录。",
            )
        rag_msg = next((m.content for m in tool_msgs if m.name == "search_knowledge"), "")
        citation = "[1]" if "[1]" in rag_msg else ""
        return Message(
            role="assistant",
            content=(
                f"已完成华东区 Q3 差旅超标核查与审批流转：\n"
                f"1. 超标金额：¥82,000（BI 子代理并行汇总）；\n"
                f"2. 制度依据：一线城市住宿上限 650 元/晚，超标 10% 需发起专项审批 {citation}；\n"
                f"3. 审批回执：{approval_msg}"
            ),
        )

    if "拉取全量明细" in last_user or "全量明细" in last_user:
        if not tool_msgs and "bi_query" in tool_names:
            return Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(
                        id=f"tc_{uuid.uuid4().hex[:8]}",
                        name="bi_query",
                        args={"region": "华东", "quarter": "Q3", "verbose": True},
                    )
                ],
            )
        if len(tool_msgs) == 1 and "ref_id=" in tool_msgs[0].content and "read_tool_output" in tool_names:
            ref_part = tool_msgs[0].content.split("ref_id=")[1].split()[0].strip("])|,")
            return Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(
                        id=f"tc_{uuid.uuid4().hex[:8]}",
                        name="read_tool_output",
                        args={"ref_id": ref_part, "offset": 0, "limit": 300},
                    )
                ],
            )
        return Message(
            role="assistant",
            content=f"已按 ref_id 安全分页读取明细：{tool_msgs[-1].content[:180]}",
        )

    if any(k in last_user for k in ("制度", "外星人", "高管", "奖金", "薪酬", "差旅", "住宿", "报销")):
        if not tool_msgs and "search_knowledge" in tool_names:
            return Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(
                        id=f"tc_{uuid.uuid4().hex[:8]}",
                        name="search_knowledge",
                        args={"query": last_user},
                    )
                ],
            )
        if tool_msgs and ("[NO_HIT]" in tool_msgs[-1].content or "[LOW_CONFIDENCE]" in tool_msgs[-1].content):
            return Message(
                role="assistant",
                content="知识库未收录该主题，为避免误导暂不作答。",
            )
        return Message(
            role="assistant",
            content=f"依据内部知识库 [1]：{tool_msgs[-1].content[:260]}",
        )

    return Message(
        role="assistant",
        content=(
            f"已收到你的消息：「{last_user}」。\n"
            "（当前处于本地零依赖 Mock LLM 模式：可点击上方快捷场景体验全链路，或在右上角配置 DeepSeek / OpenAI 模型。）"
        ),
    )
