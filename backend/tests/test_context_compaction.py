"""Regression coverage for the two compaction thresholds and user-turn boundaries."""
import asyncio
import unittest
from unittest.mock import AsyncMock

from agent_runtime.framework.context.compaction import SessionCompactor
from agent_runtime.framework.context.memory import ContextBudget
from agent_runtime.framework.core.contracts import AuthScope, ExecutionContext, Message, ToolCall


class History:
    def __init__(self):
        self.messages = []
        self.summary = ("", 0)

    def get_summary(self, thread):
        return self.summary

    def list_messages(self, thread, after):
        return [m for m in self.messages if m.msg_id > after]

    def save_summary(self, thread, text, cursor):
        self.summary = (text, cursor)


class TestContextCompaction(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = History()
        self.ctx = ExecutionContext(trace_id="trace", thread_id="thread", request_id="req",
                                    auth_scope=AuthScope("user"))
        self.budget = ContextBudget(max_window_tokens=2000, reserve_output_tokens=200,
                                    max_extra_context_tokens=100, max_summary_tokens=100)
        self.compactor = SessionCompactor(self.store, None, self.budget)

    async def prepare(self, overhead=0, extras=()):
        return await self.compactor.prepare(
            self.ctx, system_prompt="sys", wire_overhead=overhead,
            summary=self.store.summary[0], cursor=self.store.summary[1],
            recent=self.store.list_messages("thread", self.store.summary[1]), extras=extras,
        )

    async def test_many_short_turns_below_soft_limit_do_not_compact(self):
        self.store.messages = [Message("user" if i % 2 else "assistant", "ok", msg_id=i)
                               for i in range(1, 41)]
        self.compactor.schedule(self.ctx, fixed_tokens=20)
        await self.compactor.drain()
        self.assertEqual(self.store.summary, ("", 0))

    async def test_running_soft_compaction_is_reused_under_hard_pressure(self):
        self.store.messages = [
            Message("user", "上次任务", msg_id=1),
            Message("assistant", "", [ToolCall("id", "lookup", {})], msg_id=2),
            Message("tool", "x" * 4800, tool_call_id="id", msg_id=3),
            Message("assistant", "核验结束", msg_id=4),
            Message("user", "现在的问题", msg_id=5),
        ]
        started, release = asyncio.Event(), asyncio.Event()

        async def summarize(**kwargs):
            started.set()
            await release.wait()
            return Message("assistant", "先前任务已核验")

        model = AsyncMock()
        model.config = None
        model.generate.side_effect = summarize
        self.compactor.model = model
        self.compactor.schedule(self.ctx, fixed_tokens=100)
        await asyncio.wait_for(started.wait(), 2)
        self.compactor.schedule(self.ctx, fixed_tokens=100)
        pending = asyncio.create_task(self.prepare(overhead=700))
        await asyncio.sleep(0)
        self.assertFalse(pending.done())
        release.set()
        inputs, stats = await pending
        self.assertEqual(self.store.summary[1], 4)
        self.assertEqual(len(self.store.messages), 5)
        self.assertEqual(inputs[-1].content, "现在的问题")
        self.assertIn("先前任务已核验", inputs[1].content)
        self.assertEqual(stats["summary_cursor"], 4)
        model.generate.assert_awaited_once()

    async def test_hard_pressure_uses_temporary_summary_and_keeps_current_user(self):
        self.store.messages = [
            Message("user", "old", msg_id=1), Message("assistant", "a" * 5000, msg_id=2),
            Message("user", "b" * 6570, msg_id=3),
        ]
        inputs, stats = await self.prepare(overhead=60, extras=["background"])
        self.assertTrue(stats["emergency"])
        self.assertLessEqual(stats["input_tokens_est"], self.budget.max_input_tokens)
        self.assertEqual(inputs[-1].content, self.store.messages[-1].content)
        self.assertIn("CONVERSATION_SUMMARY", inputs[1].content)
        self.assertEqual(self.store.summary, ("", 0))
        self.store.messages[-1].content *= 2
        with self.assertRaisesRegex(ValueError, "分段"):
            await self.prepare(overhead=60)
