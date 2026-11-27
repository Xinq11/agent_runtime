"""Regression test suite for production bugs & hardening items (P0 + P1).

Covers:
  - Bug 1: SSE disconnect / unconsumed queue must NEVER deadlock worker or strand run in `running`
  - Bug 2: `assemble_before_model` atomic tool-group contiguous slicing (no orphaned `tool_calls`)
  - Bug 3: Step-wise atomic message persistence on mid-turn abort / error (preserving tool side-effects
           and preventing duplicate history replay inside multi-step turns)
  - Gateway JSON Schema parameter type validation (`string`, `integer`, `number`, `boolean`, `array`, `object`)
  - `RunService.is_cancelled` in-process fast path without per-guard DB polling
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Sequence
import unittest

from agent_runtime import (
    AgentRuntimeApp,
    AuthScope,
    ExecutionContext,
    Message,
    RiskLevel,
    SSEEvent,
    ToolCall,
    tool,
)
from agent_runtime.framework.context.memory import ContextBudget, assemble_before_model
from agent_runtime.framework.core.config import publish_sse, sse_meta, sse_sink, uid
from agent_runtime.infra.pg.postgres import PgOssStore

try:
    from tests._pg_harness import get_test_store, reset_test_db
except ImportError:
    from _pg_harness import get_test_store, reset_test_db


def _assert_valid_tool_call_pairing(tc: unittest.TestCase, messages: Sequence[Message]) -> None:
    """Assert OpenAI / DeepSeek strict tool-call / tool-result pairing & contiguity invariants."""
    idx = 0
    n = len(messages)
    while idx < n:
        msg = messages[idx]
        if msg.role == "assistant" and msg.tool_calls:
            expected_ids = [c.id for c in msg.tool_calls]
            idx += 1
            actual_ids: List[str] = []
            while idx < n and messages[idx].role == "tool":
                actual_ids.append(messages[idx].tool_call_id or "")
                idx += 1
            tc.assertEqual(
                set(actual_ids),
                set(expected_ids),
                f"Assistant tool_calls {expected_ids} must be immediately followed by matching tool results, got {actual_ids}",
            )
            tc.assertEqual(len(actual_ids), len(expected_ids))
        elif msg.role == "tool":
            tc.fail(f"Orphaned tool message at index {idx} with tool_call_id={msg.tool_call_id}")
        else:
            idx += 1


class TestProductionRegressions(unittest.TestCase):
    def setUp(self):
        reset_test_db()

    def test_bug1_publish_sse_never_blocks_indefinitely_when_consumer_stalls_or_closes(self):
        """Bug 1a: `publish_sse` must not deadlock forever when consumer stops draining queue."""

        async def _run():
            q: asyncio.Queue = asyncio.Queue(8)
            tok_sink = sse_sink.set(q)
            tok_meta = sse_meta.set({"run_id": "run_stall_1", "seq": 0})
            try:
                async def _burst():
                    for i in range(100):
                        await publish_sse(SSEEvent("token", {"text": f"chunk-{i}"}))

                # Even with nobody reading `q`, publishing 100 events must resolve cleanly within a short bound
                await asyncio.wait_for(_burst(), timeout=3.0)
            finally:
                sse_meta.reset(tok_meta)
                sse_sink.reset(tok_sink)

        asyncio.run(_run())

    def test_bug1_chat_sse_client_disconnect_completes_run_without_409_lockout(self):
        """Bug 1b: Disconnecting SSE stream mid-flight marks sink closed, lets worker finish, and never locks thread."""
        try:
            from agent_runtime.api.chat_stream import chat_sse
            from agent_runtime.api.schemas import ChatRequest
        except ImportError:
            self.skipTest("FastAPI not installed in offline host environment")

        class ManyTokensModel:
            def __init__(self) -> None:
                self.config = SimpleNamespace(
                    api_key="sk-test",
                    model="deepseek-chat",
                    effective="deepseek",
                    public=lambda: {"model": "deepseek-chat", "mode": "deepseek", "has_api_key": True},
                )

            async def generate(self, *, messages, tool_specs, ctx):
                # Emit > 256 SSE token events to overflow default Queue(256) if consumer left
                for i in range(400):
                    await ctx.emit("token", {"text": f"t{i} "})
                return Message("assistant", "完整长回答已生成")

        async def _run():
            store = get_test_store()
            model = ManyTokensModel()
            runtime = AgentRuntimeApp(model=model, store=store)

            fake_app = SimpleNamespace(
                state=SimpleNamespace(
                    runtime=runtime,
                    store=store,
                    graph=runtime.graph,
                    obs=runtime.obs,
                    model=model,
                )
            )
            disconnected = False

            async def _is_disconnected() -> bool:
                return disconnected

            fake_req = SimpleNamespace(
                app=fake_app,
                headers={"X-User-Id": "u_alice", "X-User-Roles": "employee"},
                is_disconnected=_is_disconnected,
            )

            resp = await chat_sse(fake_req, ChatRequest(message="请输出一篇超长报告", thread_id="th_sse_disc"))
            # Read just the first chunk, then simulate browser tab close / refresh
            aiter = resp.body_iterator.__aiter__()
            first_chunk = await asyncio.wait_for(aiter.__anext__(), timeout=2.0)
            self.assertIn("event:", first_chunk)
            disconnected = True
            await resp.body_iterator.aclose()

            # Background worker must finish cleanly (not hang on Queue.put) and transition run to `completed`
            for _ in range(50):
                active = runtime.run_service.get_active_run_for_thread(
                    AuthScope("u_alice", ("employee",)), "th_sse_disc"
                )
                if active is None:
                    break
                await asyncio.sleep(0.05)

            self.assertIsNone(
                runtime.run_service.get_active_run_for_thread(
                    AuthScope("u_alice", ("employee",)), "th_sse_disc"
                ),
                "Thread still has an active running/paused run after client disconnect!",
            )

            # Subsequent turn on the exact same thread MUST succeed (no 409 RunConflictError)
            follow = await runtime.run_turn(
                "刷新页面后的下一轮提问",
                thread_id="th_sse_disc",
                scope=AuthScope("u_alice", ("employee",)),
            )
            self.assertEqual(follow["final_answer"], "完整长回答已生成")

        asyncio.run(_run())

    def test_bug2_assemble_before_model_atomic_tool_groups_and_contiguous_history(self):
        """Bug 2a: Never drop a large `tool` result while retaining its parent `assistant(tool_calls)` or older turns."""
        msgs = [
            Message("user", "最早的历史提问 q0"),
            Message("assistant", "最早的历史回答 a0"),
            Message("user", "帮我检索全量数据 q1"),
            Message(
                "assistant",
                "",
                tool_calls=[
                    ToolCall("c1", "search_a", {"q": "1"}),
                    ToolCall("c2", "search_b", {"q": "2"}),
                ],
            ),
            Message("tool", "X" * 40000, tool_call_id="c1", name="search_a"),
            Message("tool", "small result 2", tool_call_id="c2", name="search_b"),
            Message("assistant", "基于检索的回答 a1"),
            Message("user", "最新一轮追问 q2"),
        ]
        budget = ContextBudget(max_window_tokens=2048, reserve_output_tokens=400)
        assembled, stats = assemble_before_model(
            system_prompt="你是严谨助手。",
            tool_schemas_text="",
            summary_text="",
            recent_messages=msgs,
            budget=budget,
        )
        non_sys = [m for m in assembled if m.role != "system"]
        _assert_valid_tool_call_pairing(self, non_sys)
        # Must keep the newest messages contiguously (a1, q2) and stop before the oversized tool group;
        # MUST NOT skip over the oversized group to include q0/a0!
        contents = [m.content for m in non_sys]
        self.assertIn("最新一轮追问 q2", contents)
        self.assertIn("基于检索的回答 a1", contents)
        self.assertNotIn("最早的历史提问 q0", contents)
        self.assertGreater(stats["dropped_messages"], 0)

    def test_bug2_oversized_newest_atomic_tool_group_truncates_tool_content_instead_of_orphaning(self):
        """Bug 2b: When the newest (active) tool group itself exceeds budget, keep pair valid & truncate tool body."""
        msgs = [
            Message("user", "旧问题"),
            Message("assistant", "旧回答"),
            Message("user", "当前问题"),
            Message("assistant", "", tool_calls=[ToolCall("c_now", "big_dump", {})]),
            Message("tool", "Z" * 50000, tool_call_id="c_now", name="big_dump"),
        ]
        budget = ContextBudget(max_window_tokens=1200, reserve_output_tokens=300)
        assembled, _ = assemble_before_model(
            system_prompt="SYS",
            tool_schemas_text="",
            summary_text="",
            recent_messages=msgs,
            budget=budget,
        )
        non_sys = [m for m in assembled if m.role != "system"]
        _assert_valid_tool_call_pairing(self, non_sys)
        tool_msgs = [m for m in non_sys if m.role == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(tool_msgs[0].tool_call_id, "c_now")
        self.assertLess(len(tool_msgs[0].content), 50000)

    def test_bug3_mid_turn_abort_preserves_completed_write_tool_history_and_no_duplicate_prompt(self):
        """Bug 3: Aborting after step-1 write tool persists step-1 pair, synthesizes cancelled tool if needed, and avoids dupes."""

        async def _run():
            writes_executed: List[str] = []
            second_step_started = asyncio.Event()
            allow_second_step = asyncio.Event()
            seen_model_inputs: List[List[Message]] = []

            @tool(risk_level=RiskLevel.WRITE)
            async def create_voucher(code: str) -> str:
                """Create an accounting voucher with side effects."""
                writes_executed.append(code)
                return f"凭证 {code} 已成功入账"

            @tool(risk_level=RiskLevel.READ)
            async def slow_audit(code: str) -> str:
                """Slow follow-up audit step during which user aborts."""
                second_step_started.set()
                await allow_second_step.wait()
                return f"凭证 {code} 复核通过"

            class TwoStepScriptedModel:
                def __init__(self) -> None:
                    self.calls = 0

                async def generate(self, *, messages: List[Message], tool_specs, ctx):
                    seen_model_inputs.append(list(messages))
                    self.calls += 1
                    if self.calls == 1:
                        return Message(
                            "assistant",
                            "",
                            tool_calls=[ToolCall("tc_v1", "create_voucher", {"code": "V-100"})],
                        )
                    if self.calls == 2:
                        return Message(
                            "assistant",
                            "",
                            tool_calls=[ToolCall("tc_a1", "slow_audit", {"code": "V-100"})],
                        )
                    return Message("assistant", f"收到历史，已完成凭证列表：{writes_executed}")

            scope = AuthScope("u_alice", ("employee",))
            app = AgentRuntimeApp(
                store=get_test_store(),
                model=TwoStepScriptedModel(),
                tools=[create_voucher, slow_audit],
            )

            ctx = ExecutionContext(
                trace_id=uid("tr"),
                thread_id="th_bug3",
                request_id="req_bug3_1",
                auth_scope=scope,
            )
            turn_task = asyncio.create_task(app.run_turn("帮我生成凭证 V-100 并复核", ctx=ctx))
            await asyncio.wait_for(second_step_started.wait(), timeout=2.0)

            # Verify no duplicate messages were fed into step-2 `before_model` after step-1 persistence!
            step2_msgs = [m for m in seen_model_inputs[1] if m.role != "system"]
            _assert_valid_tool_call_pairing(self, step2_msgs)
            step2_tc_ids = [tc.id for m in step2_msgs if m.role == "assistant" for tc in m.tool_calls]
            self.assertEqual(step2_tc_ids.count("tc_v1"), 1, "Step-1 tool_call was duplicated in step-2 prompt!")

            # Now abort mid-flight while `slow_audit` is running!
            await app.abort_run(scope=scope, thread_id="th_bug3")
            turn_task.cancel()
            res = await turn_task
            self.assertEqual(res["status"], "cancelled")
            self.assertEqual(writes_executed, ["V-100"])

            # Stored session history MUST retain the completed `create_voucher` pair AND valid pairing overall!
            saved = app.store.list_messages("th_bug3")
            _assert_valid_tool_call_pairing(self, saved)
            tool_contents = [m.content for m in saved if m.role == "tool"]
            self.assertTrue(
                any("凭证 V-100 已成功入账" in c for c in tool_contents),
                f"Completed write tool output was lost on abort! saved={saved}",
            )

            # Verify step-2 succeeded cleanly
            res2 = await app.run_turn("刚刚凭证入账了吗？", thread_id="th_bug3", scope=scope)
            self.assertIn("V-100", res2["final_answer"])
            step3_msgs = [m for m in seen_model_inputs[2] if m.role != "system"]
            _assert_valid_tool_call_pairing(self, step3_msgs)

        asyncio.run(_run())

    def test_bug3_abort_mid_batch_after_first_write_tool_succeeds_still_persists_write_side_effect(self):
        """Bug 3b: In a single batch `[write_tool, slow_read_tool]`, aborting during `slow_read_tool`
        MUST NOT lose the already-executed `write_tool` from session history!
        """

        async def _run():
            writes_executed: List[str] = []
            in_second_tool = asyncio.Event()
            never_done = asyncio.Event()

            @tool(risk_level=RiskLevel.WRITE)
            async def post_ledger(entry_id: str) -> str:
                """Write ledger entry with external side effect."""
                writes_executed.append(entry_id)
                return f"分录 {entry_id} 已过账"

            @tool(risk_level=RiskLevel.READ)
            async def slow_reconcile(entry_id: str) -> str:
                """Slow second tool in the SAME batch."""
                in_second_tool.set()
                await never_done.wait()
                return "ok"

            class SameBatchModel:
                def __init__(self) -> None:
                    self.calls = 0

                async def generate(self, *, messages: List[Message], tool_specs, ctx):
                    self.calls += 1
                    if self.calls == 1:
                        return Message(
                            "assistant",
                            "",
                            tool_calls=[
                                ToolCall("tc_b_w1", "post_ledger", {"entry_id": "L-900"}),
                                ToolCall("tc_b_r2", "slow_reconcile", {"entry_id": "L-900"}),
                            ],
                        )
                    return Message("assistant", "done")

            scope = AuthScope("u_alice", ("employee",))
            app = AgentRuntimeApp(
                store=get_test_store(),
                model=SameBatchModel(),
                tools=[post_ledger, slow_reconcile],
            )
            ctx = ExecutionContext(
                trace_id=uid("tr"),
                thread_id="th_bug3_batch",
                request_id="req_bug3_batch",
                auth_scope=scope,
            )
            turn_task = asyncio.create_task(app.run_turn("过账 L-900 并对账", ctx=ctx))
            await asyncio.wait_for(in_second_tool.wait(), timeout=2.0)
            self.assertEqual(writes_executed, ["L-900"])

            await app.abort_run(scope=scope, thread_id="th_bug3_batch")
            turn_task.cancel()
            res = await turn_task
            self.assertEqual(res["status"], "cancelled")

            saved = app.store.list_messages("th_bug3_batch")
            _assert_valid_tool_call_pairing(self, saved)
            tool_contents = [m.content for m in saved if m.role == "tool"]
            self.assertTrue(
                any("分录 L-900 已过账" in c for c in tool_contents),
                f"Write tool in batch was lost when sibling tool was aborted! saved={saved}",
            )

        asyncio.run(_run())

    def test_gateway_validates_json_schema_parameter_types(self):
        """P1: ToolGateway validates argument types against `parameters_schema` and returns `invalid_args`."""

        @tool(risk_level=RiskLevel.READ)
        async def typed_tool(
            name: str,
            count: int,
            ratio: float = 1.5,
            active: bool = True,
            tags: List[str] = None,
            meta: Dict[str, Any] = None,
        ) -> str:
            """Tool with strict parameter types."""
            return f"{name}:{count}:{ratio}:{active}"

        async def _run():
            app = AgentRuntimeApp(store=get_test_store(), tools=[typed_tool])
            ctx = ExecutionContext(
                trace_id=uid("tr"),
                thread_id="th_types",
                request_id=uid("req"),
                auth_scope=AuthScope("u_alice", ("employee",)),
            )

            # 1. Passing string for integer `count` -> `invalid_args` (NOT `tool_error`)
            r1 = await app.gateway.execute_one(
                ToolCall("tc_t1", "typed_tool", {"name": "a", "count": "not_an_int"}), ctx
            )
            self.assertFalse(r1.ok)
            self.assertEqual(r1.error_code, "invalid_args")

            # 2. Passing `bool` for `integer` (in Python `isinstance(True, int)` is True — trap!) -> `invalid_args`
            r2 = await app.gateway.execute_one(
                ToolCall("tc_t2", "typed_tool", {"name": "a", "count": True}), ctx
            )
            self.assertFalse(r2.ok)
            self.assertEqual(r2.error_code, "invalid_args")

            # 3. Built-in `read` passing string `offset="abc"` -> `invalid_args` (not unhandled ValueError `tool_error`)
            r3 = await app.gateway.execute_one(
                ToolCall("tc_t3", "read", {"path": "a.md", "offset": "abc"}), ctx
            )
            self.assertFalse(r3.ok)
            self.assertEqual(r3.error_code, "invalid_args")

            # 4. Valid types (including `int` accepted for `number`) succeed
            r4 = await app.gateway.execute_one(
                ToolCall(
                    "tc_t4",
                    "typed_tool",
                    {"name": "ok", "count": 3, "ratio": 2, "active": False, "tags": ["x"], "meta": {"k": 1}},
                ),
                ctx,
            )
            self.assertTrue(r4.ok)
            self.assertIn("ok:3:2:False", r4.content)

        asyncio.run(_run())

    def test_run_service_is_cancelled_throttles_db_reads_for_active_local_run(self):
        """P1: `RunService.is_cancelled` should not hit `store.get_run` on every single guard check within TTL."""

        class CountingStore(PgOssStore):
            def __init__(self) -> None:
                base = get_test_store()
                super().__init__(base.dsn, oss=base.oss)
                self._pool = base._pool
                self.get_run_calls = 0

            def get_run(self, run_id: str):
                self.get_run_calls += 1
                return super().get_run(run_id)

        async def _run():
            store = CountingStore()
            app = AgentRuntimeApp(store=store)
            scope = AuthScope("u_alice", ("employee",))
            store.create_thread(scope, "th_perf", "perf")
            rec = app.run_service.start_run(
                scope=scope, thread_id="th_perf", trace_id="tr_perf", request_id="req_perf"
            )
            store.get_run_calls = 0
            for _ in range(20):
                self.assertFalse(app.run_service.is_cancelled(rec.run_id))

            # 20 tight back-to-back checks within milliseconds should hit DB at most 1-2 times, not 20 times
            self.assertLessEqual(store.get_run_calls, 2)

            # And in-process `request_abort` flips `_cancel_flags` immediately without waiting for TTL
            app.run_service.request_abort(scope=scope, run_id=rec.run_id)
            self.assertTrue(app.run_service.is_cancelled(rec.run_id))

        asyncio.run(_run())

    def test_dsml_stream_guard_final_step_tool_choice_and_pagination_headers(self):
        from agent_runtime.framework.core.workflow import graph_config
        from agent_runtime.infra.llm.provider import DsmlStreamGuard, strip_dsml_markup

        # 1. Streaming DSML guard handles split markers (full-width & ASCII) without breaking normal `<`
        g1 = DsmlStreamGuard()
        out_chunks = [
            g1.feed("汇总结论：a < b 且正常输出。"),
            g1.feed("\n<"),
            g1.feed("｜DS"),
            g1.feed("ML｜ calls> <｜DSML｜ invoke name=\"read_tool_output\">"),
            g1.feed("</｜DSML｜ calls>"),
            g1.flush(),
        ]
        self.assertTrue(g1.triggered)
        self.assertEqual("".join(out_chunks).strip(), "汇总结论：a < b 且正常输出。")
        self.assertEqual(strip_dsml_markup("ok <|DSML| invoke>bad"), "ok")

        g2 = DsmlStreamGuard()
        self.assertEqual(g2.feed("x <") + g2.flush(), "x <")
        self.assertFalse(g2.triggered)

        # 2. Final step keeps identical tool_specs + passes tool_choice="none" + appends notice on tool msg
        seen_calls = []

        class FinalStepInspectModel:
            async def generate(self, *, messages, tool_specs, ctx, tool_choice=None):
                seen_calls.append(
                    {
                        "messages": list(messages),
                        "tool_names": [s.name for s in tool_specs],
                        "tool_choice": tool_choice,
                    }
                )
                if len(seen_calls) == 1:
                    return Message(
                        "assistant",
                        "",
                        [ToolCall("tc_big", "emit_big", {})],
                    )
                # Even if model still leaks DSML on final step, workflow strips it cleanly
                return Message(
                    "assistant",
                    '基于前文预览先汇报结论。\n<｜DSML｜ calls><｜DSML｜ invoke name="read_tool_output">',
                )

        @tool(risk_level=RiskLevel.READ)
        async def emit_big() -> str:
            return "\n".join(f"LINE#{i:03d} payload_abcdef0123456789" for i in range(1, 260))

        async def _run():
            store = get_test_store()
            app = AgentRuntimeApp(
                model=FinalStepInspectModel(),
                store=store,
                tools=[emit_big],
                max_steps=2,
            )
            self.assertEqual(app.max_steps, 2)
            self.assertGreaterEqual(graph_config("th_dsml", max_steps=15)["recursion_limit"], 64)

            scope = AuthScope("u_alice", ("employee",))
            res = await app.run_turn("拉取大结果", thread_id="th_dsml", scope=scope)
            self.assertEqual(len(seen_calls), 2)
            # Identical tool list kept across Step 1 and Step 2 (Prefix Cache intact)
            self.assertEqual(seen_calls[0]["tool_names"], seen_calls[1]["tool_names"])
            self.assertIsNone(seen_calls[0]["tool_choice"])
            self.assertEqual(seen_calls[1]["tool_choice"], "none")
            # Notice mounted on last tool message (no extra mid-conversation system/user message)
            step2_last = seen_calls[1]["messages"][-1]
            self.assertEqual(step2_last.role, "tool")
            self.assertIn("已达本轮工具调用步数上限", step2_last.content)
            # Truncation hint includes exact range and next offset
            self.assertIn("chars=0..", step2_last.content)
            self.assertIn("limit=4000", step2_last.content)
            # Leaked DSML stripped from final answer
            self.assertEqual(res["final_answer"], "基于前文预览先汇报结论。")
            self.assertNotIn("DSML", res["final_answer"])

            # 3. Verify read_tool_output pagination headers (4000 default, 8000 cap, has_more/next_offset/EOF)
            ref_id = store.rows("SELECT ref_id FROM tool_outputs WHERE thread_id=%s", ("th_dsml",))[0]["ref_id"]
            ctx = ExecutionContext(uid("tr"), "th_dsml", scope, uid("req"))
            page1 = await app.gateway.execute_one(
                ToolCall("tc_p1", "read_tool_output", {"ref_id": ref_id, "offset": 0, "limit": 4000}),
                ctx,
            )
            self.assertTrue(page1.ok)
            self.assertIn("has_more=true next_offset=4000", page1.content)
            self.assertGreater(len(page1.content), 3500)

            page2 = await app.gateway.execute_one(
                ToolCall("tc_p2", "read_tool_output", {"ref_id": ref_id, "offset": 4000, "limit": 8000}),
                ctx,
            )
            self.assertTrue(page2.ok)
            self.assertIn("has_more=false", page2.content)

            page_eof = await app.gateway.execute_one(
                ToolCall("tc_p3", "read_tool_output", {"ref_id": ref_id, "offset": 999999, "limit": 4000}),
                ctx,
            )
            self.assertTrue(page_eof.ok)
            self.assertIn("has_more=false EOF", page_eof.content)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
