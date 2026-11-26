"""LangGraph (`StateGraph` + `InMemorySaver` + `PgOssStore`) node-replay & idempotency test suite.

Verifies the node-replay invariants of LangGraph + `PgOssStore`:
  1. `langgraph.types.interrupt()` + `Command(resume=...)` replays `tools_node` from the top
     without double-executing preceding WRITE tools in the same batch (`op_key` idempotency in PG)
     and without duplicating step-persisted messages in `session_messages`.
  2. Aborting while paused on an interrupt prevents stale approval resume from executing the tool.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List
import unittest

from langgraph.checkpoint.memory import InMemorySaver

from agent_runtime import (
    AgentRuntimeApp,
    ApprovalDecision,
    AuthScope,
    ExecutionContext,
    Message,
    RiskLevel,
    ToolCall,
    tool,
)
from agent_runtime.framework.core.config import uid
from agent_runtime.framework.core.runs import RunStateError

try:
    from tests._pg_harness import get_test_store, reset_test_db
except ImportError:
    from _pg_harness import get_test_store, reset_test_db


class TestRealLangGraphEngine(unittest.TestCase):
    def setUp(self):
        reset_test_db()

    def test_real_langgraph_mixed_write_and_high_risk_replay_idempotency(self):
        """In one batch `[write_tool, high_risk_tool]`, resuming `high_risk_tool` replays `tools_node`
        under LangGraph — `write_tool` MUST hit PG `op_key` cache and NOT execute a second time!
        """
        write_side_effects: List[str] = []
        high_risk_side_effects: List[str] = []

        @tool(risk_level=RiskLevel.WRITE)
        async def log_draft(note: str) -> str:
            """Record a draft note before submitting high-risk wire transfer."""
            write_side_effects.append(note)
            return f"草稿已记录:{note}"

        @tool(risk_level=RiskLevel.HIGH_RISK)
        async def wire_transfer(amount: int, to_account: str, ctx: ExecutionContext = None) -> Dict[str, Any]:
            """Execute high-risk bank wire transfer requiring human approval."""
            high_risk_side_effects.append(f"{to_account}:{amount}:{ctx.op_key}")
            return {"status": "transferred", "amount": amount, "to": to_account}

        class BatchReplayModel:
            def __init__(self) -> None:
                self.step = 0

            async def generate(self, *, messages: List[Message], tool_specs, ctx):
                self.step += 1
                if self.step == 1:
                    return Message(
                        "assistant",
                        "",
                        tool_calls=[
                            ToolCall("call_w1", "log_draft", {"note": "准备付款50000"}),
                            ToolCall("call_hr1", "wire_transfer", {"amount": 50000, "to_account": "ACC-88"}),
                        ],
                    )
                return Message("assistant", f"转账已完成，共记录草稿 {len(write_side_effects)} 次。")

        async def _run():
            app = AgentRuntimeApp(
                store=get_test_store(),
                model=BatchReplayModel(),
                checkpointer=InMemorySaver(),
                tools=[log_draft, wire_transfer],
            )

            scope = AuthScope("t_acme", "u_alice", ("employee",))
            ctx = ExecutionContext(
                trace_id=uid("tr"),
                thread_id="th_lg_replay_1",
                request_id="req_lg_1",
                auth_scope=scope,
            )

            paused = await app.run_turn("记录草稿并转账 50000 到 ACC-88", ctx=ctx)
            self.assertIn("__interrupt__", paused)
            self.assertEqual(write_side_effects, ["准备付款50000"])
            self.assertEqual(high_risk_side_effects, [])
            self.assertEqual(app.store.get_run(paused["run_id"]).status, "paused")

            ticket = paused["__interrupt__"][0]
            resumed = await app.resume_approval(
                thread_id="th_lg_replay_1",
                decision=ApprovalDecision(
                    review_id=ticket["review_id"], approved=True, approver_id="mgr_cfo"
                ),
                scope=scope,
            )
            self.assertNotIn("__interrupt__", resumed)
            self.assertIn("转账已完成", resumed["final_answer"])

            # CRITICAL: `log_draft` must STILL have executed only ONCE despite LangGraph node replay!
            self.assertEqual(write_side_effects, ["准备付款50000"])
            self.assertEqual(len(high_risk_side_effects), 1)
            self.assertTrue(high_risk_side_effects[0].endswith(ticket["op_key"]))
            self.assertEqual(app.store.get_run(paused["run_id"]).status, "completed")

            # Saved session history in PG must have no duplicate tool messages after replay
            saved = app.store.list_messages("th_lg_replay_1")
            tool_ids = [m.tool_call_id for m in saved if m.role == "tool"]
            self.assertEqual(tool_ids, ["call_w1", "call_hr1"])

        asyncio.run(_run())

    def test_real_langgraph_abort_while_paused_blocks_resume(self):
        """LangGraph checkpoint + PG `RunService` abort prevents stale resume from executing tool."""
        executed: List[int] = []

        @tool(risk_level=RiskLevel.HIGH_RISK)
        async def drop_table(table_name: str) -> str:
            """Dangerous operation."""
            executed.append(1)
            return f"dropped {table_name}"

        class SingleHighRiskModel:
            def __init__(self) -> None:
                self.called = False

            async def generate(self, *, messages, tool_specs, ctx):
                if not self.called:
                    self.called = True
                    return Message(
                        "assistant", "", tool_calls=[ToolCall("tc_drop", "drop_table", {"table_name": "users"})]
                    )
                return Message("assistant", "done")

        async def _run():
            app = AgentRuntimeApp(
                store=get_test_store(),
                model=SingleHighRiskModel(),
                checkpointer=InMemorySaver(),
                tools=[drop_table],
            )
            scope = AuthScope("t_acme", "u_alice", ("employee",))
            paused = await app.run_turn("删除 users 表", thread_id="th_lg_abort", scope=scope)
            review_id = paused["__interrupt__"][0]["review_id"]

            await app.abort_run(scope=scope, run_id=paused["run_id"])
            with self.assertRaises(RunStateError):
                await app.resume_approval(
                    thread_id="th_lg_abort",
                    decision=ApprovalDecision(review_id=review_id, approved=True, approver_id="mgr"),
                    scope=scope,
                )
            self.assertEqual(executed, [])

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
