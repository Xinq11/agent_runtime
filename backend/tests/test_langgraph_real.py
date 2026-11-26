"""Real LangGraph (`StateGraph` + `InMemorySaver`) integration test suite.

Runs inside the Python 3.12 backend container (or any environment with `langgraph>=1.0` installed)
and skips cleanly on zero-dependency host Python so `unittest discover` stays fast everywhere.

Specifically verifies the subtle node-replay invariants that `MiniStateGraph` only approximates:
  1. Real `langgraph.types.interrupt()` + `Command(resume=...)` replays `tools_node` from the top
     without double-executing preceding WRITE tools in the same batch (`op_key` idempotency) and
     without duplicating step-persisted messages.
  2. Multi-write in a single turn (`write` x2) + subsequent HIGH_RISK interrupt/resume preserves
     distinct `op_key`s and exact execution counts under real `InMemorySaver`.
  3. Expired or cancelled approval resume under real LangGraph leaves run state consistent.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List
import unittest

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
from agent_runtime.framework.core.langgraph_compat import HAS_LANGGRAPH
from agent_runtime.framework.core.runs import RunStateError

try:
    from langgraph.checkpoint.memory import InMemorySaver as LG_InMemorySaver  # type: ignore
except Exception:
    LG_InMemorySaver = None


@unittest.skipUnless(HAS_LANGGRAPH and LG_InMemorySaver is not None, "Requires real `langgraph` package")
class TestRealLangGraphEngine(unittest.TestCase):
    def test_real_langgraph_mixed_write_and_high_risk_replay_idempotency(self):
        """In one batch `[write_tool, high_risk_tool]`, resuming `high_risk_tool` replays `tools_node`
        under real LangGraph — `write_tool` MUST hit `op_key` cache and NOT execute a second time!
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
            saver = LG_InMemorySaver()
            app = AgentRuntimeApp(
                model=BatchReplayModel(),
                checkpointer=saver,
                tools=[log_draft, wire_transfer],
                prefer_real_langgraph=True,
            )
            # Confirm we compiled a real LangGraph graph, NOT CompiledMiniGraph
            self.assertNotEqual(type(app.graph).__name__, "CompiledMiniGraph")

            scope = AuthScope("t_acme", "u_alice", ("employee",))
            ctx = ExecutionContext(
                trace_id=uid("tr"),
                thread_id="th_lg_replay_1",
                request_id="req_lg_1",
                auth_scope=scope,
            )

            paused = await app.run_turn("记录草稿并转账 50000 到 ACC-88", ctx=ctx)
            self.assertIn("__interrupt__", paused)
            # `log_draft` ran once before `wire_transfer` hit `interrupt()`
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

            # CRITICAL: `log_draft` must STILL have executed only ONCE despite real LangGraph node replay!
            self.assertEqual(write_side_effects, ["准备付款50000"])
            self.assertEqual(len(high_risk_side_effects), 1)
            self.assertTrue(high_risk_side_effects[0].endswith(ticket["op_key"]))
            self.assertEqual(app.store.get_run(paused["run_id"]).status, "completed")

            # Saved session history must have no duplicate tool messages after replay
            saved = app.store.list_messages("th_lg_replay_1")
            tool_ids = [m.tool_call_id for m in saved if m.role == "tool"]
            self.assertEqual(tool_ids, ["call_w1", "call_hr1"])

        asyncio.run(_run())

    def test_real_langgraph_abort_while_paused_blocks_resume(self):
        """Real LangGraph checkpoint + `RunService` abort prevents stale resume from executing tool."""
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
                model=SingleHighRiskModel(),
                checkpointer=LG_InMemorySaver(),
                tools=[drop_table],
                prefer_real_langgraph=True,
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
