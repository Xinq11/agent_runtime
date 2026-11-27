"""Unified LangGraph StateGraph workflow (ZERO raw SQL — storage-agnostic core engine)."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
import json
import time
from typing import Any, Callable, Dict, Optional

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from agent_runtime.framework.context.compaction import SessionCompactor
from agent_runtime.framework.context.memory import ContextBudget
from agent_runtime.framework.core.config import publish_sse, unpack_message
from agent_runtime.framework.core.contracts import AuthScope, ExecutionContext, Message, SpanRecord, estimate_tokens
from agent_runtime.framework.core.middleware import MiddlewareChain
from agent_runtime.framework.core.model_step import (
    filter_visible_tools,
    invoke_model_step,
    supports_tool_choice,
)
from agent_runtime.framework.core.security import get_policy_rev
from agent_runtime.infra.llm.provider import strip_dsml_markup, wire_spec

DEFAULT_SYSTEM_PROMPT = (
    "你是通用智能助手。必要时先调用工具核实事实再作答。"
    "需要生成文档、报表或结构化文件时，可使用内置 write/read 工具读写工作区文件；"
    "涉及外部业务系统写入必须通过对应工具执行。"
)
_FINAL_STEP_CONVERGENCE_NOTICE = (
    "\n\n[运行时提示：已达本轮工具调用步数上限，当前已禁止继续调用工具。"
    "请严禁输出任何工具调用或 DSML 标记，直接基于以上已获取的数据汇总给出最终中文答复；"
    "若部分分页数据未读完，请基于已有内容作答并如实注明覆盖范围。]"
)


def run_context(
    state: Dict[str, Any],
    *,
    on_event: Optional[Any] = None,
    cancel_checker: Optional[Callable[[str], bool]] = None,
    access_controller: Optional[Any] = None,
) -> ExecutionContext:
    rid = str(state.get("run_id") or "")
    scope = AuthScope(**state["scope"])
    policy_rev = (
        get_policy_rev(access_controller, scope)
        if access_controller is not None
        else int(state.get("policy_rev", 1))
    )
    return ExecutionContext(
        trace_id=state["trace_id"],
        thread_id=state["thread_id"],
        request_id=state["request_id"],
        run_id=rid,
        auth_scope=scope,
        policy_rev=policy_rev,
        on_event=on_event or publish_sse,
        parent_span_id=state["trace_id"] + "_root",
        is_cancelled=(lambda: cancel_checker(rid)) if (cancel_checker and rid) else None,
    )


def build_agent_graph(
    *,
    store: Any,
    obs: Any,
    model: Any,
    gateway: Any,
    checkpointer: Optional[Any] = None,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    middlewares: Optional[MiddlewareChain] = None,
    budget: Optional[ContextBudget] = None,
    compactor: Optional[SessionCompactor] = None,
    max_steps: int = 8,
    on_event: Optional[Any] = None,
    cancel_checker: Optional[Callable[[str], bool]] = None,
    access_controller: Optional[Any] = None,
):
    """Build and compile the single unified LangGraph StateGraph used everywhere."""
    mw = middlewares or MiddlewareChain()
    compactor = compactor or SessionCompactor(store, model, budget or ContextBudget())

    def _guard_cancel(ctx: ExecutionContext) -> None:
        if ctx.check_cancelled():
            raise asyncio.CancelledError("Run cancelled by user")

    async def generation(state: Dict[str, Any]) -> Dict[str, Any]:
        ctx = run_context(
            state,
            on_event=on_event,
            cancel_checker=cancel_checker,
            access_controller=access_controller,
        )
        _guard_cancel(ctx)
        persisted_count = int(state.get("persisted_count", 0))
        pending = [unpack_message(m) for m in state["messages"][persisted_count:]]

        with obs.span(
            trace_id=ctx.trace_id,
            operation_name="before_model",
            parent_span_id=ctx.parent_span_id,
        ) as bm_sp:
            specs = await filter_visible_tools(
                gateway.registry,
                ctx,
                access_controller=access_controller,
                include_subagents=True,
            )

            is_final_step = state["step"] >= max_steps - 1
            sent_specs = specs if not is_final_step or supports_tool_choice(model) else []
            wire_overhead = (
                estimate_tokens(json.dumps([wire_spec(s) for s in sent_specs], ensure_ascii=False))
                if sent_specs
                else 0
            )
            if is_final_step:
                wire_overhead += estimate_tokens(_FINAL_STEP_CONVERGENCE_NOTICE) + 4
            summary, cursor, history = await compactor.load(ctx.thread_id)
            recent = list(history) + list(pending)
            latest_user_text = ""
            last_user_idx = next(
                (i for i in range(len(recent) - 1, -1, -1) if recent[i].role == "user"),
                None,
            )
            if last_user_idx is not None:
                raw_user = recent[last_user_idx].content or ""
                transformed = await mw.run_transform_user_input(raw_user, ctx)
                if transformed != raw_user:
                    recent[last_user_idx] = replace(recent[last_user_idx], content=transformed)
                latest_user_text = (recent[last_user_idx].content or "").strip()
            extra_contexts = (
                await mw.run_extra_context(latest_user_text, ctx) if latest_user_text else []
            )
            inputs, bm_stats = await compactor.prepare(
                ctx,
                system_prompt=system_prompt,
                wire_overhead=wire_overhead,
                summary=summary,
                cursor=cursor,
                recent=recent,
                extras=extra_contexts,
            )
            await mw.run_on_before_model(inputs, ctx)
            bm_sp.attributes.update(
                {
                    "input_tokens_est": bm_stats.get("input_tokens_est", 0),
                    "kept_messages": bm_stats.get("kept_messages", 0),
                    "dropped_messages": bm_stats.get("dropped_messages", 0),
                    "extra_context_blocks": bm_stats.get("extra_context_blocks", 0),
                    "extra_context_truncated": bm_stats.get("extra_context_truncated", False),
                    "summary_cursor": bm_stats["summary_cursor"],
                    "emergency": bm_stats["emergency"],
                }
            )

        _guard_cancel(ctx)
        step_num = state["step"] + 1
        model_cfg = getattr(model, "config", None)
        mode_name = getattr(model_cfg, "effective", "deepseek") if model_cfg else "deepseek"
        model_name = getattr(model_cfg, "model", "deepseek-chat") if model_cfg else "deepseek-chat"

        with obs.span(
            trace_id=ctx.trace_id,
            operation_name=f"model#{step_num}",
            parent_span_id=ctx.parent_span_id,
            attributes={"mode": mode_name, "model": model_name, "step": step_num},
        ) as m_sp:
            msg = await invoke_model_step(
                model=model,
                messages=inputs,
                tool_specs=specs,
                ctx=ctx,
                is_final_step=is_final_step,
                convergence_notice=_FINAL_STEP_CONVERGENCE_NOTICE,
            )
            _guard_cancel(ctx)
            msg = await mw.run_after_model(msg, ctx)
            cleaned = strip_dsml_markup(msg.content or "")
            if cleaned != (msg.content or ""):
                msg = replace(msg, content=cleaned)
            if is_final_step:
                if msg.tool_calls or not (msg.content or "").strip():
                    fallback = (
                        msg.content.strip()
                        if (msg.content or "").strip()
                        else "已达到本轮工具调用步数上限，请基于已有内容继续或缩小查询范围。"
                    )
                    msg = Message("assistant", fallback)
            m_sp.attributes.update(
                {
                    "tool_calls": [c.name for c in msg.tool_calls],
                    "reply_chars": len(msg.content or ""),
                    "final_step_guard": is_final_step,
                }
            )

        obs.log_event(
            "llm_done",
            ctx.trace_id,
            ctx.thread_id,
            step=step_num,
            model=model_name,
            tool_calls=[c.name for c in msg.tool_calls],
        )
        return {
            **state,
            "step": step_num,
            "fixed_tokens": estimate_tokens(system_prompt) + 4 + wire_overhead,
            "messages": [*state["messages"], asdict(msg)],
        }

    async def tools_node(state: Dict[str, Any]) -> Dict[str, Any]:
        ctx = run_context(
            state,
            on_event=on_event,
            cancel_checker=cancel_checker,
            access_controller=access_controller,
        )
        _guard_cancel(ctx)
        counts = dict(state["counts"])
        last_asst = unpack_message(state["messages"][-1])
        persisted_count = int(state.get("persisted_count", 0))
        partial_results = []

        def _to_tool_msg(r: Any) -> Message:
            return Message(
                "tool",
                r.content,
                tool_call_id=r.tool_call_id,
                name=r.tool_name,
                metadata={"ok": r.ok, "ref_id": r.ref_id, "truncated": r.truncated},
            )

        try:
            results = await gateway.execute_batch(
                last_asst.tool_calls, ctx, counts, completed_sink=partial_results
            )
            _guard_cancel(ctx)
        except asyncio.CancelledError:
            if partial_results:
                by_id = {r.tool_call_id: r for r in partial_results}
                aborted_tool_msgs = [
                    _to_tool_msg(by_id[tc.id])
                    if tc.id in by_id
                    else Message(
                        "tool",
                        "[cancelled] 工具执行已中止",
                        tool_call_id=tc.id,
                        name=tc.name,
                        metadata={"ok": False, "cancelled": True},
                    )
                    for tc in last_asst.tool_calls
                ]
                unpersisted_objs = [
                    unpack_message(m) for m in state["messages"][persisted_count:]
                ] + aborted_tool_msgs
                store.save_turn_messages(
                    ctx.thread_id,
                    ctx.trace_id,
                    unpersisted_objs,
                    step_key=f"tools:{state['step']}:aborted",
                )
            raise

        tool_msgs_obj = [_to_tool_msg(r) for r in results]
        new_state_msgs = state["messages"] + [asdict(m) for m in tool_msgs_obj]
        unpersisted_objs = [unpack_message(m) for m in new_state_msgs[persisted_count:]]
        await asyncio.to_thread(
            store.save_turn_messages,
            ctx.thread_id,
            ctx.trace_id,
            unpersisted_objs,
            step_key=f"tools:{state['step']}",
        )
        return {
            **state,
            "messages": new_state_msgs,
            "counts": counts,
            "persisted_count": len(new_state_msgs),
        }

    async def finish(state: Dict[str, Any]) -> Dict[str, Any]:
        ctx = run_context(
            state,
            on_event=on_event,
            cancel_checker=cancel_checker,
            access_controller=access_controller,
        )
        _guard_cancel(ctx)
        with obs.span(
            trace_id=ctx.trace_id,
            operation_name="after_agent",
            parent_span_id=ctx.parent_span_id,
        ):
            persisted_count = int(state.get("persisted_count", 0))
            remaining_msgs = [unpack_message(m) for m in state["messages"][persisted_count:]]
            if remaining_msgs:
                await asyncio.to_thread(
                    store.save_turn_messages,
                    ctx.thread_id,
                    ctx.trace_id,
                    remaining_msgs,
                    step_key="finish",
                )
            compactor.schedule(ctx, state.get("fixed_tokens", estimate_tokens(system_prompt) + 4))

        final_answer = state["messages"][-1]["content"] if state["messages"] else ""
        await mw.run_after_turn(final_answer, ctx)
        obs.log_event("turn_done", ctx.trace_id, ctx.thread_id)
        return {**state, "final_answer": final_answer, "persisted_count": len(state["messages"])}

    graph = StateGraph(dict)
    graph.add_node("model", generation)
    graph.add_node("tools", tools_node)
    graph.add_node("finish", finish)
    graph.add_edge(START, "model")
    graph.add_conditional_edges(
        "model",
        lambda s: "tools" if s["messages"] and s["messages"][-1].get("tool_calls") else "finish",
        {"tools": "tools", "finish": "finish"},
    )
    graph.add_edge("tools", "model")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=checkpointer or InMemorySaver())


async def invoke_phase(graph: Any, store: Any, payload: Any, cfg: dict, trace: str, start_ms: int) -> dict:
    """Run one graph invocation phase wrapped in a single root `chat` span."""
    root = SpanRecord(trace, trace + "_root", None, "agent-runtime", "chat", start_ms, 0)
    try:
        result = await graph.ainvoke(payload, cfg)
        root.status = "paused" if result.get("__interrupt__") else "ok"
        return result
    except asyncio.CancelledError:
        root.status = "cancelled"
        raise
    except BaseException:
        root.status = "error"
        raise
    finally:
        root.duration_ms = max(1, int(time.time() * 1000) - start_ms)
        await asyncio.to_thread(store.save_span, root)


def graph_config(checkpoint_id: str, *, max_steps: Optional[int] = None) -> dict:
    """Build LangGraph configurable dict keyed by `run_id` so cancelled interrupts never block new turns."""
    steps = max(1, int(max_steps)) if max_steps is not None else 15
    return {"configurable": {"thread_id": checkpoint_id}, "recursion_limit": max(64, 2 * steps + 10)}


def extract_interrupt_tickets(raw_interrupts: Any) -> list:
    return [getattr(item, "value", item) for item in (raw_interrupts or [])]


def pending_tickets(snapshot: Any) -> list:
    return [i.value for task in getattr(snapshot, "tasks", []) for i in getattr(task, "interrupts", [])]
