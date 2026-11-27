"""Integration coverage for the independently installed expense example."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
import unittest

from agent_runtime import (
    AgentMiddleware, AgentRuntimeApp, ApprovalDecision, AuthScope, ExecutionContext,
    RagExtension, SkillsExtension, ToolCall,
)
from agent_runtime.framework.context.memory import ContextBudget
from agent_runtime.framework.core.config import uid
from agent_runtime.framework.core.contracts import Message, canonical_hash
from agent_runtime.framework.core.runs import RunStateError
from enterprise_expense import EnterpriseExpensePlugin
from tests._pg_harness import EchoTestModel, get_test_store, reset_test_db


def _expense_test_router(ctx, last_user, tool_msgs, tool_names):
    """Test-only scripted LLM router for deterministic expense integration tests."""
    if ctx.agent_name == "bi_analyst":
        if not tool_msgs and "bi_query" in tool_names:
            return Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id=uid("tc"), name="bi_query", args={"region": "华东", "quarter": "Q3"})],
            )
        summary = tool_msgs[-1].content[:360] if tool_msgs else "无数据"
        return Message(role="assistant", content=f"【BI 子代理结论】¥82,000。摘要：{summary}")

    if "LOOP_TEST" in last_user:
        if any("loop_broken" in m.content or "连续" in m.content for m in tool_msgs):
            return Message(role="assistant", content="工具连续无进展已触发熔断，停止重试。")
        return Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id=uid("tc"), name="flaky_lookup", args={"id": "X-999"})],
        )

    if "超标" in last_user and "审批" in last_user:
        called = {m.name for m in tool_msgs}
        if not tool_msgs:
            calls = []
            if "call_bi_analyst" in tool_names:
                calls.append(ToolCall(id=uid("tc"), name="call_bi_analyst", args={"goal": "统计华东区 Q3 超标"}))
            if "search_knowledge" in tool_names:
                calls.append(ToolCall(id=uid("tc"), name="search_knowledge", args={"query": "差旅住宿标准"}))
            return Message(role="assistant", content="", tool_calls=calls)
        if "submit_approval" not in called and "submit_approval" in tool_names:
            return Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(
                        id=uid("tc"),
                        name="submit_approval",
                        args={"title": "华东区 Q3 差旅超标整改审批", "amount": 82000, "region": "华东"},
                    )
                ],
            )
        approval_msg = next((m.content for m in tool_msgs if m.name == "submit_approval"), "")
        return Message(role="assistant", content=f"已完成核查与审批：{approval_msg}")

    if "全量明细" in last_user:
        if not tool_msgs and "bi_query" in tool_names:
            return Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(id=uid("tc"), name="bi_query", args={"region": "华东", "quarter": "Q3", "verbose": True})
                ],
            )
        if len(tool_msgs) == 1 and "ref_id=" in tool_msgs[0].content and "read_tool_output" in tool_names:
            ref_part = tool_msgs[0].content.split("ref_id=")[1].split()[0].strip("])|,")
            return Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id=uid("tc"), name="read_tool_output", args={"ref_id": ref_part, "offset": 0, "limit": 300})],
            )
        return Message(role="assistant", content=f"已分页读取：{tool_msgs[-1].content[:180]}")

    return Message(role="assistant", content=f"已收到：{last_user}")


def build_full_app(
    *,
    budget: Optional[ContextBudget] = None,
    loop_limit: int = 3,
    middlewares: Optional[List[AgentMiddleware]] = None,
):
    approval_writes: List[Dict[str, Any]] = []
    app = AgentRuntimeApp(
        model=EchoTestModel(router=_expense_test_router),
        store=get_test_store(),
        extensions=[
            RagExtension(),
            SkillsExtension(),
            EnterpriseExpensePlugin(approval_writes=approval_writes),
        ],
        budget=budget,
        loop_limit=loop_limit,
        middlewares=middlewares or (),
    )
    app.bootstrap_extensions()
    return app, approval_writes


class TestExpensePlugin(unittest.TestCase):
    def setUp(self):
        reset_test_db()

    def test_01_high_risk_approval_interrupt_resume_and_idempotency(self):
        async def _run():
            app, approval_writes = build_full_app()
            scope = AuthScope(tenant_id="t_acme", user_id="u_alice", roles=("employee",))
            ctx = ExecutionContext(
                trace_id=uid("tr"),
                thread_id="th_test_1",
                request_id="req_t1",
                auth_scope=scope,
            )
            paused = await app.run_turn("查华东区 Q3 差旅超标并起草审批", ctx=ctx)
            self.assertIn("__interrupt__", paused)
            self.assertEqual(len(approval_writes), 0)

            ticket = paused["__interrupt__"][0]
            review_id = ticket["review_id"]
            op_key = ticket["op_key"]

            done = await app.resume_approval(
                thread_id="th_test_1",
                decision=ApprovalDecision(review_id=review_id, approved=True, approver_id="mgr_bob"),
                scope=scope,
            )
            self.assertNotIn("__interrupt__", done)
            self.assertIn("APR_", done["final_answer"])
            self.assertEqual(len(approval_writes), 1)

            same_args_hash = canonical_hash(
                {"title": "华东区 Q3 差旅超标整改审批", "amount": 82000, "region": "华东"}
            )
            acquired, rec, err = app.store.authorize_and_claim_run(
                ctx, op_key, "submit_approval", same_args_hash, review_id
            )
            self.assertFalse(acquired)
            self.assertIsNone(err)
            self.assertEqual(rec.status, "done")

            tampered_hash = canonical_hash(
                {"title": "篡改金额", "amount": 999999, "region": "华东"}
            )
            acquired2, _rec2, err2 = app.store.authorize_and_claim_run(
                ctx, op_key, "submit_approval", tampered_hash, review_id
            )
            self.assertFalse(acquired2)
            self.assertEqual(err2, "op_key_conflict")
            self.assertEqual(len(approval_writes), 1)

            spans = app.obs.store.list_spans(ctx.trace_id)
            op_names = [s.operation_name for s in spans]
            self.assertTrue(any(n == "chat" for n in op_names))
            self.assertIn("subagent:bi_analyst", op_names)
            self.assertIn("tool:call_bi_analyst", op_names)
            self.assertIn("tool:bi_query", op_names)
            self.assertTrue(any(n == "tool:search_knowledge" for n in op_names))

        asyncio.run(_run())

    def test_legacy_oa_receipts_survive_upgrade_and_follow_thread_deletion(self):
        app, _ = build_full_app()
        store = app.store
        scope = AuthScope("t_acme", "u_alice", ("employee",))
        store.create_thread(scope, "legacy_thread", "Historical approval")
        with store.transaction() as cur:
            cur.execute(
                "ALTER TABLE oa_approvals DROP CONSTRAINT oa_approvals_thread_id_fkey, "
                "ADD FOREIGN KEY (thread_id) REFERENCES agent_threads"
            )
            cur.execute(
                "INSERT INTO oa_approvals(approval_id,thread_id,tenant_id,applicant,op_key,title,region,amount) "
                "VALUES ('receipt','legacy_thread','t_acme','u_alice','old_key','Report','HQ',5)"
            )
        store.bootstrap()
        self.assertEqual(store.rows("SELECT approval_id FROM oa_approvals"), [{"approval_id": "receipt"}])
        self.assertTrue(store.delete_thread(scope, "legacy_thread"))
        self.assertEqual(store.rows("SELECT approval_id FROM oa_approvals"), [])

    def test_02_truncation_spill_and_cross_tenant_acl_denial(self):
        async def _run():
            app, _writes = build_full_app()
            alice_scope = AuthScope(tenant_id="t_acme", user_id="u_alice", roles=("employee",))
            ctx = ExecutionContext(
                trace_id=uid("tr"),
                thread_id="th_test_2",
                request_id="req_t2",
                auth_scope=alice_scope,
            )
            res = await app.run_turn("帮我拉取全量明细", ctx=ctx)
            self.assertIn("ROW#001", res["final_answer"])

            ref_rows = app.store.rows("SELECT ref_id FROM tool_outputs WHERE thread_id=%s", ("th_test_2",))
            ref_ids = [r["ref_id"] for r in ref_rows]
            self.assertEqual(len(ref_ids), 1)
            ref_id = ref_ids[0]

            mallory_ctx = ExecutionContext(
                trace_id=uid("tr"),
                thread_id="th_other",
                request_id="req_other",
                auth_scope=AuthScope(tenant_id="t_evil", user_id="u_mallory", roles=("employee",)),
            )
            page, err = app.store.read_tool_output_page(mallory_ctx, ref_id)
            self.assertIsNone(page)
            self.assertEqual(err, "output_not_found")

        asyncio.run(_run())

    def test_05_subagent_max_depth_and_readonly_isolation(self):
        async def _run():
            app, _writes = build_full_app()
            scope = AuthScope(
                tenant_id="t_acme",
                user_id="u_alice",
                roles=("employee",),
                allowed_tools=("call_bi_analyst", "bi_query"),
            )
            nested_ctx = ExecutionContext(
                trace_id=uid("tr"),
                thread_id="th_depth",
                request_id="req_depth",
                auth_scope=scope,
                depth=1,
                max_depth=1,
            )
            res = await app.gateway.execute_one(
                ToolCall(id="tc_d1", name="call_bi_analyst", args={"goal": "递归派活"}),
                nested_ctx,
            )
            self.assertFalse(res.ok)
            self.assertEqual(res.error_code, "max_depth")

            sub_ctx = nested_ctx.derive_subagent(
                agent_name="bi_analyst",
                allowed_tools=("bi_query", "submit_approval"),
                parent_span_id="sp_1",
            )
            self.assertFalse(sub_ctx.auth_scope.allow_write)

        asyncio.run(_run())

    def test_06_loop_breaker_and_cursor_compaction(self):
        async def _run():
            app, _writes = build_full_app(
                budget=ContextBudget(max_window_tokens=12000, reserve_output_tokens=1000),
                loop_limit=2,
            )
            ctx = ExecutionContext(
                trace_id=uid("tr"),
                thread_id="th_loop",
                request_id="req_loop",
                auth_scope=AuthScope(tenant_id="t_acme", user_id="u1", roles=("employee",)),
            )
            app.store.create_thread(ctx.auth_scope, ctx.thread_id, "历史对话")
            app.store.append_message(ctx.thread_id, Message("user", "之前确认了哪些事项？"))
            old_end = app.store.append_message(ctx.thread_id, Message("assistant", "记录" * 2400))
            res = await app.run_turn("LOOP_TEST", ctx=ctx)
            self.assertIn("熔断", res["final_answer"])

            await app.compactor.drain()
            summary, cursor = app.store.get_summary("th_loop")
            self.assertEqual(cursor, old_end)
            self.assertTrue(len(summary) > 0)

        asyncio.run(_run())

    def test_11_run_service_abort_blocks_stale_approval_resume(self):
        async def _run():
            app, _writes = build_full_app()
            scope = AuthScope("t_acme", "u_alice", ("employee",))
            ctx = ExecutionContext(trace_id=uid("tr"), thread_id="th_abort_1", request_id=uid("req"), auth_scope=scope)

            paused = await app.run_turn("查华东区 Q3 差旅超标并起草审批", ctx=ctx)
            self.assertIn("__interrupt__", paused)
            run_id = paused["run_id"]
            review_id = paused["__interrupt__"][0]["review_id"]

            # User aborts while paused awaiting approval
            aborted = await app.abort_run(scope=scope, run_id=run_id)
            self.assertEqual(aborted.status, "cancelled")

            # Stale approval resume MUST be rejected once cancelled!
            with self.assertRaises(RunStateError):
                await app.resume_approval(
                    thread_id="th_abort_1",
                    decision=ApprovalDecision(review_id=review_id, approved=True, approver_id="mgr_bob"),
                    scope=scope,
                )

            # New run on the same thread is allowed after previous run is cancelled
            next_turn = await app.run_turn("你好", thread_id="th_abort_1", scope=scope)
            self.assertTrue(next_turn["final_answer"])

        asyncio.run(_run())

    def test_13_consistency_regressions_op_key_expired_ticket_and_unique_oss_keys(self):
        async def _run():
            # 1. Gateway op_key is propagated to write/high-risk tool handler (matches ticket op_key)
            app, approval_writes = build_full_app()
            scope = AuthScope(tenant_id="t_acme", user_id="u_alice", roles=("employee",))
            ctx = ExecutionContext(
                trace_id=uid("tr"),
                thread_id="th_reg_1",
                request_id="req_reg_1",
                auth_scope=scope,
            )
            paused = await app.run_turn("查华东区 Q3 差旅超标并起草审批", ctx=ctx)
            ticket = paused["__interrupt__"][0]
            run_id = paused["run_id"]
            review_id = ticket["review_id"]
            op_key = ticket["op_key"]

            # 2. Expired approval ticket MUST fail cleanly WITHOUT stranding run in `running`
            with app.store.transaction() as cur:
                cur.execute(
                    "UPDATE tool_approvals SET expires_at = to_timestamp(1) WHERE review_id=%s",
                    (review_id,),
                )
            with self.assertRaises(ValueError):
                await app.resume_approval(
                    thread_id="th_reg_1",
                    decision=ApprovalDecision(review_id=review_id, approved=True, approver_id="mgr_bob"),
                    scope=scope,
                )
            self.assertEqual(app.store.get_run(run_id).status, "paused")

            # Restore expiry and resume -> handler receives exact gateway op_key
            with app.store.transaction() as cur:
                cur.execute(
                    "UPDATE tool_approvals SET expires_at = clock_timestamp() + interval '1 hour' WHERE review_id=%s",
                    (review_id,),
                )
            await app.resume_approval(
                thread_id="th_reg_1",
                decision=ApprovalDecision(review_id=review_id, approved=True, approver_id="mgr_bob"),
                scope=scope,
            )
            self.assertEqual(len(approval_writes), 1)
            self.assertEqual(approval_writes[0]["op_key"], op_key)

            # 3. Repeated writes to same logical path produce distinct immutable OSS keys & monotonic versions
            r1 = app.store.write_workspace_file("t_acme", "th_reg_ws", "out.md", "v1", actor_id="u_alice")
            r2 = app.store.write_workspace_file("t_acme", "th_reg_ws", "out.md", "v2", actor_id="u_alice")
            self.assertEqual((r1.version, r2.version), (1, 2))
            self.assertNotEqual(r1.oss_key, r2.oss_key)

        asyncio.run(_run())
