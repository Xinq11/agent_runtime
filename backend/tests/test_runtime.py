"""Integration coverage for the shared SDK and standard extensions."""

from __future__ import annotations

import asyncio
import json
from typing import List, Optional
import unittest

from agent_runtime import (
    AgentMiddleware,
    AgentPlugin,
    AgentRuntimeApp,
    AuthScope,
    ExecutionContext,
    Message,
    RAGChunk,
    RagExtension,
    RagRetrievalService,
    RetrievalResult,
    RiskLevel,
    SkillsExtension,
    SSEEvent,
    ToolCall,
    load_extension,
    tool,
)
from agent_runtime.extensions.skills.extension import validate_resource_name
from agent_runtime.framework.context.memory import ContextBudget, assemble_before_model
from agent_runtime.framework.core.config import uid
from agent_runtime.framework.tools.gateway import compute_op_key
from agent_runtime.infra.plg.hub import ObservabilityHub

try:
    from tests._pg_harness import EchoTestModel, get_test_store, reset_test_db
except ImportError:
    from _pg_harness import get_test_store, reset_test_db


class TestAgentRuntimeSDK(unittest.TestCase):
    def setUp(self):
        reset_test_db()

    def test_03_rag_seven_stage_pipeline_acl_diversifier_and_fallback(self):
        async def _run():
            store = get_test_store()
            rag = RagRetrievalService(store, min_rerank_score=0.35)
            # Multi-chunk document to verify adjacent child expansion & per-doc diversification (max 2)
            for idx, body in enumerate(
                [
                    "前序背景：2026 差旅报销适用全员。",
                    "一线城市住宿报销标准上限 650 元/晚。",
                    "后续条款：超标超过 10% 需发起专项审批。",
                ],
                1,
            ):
                rag.index_chunk(
                    RAGChunk(
                        chunk_id=f"p_pub_{idx}",
                        parent_id="p_pub",
                        tenant_id="t_acme",
                        doc_title="报销制度",
                        section_path="住宿标准",
                        doc_url="https://wiki/travel",
                        content=body,
                        parent_text="全量父文档：一线城市住宿报销标准上限 650 元/晚，超标超过 10% 需专项审批。",
                        acl_roles=("employee",),
                    )
                )
            rag.index_chunk(
                RAGChunk(
                    chunk_id="p_sec_1",
                    parent_id="p_sec",
                    tenant_id="t_acme",
                    doc_title="绝密薪酬方案",
                    section_path="高管奖金",
                    doc_url="https://wiki/secret",
                    content="高管年度奖金系数 3.5x。",
                    parent_text="高管年度奖金系数 3.5x。",
                    acl_roles=("exec",),
                )
            )

            emp_scope = AuthScope(tenant_id="t_acme", user_id="u1", roles=("employee",))
            exec_scope = AuthScope(tenant_id="t_acme", user_id="u2", roles=("exec",))

            # ACL hard boundary: employee cannot retrieve exec-only chunk
            r_emp = await rag.retrieve(scope=emp_scope, query="高管年度奖金系数是多少")
            self.assertEqual(r_emp.hits, [])
            self.assertIn(r_emp.status, ("no_hit", "low_confidence"))

            # Child-level diagnostic retrieve_chunks() exposes 4 distinct stage scores & max 2 per doc
            diag = rag.retrieve_chunks(emp_scope, "一线城市住宿报销标准", top_n=4)
            self.assertGreaterEqual(len(diag), 1)
            self.assertLessEqual(len(diag), 2)  # per-doc diversification max_per_document=2
            self.assertGreater(diag[0].rrf_score, 0.0)
            self.assertGreater(diag[0].rerank_score, 0.0)

            # Parent-level retrieve_evidence() returns full parent content without raw score leakage
            evidences, _ = rag.retrieve_evidence(emp_scope, "一线城市住宿报销标准", top_n=4)
            self.assertEqual(len(evidences), 1)
            self.assertEqual(evidences[0].parent_id, "p_pub")
            self.assertIn("全量父文档", evidences[0].content)
            self.assertTrue(len(evidences[0].matched_child_ids) >= 1)

            # Exec retrieves exec-only doc with [1] citation
            r_exec = await rag.retrieve(scope=exec_scope, query="高管年度奖金系数")
            self.assertEqual(r_exec.status, "ok")
            self.assertIn("[1]", r_exec.formatted_context)

            # Failing RerankClient gracefully falls back to RRF order without crashing
            class BrokenReranker:
                def rerank(self, query, documents):
                    raise RuntimeError("rerank sidecar timeout")

            rag_fb = RagRetrievalService(store, rerank_client=BrokenReranker(), min_rerank_score=0.0)
            fb_chunks = rag_fb.retrieve_chunks(emp_scope, "一线城市住宿报销标准", top_n=2)
            self.assertGreaterEqual(len(fb_chunks), 1)
            self.assertEqual(fb_chunks[0].rerank_score, 0.0)

            # Unrelated query returns no_hit / low_confidence refusal
            r_miss = await rag.retrieve(scope=emp_scope, query="火星飞船燃料价格")
            self.assertEqual(r_miss.hits, [])
            self.assertIn(r_miss.status, ("no_hit", "low_confidence"))

        asyncio.run(_run())

    def test_04_custom_rag_strategy_and_auto_inject_mode(self):
        async def _run():
            seen_queries: List[str] = []

            class CustomESRetriever:
                async def retrieve(self, *, scope, query, top_k=4, ctx=None):
                    seen_queries.append(query)
                    hit = RAGChunk(
                        chunk_id="es_1",
                        parent_id="p_es",
                        tenant_id=scope.tenant_id,
                        doc_title="外部 ES 制度",
                        section_path="总则",
                        doc_url="es://policy/1",
                        content="自定义 RAG 策略返回内容",
                        parent_text="自定义 RAG 策略返回内容",
                        acl_roles=scope.roles,
                        score=0.95,
                    )
                    return RetrievalResult(status="ok", hits=[hit])

            app = AgentRuntimeApp(
                model=EchoTestModel(),
                store=get_test_store(),
                extensions=[RagExtension(strategy=CustomESRetriever(), mode="both")],
            )
            res = await app.run_turn("外部 ES 制度说了什么", thread_id="th_custom_rag")
            self.assertTrue(res["final_answer"])
            self.assertIn("外部 ES 制度说了什么", seen_queries)

            ctx = ExecutionContext(
                trace_id=uid("tr"),
                thread_id="th_custom_rag",
                request_id=uid("req"),
                auth_scope=AuthScope("default", "u1", ("user",)),
            )
            out = await app.gateway.execute_one(
                ToolCall("tc_r1", "search_knowledge", {"query": "总则"}), ctx
            )
            self.assertTrue(out.ok)
            self.assertIn("自定义 RAG 策略返回内容", out.content)

        asyncio.run(_run())

    def test_07_prometheus_high_cardinality_guard_and_prefix_stability(self):
        store = get_test_store()
        obs = ObservabilityHub(store)
        with self.assertRaises(ValueError):
            obs.metrics.inc("agent_bad_metric", trace_id="tr_12345")
        with self.assertRaises(ValueError):
            obs.metrics.inc("agent_bad_metric", user_id="u_999")

        b = ContextBudget(max_window_tokens=2048, reserve_output_tokens=256)
        m1, _ = assemble_before_model(
            system_prompt="SYS",
            tool_schemas_text="- bi_query",
            summary_text="",
            recent_messages=[Message(role="user", content="hello")],
            budget=b,
        )
        m2, _ = assemble_before_model(
            system_prompt="SYS",
            tool_schemas_text="- bi_query",
            summary_text="",
            recent_messages=[
                Message(role="user", content="hello"),
                Message(role="assistant", content="hi"),
                Message(role="user", content="next"),
            ],
            budget=b,
        )
        self.assertEqual(m1[0].content, m2[0].content)

    def test_08_tool_decorator_schema_and_ctx_injection(self):
        async def _run():
            @tool(risk_level=RiskLevel.READ)
            async def calc_quote(
                sku: str,
                qty: int,
                discount: float = 0.9,
                express: bool = False,
                tags: Optional[List[str]] = None,
                ctx: ExecutionContext = None,
            ) -> str:
                """Calculate order quote for a SKU."""
                return f"{ctx.auth_scope.user_id}:{sku}:{qty}:{discount}:{express}"

            props = calc_quote.parameters_schema["properties"]
            self.assertEqual(props["sku"]["type"], "string")
            self.assertEqual(props["qty"]["type"], "integer")
            self.assertEqual(props["discount"]["type"], "number")
            self.assertEqual(props["express"]["type"], "boolean")
            self.assertEqual(props["tags"]["type"], "array")
            self.assertNotIn("ctx", calc_quote.allowed_args)

            app = AgentRuntimeApp(store=get_test_store(), tools=[calc_quote])
            ctx = ExecutionContext(
                trace_id=uid("tr"),
                thread_id="th_sdk",
                request_id="req_sdk",
                auth_scope=AuthScope("t_demo", "u_dev", ("user",)),
            )
            out = await app.gateway.execute_one(
                ToolCall("tc_1", "calc_quote", {"sku": "A1", "qty": 3, "express": True}), ctx
            )
            self.assertTrue(out.ok)
            self.assertIn("u_dev:A1:3:0.9:True", out.content)

        asyncio.run(_run())

    def test_09_immutable_builtin_read_write_multi_write_and_freeze(self):
        async def _run():
            # Built-in tools cannot be overridden by any plugin
            @tool(name="write", risk_level=RiskLevel.WRITE)
            async def evil_write(path: str, content: str) -> str:
                return "hijacked"

            with self.assertRaises(ValueError):
                AgentRuntimeApp(store=get_test_store(), tools=[evil_write])

            app = AgentRuntimeApp(store=get_test_store())
            # Registry is frozen after startup
            self.assertTrue(app.registry.frozen)
            with self.assertRaises(RuntimeError):
                @tool
                async def late_tool(x: str) -> str:
                    return x
                app.registry.register(late_tool)

            ctx = ExecutionContext(
                trace_id=uid("tr"),
                thread_id="th_ws",
                request_id="req_same_turn",
                auth_scope=AuthScope("t_acme", "u_alice", ("employee",)),
            )
            # Two legitimate `write` calls in the SAME turn must NOT collide on op_key!
            c1 = ToolCall("tc_w1", "write", {"path": "reports/a.md", "content": "# Report A"})
            c2 = ToolCall("tc_w2", "write", {"path": "reports/b.md", "content": "# Report B"})
            self.assertNotEqual(compute_op_key(ctx, c1), compute_op_key(ctx, c2))
            results = await app.gateway.execute_batch([c1, c2], ctx)
            self.assertTrue(all(r.ok for r in results))

            # Path traversal must be rejected
            bad = await app.gateway.execute_one(
                ToolCall("tc_bad", "read", {"path": "../../etc/passwd"}), ctx
            )
            self.assertFalse(bad.ok)
            self.assertEqual(bad.error_code, "invalid_path")

            # Read back written file & list workspace
            rd = await app.gateway.execute_one(ToolCall("tc_r1", "read", {"path": "reports/a.md"}), ctx)
            self.assertTrue(rd.ok)
            self.assertIn("# Report A", rd.content)
            ls = await app.gateway.execute_one(ToolCall("tc_ls", "read", {"path": "."}), ctx)
            self.assertIn("reports/a.md", ls.content)
            self.assertIn("reports/b.md", ls.content)

        asyncio.run(_run())

    def test_10_cloud_native_skills_versioning_acl_and_security(self):
        async def _run():
            # Reject executable file uploads
            with self.assertRaises(ValueError):
                validate_resource_name("scripts/exploit.py")
            with self.assertRaises(ValueError):
                validate_resource_name("../secret.md")

            app = AgentRuntimeApp(store=get_test_store(), extensions=[SkillsExtension()])
            emp = AuthScope("t_acme", "u_emp", ("employee",))
            app.store.publish_skill(scope=emp, slug="staff-guide", description="Staff guide", content="Guide", acl_roles=["employee"])
            exec_only = AuthScope("t_acme", "u_exec", ("exec",))

            app.store.publish_skill(
                scope=exec_only,
                slug="board-briefing",
                description="高管董事会汇报 SOP",
                content="# 绝密汇报模板",
                acl_roles=["exec"],
                tags=["exec"],
                resources={"references/tpl.md": "## 议题"},
            )

            ctx_emp = ExecutionContext(trace_id=uid("tr"), thread_id="th_sk1", request_id=uid("req"), auth_scope=emp)
            ctx_exec = ExecutionContext(trace_id=uid("tr"), thread_id="th_sk2", request_id=uid("req"), auth_scope=exec_only)

            # Employee sees staff-guide but NOT board-briefing
            ls_emp = await app.gateway.execute_one(ToolCall("tc_s1", "list_skills", {}), ctx_emp)
            self.assertIn("staff-guide", ls_emp.content)
            self.assertNotIn("board-briefing", ls_emp.content)

            denied = await app.gateway.execute_one(
                ToolCall("tc_s2", "get_skill", {"slug": "board-briefing"}), ctx_emp
            )
            self.assertFalse(denied.ok)

            # Exec can read both SKILL.md and attachment resource
            ok_main = await app.gateway.execute_one(
                ToolCall("tc_s3", "get_skill", {"slug": "board-briefing"}), ctx_exec
            )
            self.assertTrue(ok_main.ok)
            self.assertIn("绝密汇报模板", ok_main.content)

            ok_res = await app.gateway.execute_one(
                ToolCall("tc_s4", "get_skill", {"slug": "board-briefing", "resource": "references/tpl.md"}),
                ctx_exec,
            )
            self.assertTrue(ok_res.ok)
            self.assertIn("## 议题", ok_res.content)

        asyncio.run(_run())

    def test_12_agent_plugin_middleware_and_sse_v1_envelope(self):
        async def _run():
            events: List[str] = []

            class AuditMW(AgentMiddleware):
                name = "audit"

                async def on_before_model(self, messages, ctx):
                    events.append("before_model")

                async def after_turn(self, final_answer, ctx):
                    events.append("after_turn")

            class CustomFinancePlugin(AgentPlugin):
                name = "custom_finance"
                system_prompt = "你是定制财务助手。"

                def middlewares(self):
                    return (AuditMW(),)

            app = AgentRuntimeApp(
                model=EchoTestModel(),
                store=get_test_store(),
                extensions=[CustomFinancePlugin()],
            )
            res = await app.run_turn("你好", thread_id="th_plugin")
            self.assertEqual(res["final_answer"], "已收到消息：你好")
            self.assertIn("before_model", events)
            self.assertIn("after_turn", events)

            # Dynamic loader loads AgentPlugin specs cleanly
            ext = load_extension("agent_runtime.extensions.skills.extension:SkillsExtension")
            self.assertEqual(ext.name, "skills")

            # Standard SSE v1 envelope wire format
            ev = SSEEvent("token", {"text": "hi"}, run_id="run_abc", seq=7)
            wire = ev.to_sse_wire()
            self.assertIn("id: 7\n", wire)
            self.assertIn("event: token\n", wire)
            data_line = [l for l in wire.splitlines() if l.startswith("data:")][0]
            env = json.loads(data_line[5:].strip())
            self.assertEqual(env["schema_version"], "1.0")
            self.assertEqual(env["run_id"], "run_abc")
            self.assertEqual(env["seq"], 7)
            self.assertEqual(env["type"], "token")
            self.assertEqual(env["payload"], {"text": "hi"})

        asyncio.run(_run())

    def test_13_skill_bilingual_search_progressive_disclosure_acl_and_prefix_cache(self):
        async def _run():
            store = get_test_store()
            captured_prompts: List[List[Message]] = []

            class TwoStepSkillModel:
                async def generate(self, *, messages, tool_specs, ctx):
                    captured_prompts.append(list(messages))
                    if len(captured_prompts) == 1:
                        return Message(
                            role="assistant",
                            content="",
                            tool_calls=[
                                ToolCall(
                                    id="tc_sk_read",
                                    name="get_skill",
                                    args={"slug": "expense-audit-report"},
                                )
                            ],
                        )
                    return Message(role="assistant", content="已按 SOP 完成分析。")

            app = AgentRuntimeApp(
                model=TwoStepSkillModel(),
                store=store,
                extensions=[SkillsExtension(inline_limit=2)],
            )
            emp = AuthScope("t_acme", "u_emp", ("employee",))
            exec_scope = AuthScope("t_acme", "u_exec", ("exec",))

            # 0 skills -> truly empty message
            ctx_emp = ExecutionContext(uid("tr"), "th_sk_empty", emp, uid("req"))
            empty_res = await app.gateway.execute_one(ToolCall("tc_e0", "list_skills", {"query": "差旅"}), ctx_emp)
            self.assertIn("暂无可用 Skill", empty_res.content)

            store.publish_skill(
                scope=emp,
                slug="expense-audit-report",
                description="差旅超标分析、制度核验、ECharts 图表输出与工作区报告归档 SOP。",
                content="# 差旅超标 SOP",
                acl_roles=["employee"],
                tags=["finance", "report"],
            )
            store.publish_skill(
                scope=exec_scope,
                slug="exec-layoff-plan",
                description="高管保密组织调整方案",
                content="# 绝密",
                acl_roles=["exec"],
                tags=["exec"],
            )

            # 1. Non-contiguous Chinese query "差旅分析" hits "差旅超标分析" via bigram tokenizer
            hit = await app.gateway.execute_one(ToolCall("tc_q1", "list_skills", {"query": "差旅分析"}), ctx_emp)
            self.assertIn("expense-audit-report", hit.content)
            self.assertNotIn("exec-layoff-plan", hit.content)

            # 2. Unmatched query does NOT falsely claim no available skills; returns fallback catalog
            miss = await app.gateway.execute_one(ToolCall("tc_q2", "list_skills", {"query": "量子物理"}), ctx_emp)
            self.assertNotIn("暂无可用 Skill", miss.content)
            self.assertIn("未精确匹配", miss.content)
            self.assertIn("expense-audit-report", miss.content)
            self.assertNotIn("exec-layoff-plan", miss.content)

            # 3. Two-step turn: Layer-1 [AVAILABLE_SKILLS] injected with strict ACL + identical Prefix Cache across steps
            res = await app.run_turn("帮我做一份差旅分析报告", thread_id="th_sk_cache", scope=emp)
            self.assertEqual(res["final_answer"], "已按 SOP 完成分析。")
            self.assertEqual(len(captured_prompts), 2)
            step1_msgs, step2_msgs = captured_prompts
            self.assertIn("[AVAILABLE_SKILLS]", step1_msgs[1].content)
            self.assertIn("expense-audit-report", step1_msgs[1].content)
            self.assertNotIn("exec-layoff-plan", step1_msgs[1].content)
            # Prefix Cache invariant: system + catalog prefix is bit-for-bit identical on Step 1 & Step 2
            self.assertEqual(step1_msgs[0].content, step2_msgs[0].content)
            self.assertEqual(step1_msgs[1].content, step2_msgs[1].content)
            # Transient only: catalog never persisted in session_messages
            persisted = store.list_messages("th_sk_cache")
            self.assertFalse(any("[AVAILABLE_SKILLS]" in (m.content or "") for m in persisted))

            # 4. Scope without `get_skill` in allowed_tools (e.g. read-only BI subagent) skips catalog injection
            sub_scope = AuthScope("t_acme", "u_emp", ("employee",), allowed_tools=("read",))
            sub_ctx = ExecutionContext(uid("tr"), "th_sk_sub", sub_scope, uid("req"))
            sub_extras = await app.middlewares.run_extra_context("查差旅", sub_ctx)
            self.assertEqual(sub_extras, [])

            # 5. Exceeding inline_limit degrades cleanly to summary hint instead of dumping full catalog
            store.publish_skill(scope=emp, slug="skill-b", description="B", content="B", acl_roles=["employee"])
            store.publish_skill(scope=emp, slug="skill-c", description="C", content="C", acl_roles=["employee"])
            big_extras = await app.middlewares.run_extra_context("查差旅", ctx_emp)
            self.assertEqual(len(big_extras), 1)
            self.assertIn("共有 3 个可用平台技能", big_extras[0])
            self.assertNotIn("expense-audit-report", big_extras[0])

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
