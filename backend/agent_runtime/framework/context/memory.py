"""Memory and context-window management (before_model + after_agent).

Implements the exact 4-segment prompt layout (preserving Prefix Cache),
arithmetic token-budget slicing, and cursor-based async compaction from
`agent-memory-architecture-comparison.html`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from typing import List, Sequence, Tuple

from agent_runtime.framework.core.contracts import Message, estimate_tokens


class ContextWindowExceeded(ValueError):
    """The current input cannot fit even with all older context removed."""


@dataclass(frozen=True)
class ContextBudget:
    """Window slicing formula ensuring prompt never overflows model limit."""

    max_window_tokens: int = int(os.getenv("AGENT_MAX_WINDOW_TOKENS", "32768"))
    reserve_output_tokens: int = int(os.getenv("AGENT_RESERVE_OUTPUT_TOKENS", "2048"))
    max_summary_tokens: int = 800
    max_extra_context_tokens: int = 1200
    compact_trigger_ratio: float = 0.7
    keep_tail_ratio: float = 0.2

    def history_budget(self, fixed_tokens: int) -> int:
        # The design reserves 10k for the next question in a 128k window.
        user_buffer = self.max_window_tokens * 10 // 128
        return max(0, self.max_input_tokens - fixed_tokens - self.max_extra_context_tokens - user_buffer)

    @property
    def max_input_tokens(self) -> int:
        return self.max_window_tokens - self.reserve_output_tokens


def _msg_tokens(msg: Message) -> int:
    cost = 4 + estimate_tokens(msg.content) + estimate_tokens(msg.tool_call_id or "")
    for call in msg.tool_calls:
        cost += 8 + estimate_tokens(call.name + call.id + json.dumps(call.args, ensure_ascii=False))
    if msg.tool_calls:
        cost += estimate_tokens(msg.metadata.get("reasoning_content") or "")
    return cost


def _truncate_to_token_budget(text: str, budget_tokens: int) -> str:
    if budget_tokens <= 0:
        return ""
    if estimate_tokens(text) <= budget_tokens:
        return text
    suffix = "\n...[truncated for context budget]"
    if estimate_tokens(suffix) > budget_tokens:
        suffix = ""
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if estimate_tokens(text[:mid] + suffix) <= budget_tokens:
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
    tool_count = sum(m.role == "tool" for m in group)
    base = sum(_msg_tokens(replace(m, content="")) if m.role == "tool" else _msg_tokens(m) for m in group)
    if not tool_count or base > budget_tokens:
        raise ContextWindowExceeded("本轮输入超过模型窗口，请分段发送或改用附件。")
    per_tool = (budget_tokens - base) // tool_count
    return [replace(m, content=_truncate_to_token_budget(m.content, per_tool))
            if m.role == "tool" else m for m in group]


def assemble_before_model(
    *,
    system_prompt: str,
    tool_schemas_text: str = "",
    summary_text: str,
    recent_messages: List[Message],
    budget: ContextBudget,
    extra_contexts: Sequence[str] = (),
    wire_overhead: int = 0,
) -> Tuple[List[Message], dict]:
    """Build cache-friendly segments with strictly contiguous atomic tool-group trimming."""
    static_block = system_prompt.strip()
    if tool_schemas_text:
        static_block = f"{static_block}\n\n[FROZEN_TOOLS]\n{tool_schemas_text}"

    assembled: List[Message] = [Message(role="system", content=static_block)]
    used_tokens = _msg_tokens(assembled[0]) + wire_overhead

    if summary_text.strip():
        sum_msg = Message(
            role="system",
            content=f"[CONVERSATION_SUMMARY]\n{summary_text.strip()}",
        )
        assembled.append(sum_msg)
        used_tokens += _msg_tokens(sum_msg)

    # Merge middleware-provided reference blocks under an explicit cap so injections never blow budget
    clean_extras = [b.strip() for b in extra_contexts if isinstance(b, str) and b.strip()]
    extra_truncated = False
    if clean_extras:
        merged_extra = "\n\n".join(clean_extras)
        extra_cap = min(
            budget.max_extra_context_tokens,
            max(64, (budget.max_input_tokens - used_tokens) // 3),
        )
        if estimate_tokens(merged_extra) > extra_cap:
            merged_extra = _truncate_to_token_budget(merged_extra, extra_cap)
            extra_truncated = True
        extra_msg = Message(role="system", content=merged_extra)
        used_tokens += _msg_tokens(extra_msg)

    remaining = budget.max_input_tokens - used_tokens
    latest_user = next((m for m in reversed(recent_messages) if m.role == "user"), None)
    if latest_user is not None:
        remaining -= _msg_tokens(latest_user)
    if remaining < 0:
        raise ContextWindowExceeded("本轮输入超过模型窗口，请分段发送或改用附件。")
    groups = _group_atomic_messages(recent_messages)
    kept_groups_rev: List[List[Message]] = []

    for grp in reversed(groups):
        if grp[0] is latest_user:
            kept_groups_rev.append(grp)
            continue
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
    if latest_user is not None and all(m is not latest_user for m in kept):
        kept.insert(0, latest_user)
    dropped_count = max(0, len(recent_messages) - len(kept))

    if clean_extras:
        # Dynamic references follow the cacheable history, just before this turn.
        cut = next((i for i, m in enumerate(kept) if m is latest_user), len(kept))
        kept = [*kept[:cut], extra_msg, *kept[cut:]]
    assembled.extend(kept)
    stats = {
        "input_tokens_est": budget.max_input_tokens - remaining,
        "dropped_messages": dropped_count,
        "fallback_folded": dropped_count > 0,
        "kept_messages": len(recent_messages) - dropped_count,
        "extra_context_blocks": len(clean_extras),
        "extra_context_truncated": extra_truncated,
    }
    return assembled, stats
