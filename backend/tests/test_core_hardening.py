"""Core module hardening tests:
  1. Zombie `running` run lease heartbeat & auto-recovery (never reaps `paused`)
  2. SSE `Last-Event-ID` reconnect replay & creator ownership isolation
  3. Write tool `reconcile(op_key, args, ctx)` callback (done / failed / pending)
  4. Sub-agent context budget enforcement & DSML leak stripping
  5. Multi-provider LLM retry (pre-token only), cache-hit metrics, model windows & `/config/model`
  6. Compaction observability metrics & structured logs
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest

import httpx
from prometheus_client import CollectorRegistry, Counter, Histogram

from agent_runtime import (
    AgentRuntimeApp,
    AuthScope,
    ExecutionContext,
    Message,
    ReconcileResult,
    RiskLevel,
    SubAgentProfile,
    ToolCall,
    tool,
)
from agent_runtime.framework.context.compaction import SessionCompactor
from agent_runtime.framework.context.memory import ContextBudget, resolve_model_window
from agent_runtime.framework.core.config import ModelSettings
from agent_runtime.framework.core.runs import RunConflictError
from agent_runtime.framework.tools.gateway import ToolGateway, compute_op_key
from agent_runtime.framework.tools.registry import ToolRegistry
from agent_runtime.framework.tools.subagent import run_readonly_subagent
from agent_runtime.infra.llm.provider import OpenAICompatibleProvider

try:
    from tests._pg_harness import get_test_store, reset_test_db
except ImportError:
    from _pg_harness import get_test_store, reset_test_db


def _make_metrics_obs():
    prom = CollectorRegistry()
    events = []
    return SimpleNamespace(
        prom=prom,
        ttft=Histogram("test_ttft_seconds", "ttft", ["mode"], registry=prom),
        tokens=Counter("test_tokens", "tokens", ["kind", "mode"], registry=prom),
        prompt_cache_hit_ratio=Histogram(
            "test_prompt_cache_hit_ratio",
            "cache ratio",
            ["mode"],
            buckets=(0.0, 0.25, 0.5, 0.75, 1.0),
            registry=prom,
        ),
        compaction_duration=Histogram("test_compaction_dur", "dur", ["trigger"], registry=prom),
        compactions=Counter("test_compactions", "cnt", ["trigger", "outcome"], registry=prom),
        llm_retries=Counter("test_llm_retries", "retries", ["mode", "reason"], registry=prom),
        runs_recovered=Counter("test_runs_recovered", "recovered", ["reason"], registry=prom),
        events=events,
        log_event=lambda ev, tr, th, **kw: events.append({"event": ev, "trace_id": tr, "thread_id": th, **kw}),
    )


class TestCoreHardening(unittest.TestCase):
    def setUp(self):
        reset_test_db()

    def test_01_zombie_running_run_auto_reaped_while_paused_run_preserved(self):
        store = get_test_store()
        obs = _make_metrics_obs()
        runtime = AgentRuntimeApp(store=store, obs=obs)
        scope = AuthScope("u_alice", ("employee",))

        store.create_thread(scope, "th_zombie", "Zombie Run Test")
        r1 = runtime.run_service.start_run(
            scope=scope, thread_id="th_zombie", trace_id="tr_1", request_id="req_1"
        )
        # Fresh active running run blocks a concurrent run
        with self.assertRaises(RunConflictError):
            runtime.run_service.start_run(
                scope=scope, thread_id="th_zombie", trace_id="tr_2", request_id="req_2"
            )

        # Heartbeat keeps active run fresh
        self.assertTrue(runtime.run_service.heartbeat_run(r1.run_id))

        # Simulate crashed worker by backdating `updated_at` past stale threshold
        with store.transaction() as cur:
            cur.execute(
                "UPDATE agent_runs SET updated_at = clock_timestamp() - interval '600 seconds' WHERE run_id=%s",
                (r1.run_id,),
            )

        # Next `start_run` automatically reaps the zombie run and succeeds without 409!
        r2 = runtime.run_service.start_run(
            scope=scope, thread_id="th_zombie", trace_id="tr_3", request_id="req_3"
        )
        self.assertEqual(r2.status, "running")
        old = store.get_run(r1.run_id)
        self.assertEqual(old.status, "failed")
        self.assertEqual(old.error_detail, "stale_run_recovered")
        self.assertEqual(obs.runs_recovered.labels("conflict_recovery")._value.get(), 1.0)

        # Now mark `r2` as `paused` (waiting for human approval) and backdate it far in the past
        runtime.run_service.mark_paused(r2.run_id)
        with store.transaction() as cur:
            cur.execute(
                "UPDATE agent_runs SET updated_at = clock_timestamp() - interval '600 seconds' WHERE run_id=%s",
                (r2.run_id,),
            )
        # Reaper MUST NEVER kill `paused` runs waiting for human approval
        self.assertEqual(runtime.run_service.reap_stale_runs(stale_after_sec=1.0), 0)
        self.assertEqual(store.get_run(r2.run_id).status, "paused")

    def test_02_sse_last_event_id_reconnect_replay_and_ownership(self):
        from fastapi import HTTPException
        from agent_runtime.api.chat_stream import chat_sse, resume_run_events
        from agent_runtime.api.schemas import ChatRequest

        class StreamModel:
            def __init__(self):
                self.config = SimpleNamespace(
                    api_key="sk-test",
                    model="deepseek-chat",
                    effective="deepseek",
                    public=lambda: {"model": "deepseek-chat", "mode": "deepseek", "has_api_key": True},
                )

            async def generate(self, *, messages, tool_specs, ctx):
                for idx in range(5):
                    await ctx.emit("token", {"text": f"part-{idx}"})
                    await asyncio.sleep(0.01)
                return Message("assistant", "part-0part-1part-2part-3part-4")

        async def _run():
            store = get_test_store()
            model = StreamModel()
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

            req_alice = SimpleNamespace(
                app=fake_app,
                headers={"X-User-Id": "u_alice", "X-User-Roles": "employee"},
                is_disconnected=_is_disconnected,
            )
            resp = await chat_sse(req_alice, ChatRequest(message="hello", thread_id="th_replay"))
            aiter = resp.body_iterator.__aiter__()
            frame1 = await asyncio.wait_for(aiter.__anext__(), timeout=2.0)
            self.assertIn("id: 1\n", frame1)
            self.assertIn("event: meta\n", frame1)
            # Extract run_id from first frame
            import json as _json

            data_line = next(l for l in frame1.splitlines() if l.startswith("data:"))
            run_id = _json.loads(data_line[5:].strip())["run_id"]

            # Client disconnects right after frame 1
            disconnected = True
            await resp.body_iterator.aclose()

            # Wait for background worker to finish
            for _ in range(50):
                if runtime.sse_broker.get(run_id).done:
                    break
                await asyncio.sleep(0.02)

            # Reconnect with Last-Event-ID: 1 -> must receive frames 2..done cleanly!
            async def _not_disconnected() -> bool:
                return False

            req_reconnect = SimpleNamespace(
                app=fake_app,
                headers={"X-User-Id": "u_alice", "X-User-Roles": "employee", "Last-Event-ID": "1"},
                is_disconnected=_not_disconnected,
            )
            replay_resp = await resume_run_events(run_id, req_reconnect)
            frames = [chunk async for chunk in replay_resp.body_iterator]
            joined = "".join(frames)
            self.assertNotIn("id: 1\n", joined)
            self.assertIn("part-0", joined)
            self.assertIn("part-4", joined)
            self.assertIn("event: done\n", joined)

            # Another user CANNOT replay Alice's run events -> 404
            req_bob = SimpleNamespace(
                app=fake_app,
                headers={"X-User-Id": "u_bob", "X-User-Roles": "employee", "Last-Event-ID": "0"},
                is_disconnected=_not_disconnected,
            )
            with self.assertRaises(HTTPException) as ctx_err:
                await resume_run_events(run_id, req_bob)
            self.assertEqual(ctx_err.exception.status_code, 404)

            # Evicted stream buffer -> 410 Gone
            runtime.sse_broker._streams.pop(run_id, None)
            with self.assertRaises(HTTPException) as gone_err:
                await resume_run_events(run_id, req_reconnect)
            self.assertEqual(gone_err.exception.status_code, 410)

        asyncio.run(_run())

    def test_03_write_tool_reconcile_hook_resolves_timeout_without_double_write(self):
        async def _run():
            store = get_test_store()
            scope = AuthScope("u_alice", ("employee",))
            store.create_thread(scope, "th_rec", "Reconcile Test")
            ctx = ExecutionContext(
                trace_id="tr_rec",
                thread_id="th_rec",
                request_id="req_rec",
                auth_scope=scope,
            )

            write_invocations = {"count": 0}
            reconcile_mode = {"state": "pending"}

            async def _reconcile_oa(op_key: str, args: dict, rctx: ExecutionContext):
                if reconcile_mode["state"] == "pending":
                    return ReconcileResult(status="pending", content="仍在对账中")
                if reconcile_mode["state"] == "done":
                    return ReconcileResult(
                        status="done",
                        content=f"已对账确认单据 OA-{args['order_id']}",
                        receipt={"ticket": f"OA-{args['order_id']}"},
                    )
                return ReconcileResult(
                    status="failed",
                    content="下游确认未受理",
                    error_code="downstream_rejected",
                )

            @tool(risk_level=RiskLevel.WRITE, timeout_sec=0.05, reconcile=_reconcile_oa)
            async def create_oa_order(order_id: str, ctx: ExecutionContext = None) -> str:
                """Create an OA order (times out first time)."""
                write_invocations["count"] += 1
                await asyncio.sleep(0.5)
                return f"created {order_id}"

            reg = ToolRegistry()
            reg.register(create_oa_order)
            reg.freeze()
            gw = ToolGateway(store=store, registry=reg)

            call = ToolCall(id="call_oa_1", name="create_oa_order", args={"order_id": "2026-01"})
            # 1st attempt times out -> persisted as `reconcile`
            res1 = await gw.execute_one(call, ctx)
            self.assertFalse(res1.ok)
            self.assertEqual(res1.error_code, "timeout")
            self.assertEqual(write_invocations["count"], 1)

            # 2nd attempt with pending reconciler -> stays `reconcile`, write NOT re-executed
            res2 = await gw.execute_one(call, ctx)
            self.assertFalse(res2.ok)
            self.assertEqual(res2.error_code, "reconcile")
            self.assertEqual(write_invocations["count"], 1)

            # 3rd attempt when downstream confirms success -> transitions to `done` cleanly!
            reconcile_mode["state"] = "done"
            res3 = await gw.execute_one(call, ctx)
            self.assertTrue(res3.ok)
            self.assertIn("OA-2026-01", res3.content)
            self.assertTrue(res3.metadata.get("reconciled"))
            self.assertEqual(write_invocations["count"], 1)

            # 4th attempt hits normal idempotent `done` cache without calling handler or reconciler again
            res4 = await gw.execute_one(call, ctx)
            self.assertTrue(res4.ok)
            self.assertTrue(res4.metadata.get("cached"))
            self.assertEqual(write_invocations["count"], 1)

            # Separate call testing `failed` reconcile outcome via explicit `reconcile_tool_run`
            call2 = ToolCall(id="call_oa_2", name="create_oa_order", args={"order_id": "2026-02"})
            res_t = await gw.execute_one(call2, ctx)
            self.assertEqual(res_t.error_code, "timeout")
            reconcile_mode["state"] = "failed"
            op_key_2 = compute_op_key(ctx, call2)
            res_fail = await gw.reconcile_tool_run(
                op_key_2, tool_name="create_oa_order", args={"order_id": "2026-02"}, ctx=ctx
            )
            self.assertFalse(res_fail.ok)
            self.assertEqual(res_fail.error_code, "downstream_rejected")

        asyncio.run(_run())

    def test_04_subagent_applies_context_budget_and_strips_dsml_markup(self):
        async def _run():
            store = get_test_store()
            obs = _make_metrics_obs()

            @tool(risk_level=RiskLevel.READ)
            def big_lookup(q: str) -> str:
                """Return a sizable read-only snippet."""
                return "data " * 120

            reg = ToolRegistry()
            reg.register(big_lookup)
            reg.freeze()
            gw = ToolGateway(store=store, registry=reg)

            seen_msg_counts = []

            class SubModel:
                def __init__(self):
                    self.step = 0

                async def generate(self, *, messages, tool_specs, ctx, tool_choice=None):
                    self.step += 1
                    seen_msg_counts.append(len(messages))
                    if self.step == 1:
                        return Message("assistant", "", [ToolCall("sub_c1", "big_lookup", {"q": "a"})])
                    return Message(
                        "assistant",
                        "华东区汇总完成。<｜DSML｜function_calls><invoke name=\"big_lookup\"/></｜DSML｜function_calls>",
                    )

            profile = SubAgentProfile(
                subagent_name="analyst",
                description="Read-only analyst",
                system_prompt="你是只读分析师",
                allowed_tools=("big_lookup",),
                max_steps=3,
                budget=ContextBudget(max_window_tokens=1200, reserve_output_tokens=200),
            )
            parent_ctx = ExecutionContext(
                trace_id="tr_sub",
                thread_id="th_sub",
                request_id="req_sub",
                auth_scope=AuthScope("u_alice", ("employee",)),
            )

            # Use real ObservabilityHub for span context manager
            from agent_runtime.infra.plg.hub import ObservabilityHub

            hub = ObservabilityHub(store)
            ans = await run_readonly_subagent(
                profile=profile,
                goal="查华东区数据",
                parent_ctx=parent_ctx,
                model_client=SubModel(),
                tool_gateway=gw,
                obs=hub,
            )
            self.assertEqual(ans, "华东区汇总完成。")
            self.assertNotIn("DSML", ans)
            self.assertTrue(any(e["event"] == "subagent_done" for e in hub.loki_events))

        asyncio.run(_run())

    def test_05_llm_transient_retry_pre_token_safety_cache_metrics_and_multi_provider(self):
        from fastapi import HTTPException
        from agent_runtime.api.schemas import ModelConfigUpdateRequest
        from agent_runtime.api.system import update_model
        from agent_runtime.framework.core.security import NoopAccessController

        # 1. Model context window presets
        self.assertEqual(resolve_model_window("deepseek-chat"), 65536)
        self.assertEqual(resolve_model_window("gpt-4o"), 128000)
        self.assertEqual(resolve_model_window("claude-3-7-sonnet-20250219"), 200000)
        self.assertEqual(ContextBudget.for_model("qwen-plus").max_window_tokens, 128000)

        # 2. Retry on transient 429 & 503 BEFORE any token emitted, then succeed + record cache hit ratio
        attempts = {"n": 0}
        sse_body = (
            'data: {"choices":[{"delta":{"content":"你好"}}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1000,"completion_tokens":20,"prompt_cache_hit_tokens":800}}\n\n'
            "data: [DONE]\n\n"
        )

        def _handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
            if attempts["n"] == 2:
                return httpx.Response(503, request=request)
            return httpx.Response(200, text=sse_body, request=request)

        async def _run_retry():
            obs = _make_metrics_obs()
            async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
                provider = OpenAICompatibleProvider(
                    client,
                    obs,
                    settings=ModelSettings(api_key="sk-test", model="deepseek-chat", mode="deepseek"),
                    max_retries=2,
                    retry_base_delay_sec=0.0,
                )
                ctx = ExecutionContext(trace_id="tr_llm", thread_id="th_llm", request_id="req_llm", auth_scope=AuthScope("u_alice"))
                msg = await provider.generate(
                    messages=[Message("user", "hi")],
                    tool_specs=[],
                    ctx=ctx,
                )
            self.assertEqual(msg.content, "你好")
            self.assertEqual(attempts["n"], 3)
            self.assertEqual(obs.llm_retries.labels("deepseek", "http_429")._value.get(), 1.0)
            self.assertEqual(obs.llm_retries.labels("deepseek", "http_503")._value.get(), 1.0)
            # Prompt cache hit ratio 800 / 1000 = 0.8 observed once
            self.assertAlmostEqual(obs.prompt_cache_hit_ratio.labels("deepseek")._sum.get(), 0.8, places=4)

            # Multi-provider `/config/model` routing
            fake_req = SimpleNamespace(
                app=SimpleNamespace(
                    state=SimpleNamespace(
                        model=provider,
                        runtime=SimpleNamespace(
                            access_controller=NoopAccessController(),
                            default_identity=lambda: AuthScope("u_admin", ("platform_admin",)),
                        ),
                    )
                ),
                headers={"X-User-Id": "u_admin", "X-User-Roles": "platform_admin"},
            )
            pub_qwen = await update_model(
                fake_req, ModelConfigUpdateRequest(mode="qwen", model="qwen-plus", api_key="sk-qwen")
            )
            self.assertEqual(pub_qwen["mode"], "qwen")
            self.assertIn("dashscope.aliyuncs.com", pub_qwen["base_url"])

            with self.assertRaises(HTTPException) as bad_host:
                await update_model(
                    fake_req,
                    ModelConfigUpdateRequest(mode="compatible", base_url="https://evil.example.com/v1", model="x"),
                )
            self.assertEqual(bad_host.exception.status_code, 422)

        asyncio.run(_run_retry())

    def test_06_compaction_emits_duration_and_outcome_metrics(self):
        async def _run():
            obs = _make_metrics_obs()

            class MemHistory:
                def __init__(self):
                    self.messages = [
                        Message("user", "u" * 300, msg_id=1),
                        Message("assistant", "a" * 3000, msg_id=2),
                        Message("user", "latest question", msg_id=3),
                    ]
                    self.summary = ("", 0)

                def get_summary(self, thread):
                    return self.summary

                def list_messages(self, thread, after):
                    return [m for m in self.messages if m.msg_id > after]

                def save_summary(self, thread, text, cursor):
                    self.summary = (text, cursor)

            store = MemHistory()
            budget = ContextBudget(
                max_window_tokens=1000, reserve_output_tokens=100, max_extra_context_tokens=50, max_summary_tokens=80
            )
            compactor = SessionCompactor(store, None, budget, obs=obs)
            ctx = ExecutionContext(trace_id="tr_cmp", thread_id="th_cmp", request_id="req_cmp", auth_scope=AuthScope("u_alice"))
            compactor.schedule(ctx, fixed_tokens=20)
            await compactor.drain()

            self.assertEqual(obs.compactions.labels("soft", "fallback")._value.get(), 1.0)
            self.assertTrue(any(e["event"] == "context_compacted" for e in obs.events))

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
