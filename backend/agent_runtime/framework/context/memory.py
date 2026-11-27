"""Memory and context-window management (before_model + after_agent).

Implements the exact 4-segment prompt layout (preserving Prefix Cache),
arithmetic token-budget slicing, and cursor-based async compaction from
`agent-memory-architecture-comparison.html`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import os
from typing import Any, List, Sequence, Tuple

from agent_runtime.framework.core.contracts import ExecutionContext, Message, estimate_tokens


@dataclass(frozen=True)
class ContextBudget:
    """Window slicing formula ensuring prompt never overflows model limit."""

    max_window_tokens: int = int(os.getenv("AGENT_MAX_WINDOW_TOKENS", "32768"))
    reserve_output_tokens: int = int(os.getenv("AGENT_RESERVE_OUTPUT_TOKENS", "2048"))
    max_summary_tokens: int = 800
    max_extra_context_tokens: int = 1200
    compact_trigger_messages: int = 8
    keep_recent_messages: int = 4

    @property
    def max_input_tokens(self) -> int:
        return self.max_window_tokens - self.reserve_output_tokens


def _msg_tokens(msg: Message) -> int:
    return estimate_tokens(msg.content) + len(msg.tool_calls) * 32


def _truncate_to_token_budget(text: str, budget_tokens: int) -> str:
    suffix = "\n...[truncated for context budget]"
    if budget_tokens <= estimate_tokens(suffix) + 4:
        return suffix.strip()
    target = budget_tokens - estimate_tokens(suffix)
    low, high = 0, min(len(text), 4 * target + 8)
    while low < high:
        mid = (low + high + 1) // 2
        if estimate_tokens(text[:mid]) <= target:
            low = mid
        else:
            high = mid - 1
    return text[:low] + suffix


def _group_atomic_messages(messages: List[Message]) -> List[List[Message]]:
    """Group each `assistant(tool_calls)` with its immediately following `tool` results atomically."""
    groups: List[List[Message]] = []
    idx = 0
    n = len(messages)
    while idx < n:
        msg = messages[idx]
        if msg.role == "assistant" and msg.tool_calls:
            expected_ids = {tc.id for tc in msg.tool_calls}
            grp = [msg]
            idx += 1
            while idx < n and messages[idx].role == "tool":
                grp.append(messages[idx])
                idx += 1
            actual_ids = {m.tool_call_id for m in grp[1:] if m.tool_call_id}
            # Only emit valid complete pairs so OpenAI/DeepSeek never receive an orphaned tool_call
            if expected_ids.issubset(actual_ids) and len(grp) - 1 == len(msg.tool_calls):
                groups.append(grp)
        elif msg.role == "tool":
            # Drop orphaned leading tool result whose parent assistant message is absent
            idx += 1
        else:
            groups.append([msg])
            idx += 1
    return groups


def _fit_newest_group(group: List[Message], budget_tokens: int) -> List[Message]:
    """Keep the newest atomic group valid even when oversized by truncating its `tool` payloads."""
    tool_indices = [i for i, m in enumerate(group) if m.role == "tool"]
    if not tool_indices:
        return [
            replace(group[0], content=_truncate_to_token_budget(group[0].content, max(64, budget_tokens)))
        ]
    fixed_cost = sum(_msg_tokens(m) for i, m in enumerate(group) if i not in tool_indices)
    avail = max(64, budget_tokens - fixed_cost)
    per_tool = max(32, avail // len(tool_indices))
    fitted: List[Message] = []
    for i, m in enumerate(group):
        if i in tool_indices and estimate_tokens(m.content) > per_tool:
            fitted.append(replace(m, content=_truncate_to_token_budget(m.content, per_tool)))
        else:
            fitted.append(m)
    return fitted


def assemble_before_model(
    *,
    system_prompt: str,
    tool_schemas_text: str = "",
    summary_text: str,
    recent_messages: List[Message],
    budget: ContextBudget,
    extra_contexts: Sequence[str] = (),
) -> Tuple[List[Message], dict]:
    """Build cache-friendly segments with strictly contiguous atomic tool-group trimming."""
    static_block = system_prompt.strip()
    if tool_schemas_text:
        static_block = f"{static_block}\n\n[FROZEN_TOOLS]\n{tool_schemas_text}"

    assembled: List[Message] = [Message(role="system", content=static_block)]
    used_tokens = estimate_tokens(static_block)

    if summary_text.strip():
        sum_msg = Message(
            role="system",
            content=f"[CONVERSATION_SUMMARY]\n{summary_text.strip()}",
        )
        assembled.append(sum_msg)
        used_tokens += estimate_tokens(sum_msg.content)

    # Merge middleware-provided reference blocks under an explicit cap so injections never blow budget
    clean_extras = [b.strip() for b in extra_contexts if isinstance(b, str) and b.strip()]
    extra_truncated = False
    if clean_extras:
        merged_extra = "\n\n".join(clean_extras)
        extra_cap = min(
            max(64, budget.max_extra_context_tokens),
            max(64, (budget.max_input_tokens - used_tokens) // 3),
        )
        if estimate_tokens(merged_extra) > extra_cap:
            merged_extra = _truncate_to_token_budget(merged_extra, extra_cap)
            extra_truncated = True
        extra_msg = Message(role="system", content=merged_extra)
        assembled.append(extra_msg)
        used_tokens += estimate_tokens(extra_msg.content)

    remaining = max(256, budget.max_input_tokens - used_tokens)
    groups = _group_atomic_messages(recent_messages)
    kept_groups_rev: List[List[Message]] = []

    for grp in reversed(groups):
        cost = sum(_msg_tokens(m) for m in grp)
        if not kept_groups_rev:
            if cost > remaining:
                fitted = _fit_newest_group(grp, remaining)
                kept_groups_rev.append(fitted)
                remaining = max(0, remaining - sum(_msg_tokens(m) for m in fitted))
                break
            kept_groups_rev.append(grp)
            remaining -= cost
        else:
            if cost > remaining:
                # Strictly contiguous: stop at the first older group that does not fit
                break
            kept_groups_rev.append(grp)
            remaining -= cost

    kept = [m for grp in reversed(kept_groups_rev) for m in grp]
    dropped_count = max(0, len(recent_messages) - len(kept))

    assembled.extend(kept)
    stats = {
        "input_tokens_est": budget.max_input_tokens - remaining,
        "dropped_messages": dropped_count,
        "fallback_folded": dropped_count > 0,
        "kept_messages": len(kept),
        "extra_context_blocks": len(clean_extras),
        "extra_context_truncated": extra_truncated,
    }
    return assembled, stats


async def compact_after_agent(
    *,
    store: Any,
    ctx: ExecutionContext,
    budget: ContextBudget,
    model_client: Any = None,
) -> dict:
    """Cursor-based compaction: distill old messages into a structured memory summary."""
    old_summary, covered_id = store.get_summary(ctx.thread_id)
    uncompacted = store.list_messages(ctx.thread_id, after_msg_id=covered_id)

    if len(uncompacted) < budget.compact_trigger_messages:
        return {"compacted": False, "covered_msg_id": covered_id}

    split_idx = max(1, len(uncompacted) - budget.keep_recent_messages)
    # Never cut right in the middle of an assistant(tool_calls) -> tool pair
    while split_idx < len(uncompacted) and uncompacted[split_idx].role == "tool":
        split_idx += 1
    to_fold = uncompacted[:split_idx]

    if not to_fold:
        return {"compacted": False, "covered_msg_id": covered_id}

    transcript_lines: List[str] = []
    for m in to_fold:
        text = (m.content or "").strip()
        if not text and m.tool_calls:
            calls_desc = ", ".join(f"{tc.name}({tc.args})" for tc in m.tool_calls)
            text = f"调用工具: {calls_desc}"
        if text:
            role_label = f"{m.role}({m.name})" if m.name else m.role
            transcript_lines.append(f"[{role_label}] {text[:600]}")

    new_summary = ""
    used_llm = False
    # Use LLM compaction when a live configured model_client is supplied
    if model_client is not None:
        cfg = getattr(model_client, "config", None)
        can_call_llm = cfg is not None and bool(getattr(cfg, "api_key", ""))
        if can_call_llm:
            compact_prompt = (
                "你是上下文记忆压缩器。请将「已有历史摘要」与「新增待折叠对话」合并压缩成一份高密度、结构化的中文记忆摘要，"
                "供后续对话作为系统记忆使用。要求：\n"
                "1. 严禁逐条照抄原对话，必须提炼合并去重；\n"
                "2. 保留所有精确数字、日期、审批单号、引用出处和业务结论，丢弃寒暄与工具调用废话；\n"
                "3. 严格按以下 3~4 个小节输出（总字数控制在 250 字以内）：\n"
                "   - **核心目标**：用户要解决什么问题\n"
                "   - **关键事实与数据**：查出的确切数字、制度条款与出处\n"
                "   - **已完成动作**：已查工具/已提交或拒绝的审批结果\n"
                "   - **待跟进事项**：尚未确认或下一步动作（无则省略）"
            )
            user_payload = (
                f"【已有历史摘要】\n{old_summary.strip() or '（无）'}\n\n"
                f"【新增待折叠对话】\n" + "\n".join(transcript_lines)
            )
            try:
                compact_ctx = ctx.derive_subagent(
                    agent_name="compactor",
                    allowed_tools=(),
                    parent_span_id=ctx.parent_span_id or ctx.trace_id,
                )
                resp = await model_client.generate(
                    messages=[
                        Message(role="system", content=compact_prompt),
                        Message(role="user", content=user_payload),
                    ],
                    tool_specs=[],
                    ctx=compact_ctx,
                )
                if resp.content and resp.content.strip():
                    new_summary = resp.content.strip()
                    used_llm = True
            except Exception:
                used_llm = False

    if not new_summary:
        # Deterministic fallback if LLM compaction fails or no API key is attached (e.g. unit tests)
        lines: List[str] = []
        if old_summary.strip():
            lines.append(old_summary.strip())
        for m in to_fold:
            snippet = m.content.replace("\n", " ").strip()[:160]
            if snippet:
                lines.append(f"- [{m.role}] {snippet}")
        new_summary = "\n".join(lines[-12:])

    new_cursor = to_fold[-1].msg_id or covered_id
    store.save_summary(ctx.thread_id, new_summary, new_cursor)
    return {
        "compacted": True,
        "used_llm": used_llm,
        "folded_count": len(to_fold),
        "covered_msg_id": new_cursor,
        "summary_text": new_summary,
    }
