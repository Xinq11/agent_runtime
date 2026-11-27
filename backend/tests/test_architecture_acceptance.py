"""Acceptance test suite locking down every invariant in `ARCHITECTURE_REVIEW.md`:

1. AST reverse-dependency guard: core `framework/**`, `infra/**`, and core `api/*` NEVER import `extensions.*`.
2. Pure SDK zero-RAG/Skills/vector/Ops acceptance: fresh DB with NO `vector` extension and NO RAG/Skills tables
   runs declarative `AgentPlugin.subagents()`, `HIGH_RISK` pause/approve/reject/cancel, duplicate-approval
   idempotency, and continuing conversation on the same thread after cancel.
3. Cross-user ownership isolation across BOTH `default` and `noop` access controllers for `thread_id`,
   `run_id`, `trace_id`, `ref_id`, and `approval:decide`.
4. Customer A (official RAG/Skills extensions) vs Customer B (custom external knowledge/skill tools)
   sharing the exact same SDK version with strict visibility isolation.
5. Sub-agent immediate cancellation response.
"""

from __future__ import annotations

import ast
import asyncio
import os
from pathlib import Path
from typing import Any, Dict, List
import unittest

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from prometheus_client import CollectorRegistry
import psycopg
from psycopg import conninfo, sql

from agent_runtime import (
    AgentPlugin,
    AgentRuntimeApp,
    ApprovalDecision,
    AuthScope,
    AuthorizationError,
    ExecutionContext,
    Message,
    NoopAccessController,
    PgRagRepository,
    PgSkillsRepository,
    RagExtension,
    RbacAccessController,
    RiskLevel,
    SkillsExtension,
    SubAgentProfile,
    ToolCall,
    tool,
)
from agent_runtime.api import approvals, chat_stream, state, system, threads
from agent_runtime.api.main import create_app
from agent_runtime.framework.core.config import uid
from agent_runtime.framework.core.runs import RunStateError
from agent_runtime.infra.oss.minio import MinioObjectStore
from agent_runtime.infra.pg.postgres import DEFAULT_PG_DSN, PgOssStore
from agent_runtime.infra.plg.hub import ObservabilityHub
from tests._pg_harness import TEST_BUCKET, get_test_store, reset_test_db


class _QuietObs(ObservabilityHub):
    def __init__(self, store) -> None:
        super().__init__(store)
        self.prom = CollectorRegistry()
        self.loki_events = []

    async def trace_logs(self, trace_id):
        return []


def _http_client(runtime: AgentRuntimeApp) -> TestClient:
    app = FastAPI()
    app.state.runtime = runtime
    app.state.store = runtime.store
    app.state.obs = runtime.obs
    app.state.model = runtime.model
    app.state.graph = runtime.graph
    v1 = APIRouter()
    for sub in (system.router, threads.router, chat_stream.router, approvals.router, state.router):
        v1.include_router(sub)
    for r in runtime.extension_routers():
        v1.include_router(r)
    app.include_router(v1, prefix="/api/v1")
    return TestClient(app)


class TestArchitectureAcceptance(unittest.TestCase):
    def setUp(self) -> None:
        reset_test_db()

    def test_01_ast_core_has_zero_reverse_dependency_on_extensions(self):
        pkg_root = Path(__file__).resolve().parents[1] / "agent_runtime"
        core_paths = (
            list((pkg_root / "framework").rglob("*.py"))
            + list((pkg_root / "infra").rglob("*.py"))
            + [
                pkg_root / "api" / name
                for name in ("main.py", "chat_stream.py", "threads.py", "approvals.py", "state.py", "system.py", "deps.py", "schemas.py")
            ]
        )
        violations: List[str] = []
        for path in core_paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith("agent_runtime.extensions"):
                            violations.append(f"{path}:{node.lineno} imports {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    if mod.startswith("agent_runtime.extensions"):
                        violations.append(f"{path}:{node.lineno} imports from {mod}")
        self.assertEqual(violations, [], f"Core reverse dependency detected: {violations}")

    def test_02_pure_sdk_zero_rag_skills_tables_no_vector_no_ops(self):
        core_db = "agent_runtime_core_only_test"
        base_dsn = os.getenv("PG_DSN", DEFAULT_PG_DSN)
        admin_dsn = conninfo.make_conninfo(base_dsn, dbname="postgres")
        core_dsn = conninfo.make_conninfo(base_dsn, dbname=core_db)

        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid <> pg_backend_pid()",
                    (core_db,),
                )
                cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(core_db)))
                cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(core_db)))

        oss = MinioObjectStore(bucket=TEST_BUCKET)
        oss.ensure_bucket()
        core_store = PgOssStore(core_dsn, oss=oss)
        core_store.open_pool(min_size=1, max_size=4)
        try:
            core_store.bootstrap()

            # Assert ZERO vector extension and ZERO RAG/Skills tables exist in this fresh core-only DB
            ext_rows = core_store.rows("SELECT extname FROM pg_extension WHERE extname='vector'")
            self.assertEqual(ext_rows, [])
            for tbl in ("rag_parents", "rag_chunks", "agent_skills", "agent_skill_versions"):
                self.assertIsNone(core_store.rows("SELECT to_regclass(%s) AS t", (tbl,))[0]["t"])

            # Pure SDK FastAPI app mounts ZERO RAG/Skills/Ops routes by default
            pure_http = create_app(extensions=[])
            paths = set(pure_http.openapi()["paths"].keys())
            self.assertFalse(any("/skills" in p or "/knowledge" in p or "/ops" in p for p in paths))

            writes: List[str] = []

            class PureCustomerPlugin(AgentPlugin):
                name = "pure_customer"

                def register_tools(self, *, registry, store, model, gateway, obs) -> None:
                    @tool(risk_level=RiskLevel.READ)
                    async def fetch_metric(name: str) -> str:
                        return f"metric:{name}=42"

                    @tool(risk_level=RiskLevel.HIGH_RISK)
                    async def execute_transfer(order_id: str) -> str:
                        writes.append(order_id)
                        return f"transferred:{order_id}"

                    registry.register(fetch_metric)
                    registry.register(execute_transfer)

                def subagents(self):
                    return (
                        SubAgentProfile(
                            subagent_name="metric_analyst",
                            description="只读指标子代理",
                            system_prompt="查指标并简洁回报",
                            allowed_tools=("fetch_metric",),
                        ),
                    )

            class ScriptedPureModel:
                async def generate(self, *, messages, tool_specs, ctx):
                    tool_msgs = [m for m in messages if m.role == "tool"]
                    user_msgs = [m.content for m in messages if m.role == "user"]
                    last_user = user_msgs[-1] if user_msgs else ""
                    if ctx.agent_name == "metric_analyst":
                        if not tool_msgs:
                            return Message("assistant", "", [ToolCall(uid("tc"), "fetch_metric", {"name": "rev"})])
                        return Message("assistant", f"子代理回报:{tool_msgs[-1].content}")
                    if "RUN_FLOW" in last_user:
                        order_id = last_user.split(":")[1]
                        if not tool_msgs:
                            return Message(
                                "assistant",
                                "",
                                [ToolCall(uid("tc"), "call_metric_analyst", {"goal": "查 rev"})],
                            )
                        if not any(m.name == "execute_transfer" for m in tool_msgs):
                            return Message(
                                "assistant",
                                "",
                                [ToolCall(uid("tc"), "execute_transfer", {"order_id": order_id})],
                            )
                        return Message("assistant", f"结果:{tool_msgs[-1].content}")
                    return Message("assistant", f"继续对话成功:{last_user}")

            async def _run():
                app = AgentRuntimeApp(
                    model=ScriptedPureModel(),
                    store=core_store,
                    obs=_QuietObs(core_store),
                    checkpointer=InMemorySaver(),
                    extensions=[PureCustomerPlugin()],
                )
                app.bootstrap_extensions()
                alice = AuthScope("u_alice", ("employee",))

                # 1. Subagent delegation + HIGH_RISK pause -> approve -> resume (1 write); duplicate approve fails & no 2nd write
                p1 = await app.run_turn("RUN_FLOW:ORD-1", thread_id="th_core_1", scope=alice)
                self.assertIn("__interrupt__", p1)
                self.assertEqual(writes, [])
                rid1 = p1["__interrupt__"][0]["review_id"]
                done1 = await app.resume_approval(
                    thread_id="th_core_1",
                    decision=ApprovalDecision(rid1, True, "u_alice"),
                    scope=alice,
                )
                self.assertIn("transferred:ORD-1", done1["final_answer"])
                self.assertEqual(writes, ["ORD-1"])
                with self.assertRaises(RunStateError):
                    await app.resume_approval(
                        thread_id="th_core_1",
                        decision=ApprovalDecision(rid1, True, "u_alice"),
                        scope=alice,
                    )
                self.assertEqual(writes, ["ORD-1"])

                # 2. HIGH_RISK pause -> reject -> skips execution (0 new writes)
                p2 = await app.run_turn("RUN_FLOW:ORD-2", thread_id="th_core_2", scope=alice)
                rid2 = p2["__interrupt__"][0]["review_id"]
                done2 = await app.resume_approval(
                    thread_id="th_core_2",
                    decision=ApprovalDecision(rid2, False, "u_alice"),
                    scope=alice,
                )
                self.assertIn("approval_rejected", done2["final_answer"])
                self.assertEqual(writes, ["ORD-1"])

                # 3. HIGH_RISK pause -> cancel -> stale approve blocked AND same thread continues cleanly!
                p3 = await app.run_turn("RUN_FLOW:ORD-3", thread_id="th_core_3", scope=alice)
                rid3 = p3["__interrupt__"][0]["review_id"]
                await app.abort_run(scope=alice, run_id=p3["run_id"])
                with self.assertRaises(RunStateError):
                    await app.resume_approval(
                        thread_id="th_core_3",
                        decision=ApprovalDecision(rid3, True, "u_alice"),
                        scope=alice,
                    )
                self.assertEqual(writes, ["ORD-1"])
                # Continuing conversation on the EXACT SAME thread MUST work (no checkpoint deadlock!)
                continued = await app.run_turn("取消后继续聊聊", thread_id="th_core_3", scope=alice)
                self.assertEqual(continued["final_answer"], "继续对话成功:取消后继续聊聊")

            asyncio.run(_run())
        finally:
            core_store.close_pool()
            with psycopg.connect(admin_dsn, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid <> pg_backend_pid()",
                        (core_db,),
                    )
                    cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(core_db)))

    def test_03_cross_user_isolation_across_thread_run_trace_ref_and_approval(self):
        store = get_test_store()

        @tool(risk_level=RiskLevel.READ)
        async def big_dump() -> str:
            return "\n".join(f"SECRET_ROW_{i:04d}_DATA_PAYLOAD_XYZ" for i in range(250))

        @tool(risk_level=RiskLevel.HIGH_RISK)
        async def approve_action(item: str) -> str:
            return f"approved:{item}"

        class SpillThenApproveModel:
            async def generate(self, *, messages, tool_specs, ctx):
                tool_msgs = [m for m in messages if m.role == "tool"]
                if not tool_msgs:
                    return Message("assistant", "", [ToolCall(uid("tc"), "big_dump", {})])
                if not any(m.name == "approve_action" for m in tool_msgs):
                    return Message("assistant", "", [ToolCall(uid("tc"), "approve_action", {"item": "X"})])
                return Message("assistant", "done")

        for mode_name, ac in (
            ("default", RbacAccessController(admin_roles=None)),
            ("noop", NoopAccessController()),
        ):
            reset_test_db()
            obs = _QuietObs(store)
            runtime = AgentRuntimeApp(
                model=SpillThenApproveModel(),
                store=store,
                obs=obs,
                checkpointer=InMemorySaver(),
                tools=[big_dump, approve_action],
                access_controller=ac,
            )
            alice = AuthScope("u_alice", ("employee",))
            bob = AuthScope("u_bob", ("employee",))
            tid = f"th_iso_{mode_name}"
            ctx_a = ExecutionContext(uid("tr"), tid, alice, uid("req"))
            paused = asyncio.run(runtime.run_turn("导出并审批", ctx=ctx_a))
            run_id = paused["run_id"]
            review_id = paused["__interrupt__"][0]["review_id"]
            trace_id = ctx_a.trace_id
            ref_id = store.rows("SELECT ref_id FROM tool_outputs WHERE thread_id=%s", (tid,))[0]["ref_id"]

            client = _http_client(runtime)
            bob_h = {"X-User-Id": "u_bob", "X-User-Roles": "employee"}
            alice_h = {"X-User-Id": "u_alice", "X-User-Roles": "employee"}

            # Bob cannot read or mutate ANY of Alice's resources even when knowing every ID
            self.assertEqual(client.get(f"/api/v1/threads/{tid}/messages", headers=bob_h).status_code, 404, mode_name)
            self.assertEqual(client.get(f"/api/v1/state?thread_id={tid}", headers=bob_h).status_code, 404, mode_name)
            self.assertEqual(client.get(f"/api/v1/state?trace_id={trace_id}", headers=bob_h).status_code, 404, mode_name)
            self.assertEqual(client.get(f"/api/v1/runs/{run_id}", headers=bob_h).status_code, 404, mode_name)
            self.assertEqual(
                client.post("/api/v1/chat/abort", headers=bob_h, json={"run_id": run_id}).status_code, 404, mode_name
            )
            self.assertEqual(
                client.post(f"/api/v1/approvals/{review_id}/resume", headers=bob_h, json={"thread_id": tid, "approved": True}).status_code,
                404,
                mode_name,
            )
            self.assertEqual(client.get(f"/api/v1/outputs/{ref_id}", headers=bob_h).status_code, 404, mode_name)
            self.assertEqual(client.get(f"/api/v1/outputs/{ref_id}", headers=alice_h).status_code, 200, mode_name)

            # SDK-level cross-user checks also reject Bob
            with self.assertRaises(AuthorizationError):
                asyncio.run(
                    runtime.resume_approval(
                        thread_id=tid,
                        decision=ApprovalDecision(review_id, True, "u_bob"),
                        scope=bob,
                    )
                )
            page, err = store.read_tool_output_page(ExecutionContext(uid("tr"), "th_b", bob, uid("req")), ref_id)
            self.assertIsNone(page)
            self.assertEqual(err, "output_not_found")

            # Alice CAN read her own trace by trace_id alone and approve her own run
            st_a = client.get(f"/api/v1/state?trace_id={trace_id}", headers=alice_h)
            self.assertEqual(st_a.status_code, 200)
            self.assertEqual(st_a.json()["trace_id"], trace_id)
            self.assertNotIn("knowledge_chunks", st_a.json())

            ok_a = client.post(
                f"/api/v1/approvals/{review_id}/resume",
                headers=alice_h,
                json={"thread_id": tid, "approved": True},
            )
            self.assertEqual(ok_a.status_code, 200)

    def test_04_customer_a_official_extensions_vs_customer_b_custom_platform(self):
        async def _run():
            store = get_test_store()
            rbac = RbacAccessController(admin_roles=("admin",))

            # Customer A: uses official RagExtension + SkillsExtension
            app_a = AgentRuntimeApp(
                store=store,
                extensions=[RagExtension(), SkillsExtension()],
                access_controller=rbac,
            )
            app_a.bootstrap_extensions()
            admin = AuthScope("u_admin", ("admin",))
            emp = AuthScope("u_emp", ("employee",))
            exec_user = AuthScope("u_exec", ("exec",))

            PgRagRepository(store).index_document(admin, "董事会议题", "仅高管可见的收购方案详情", ["exec"])
            PgSkillsRepository(store).publish_skill(
                scope=admin, slug="exec-mna-sop", description="高管并购 SOP", content="# M&A", acl_roles=["exec"]
            )

            ctx_a_emp = ExecutionContext(uid("tr"), "th_a1", emp, uid("req"))
            ctx_a_exec = ExecutionContext(uid("tr"), "th_a2", exec_user, uid("req"))
            r_a_emp = await app_a.gateway.execute_one(ToolCall("c1", "search_knowledge", {"query": "收购方案"}), ctx_a_emp)
            r_a_exec = await app_a.gateway.execute_one(ToolCall("c2", "search_knowledge", {"query": "收购方案"}), ctx_a_exec)
            self.assertNotIn("收购方案详情", r_a_emp.content)
            self.assertIn("收购方案详情", r_a_exec.content)
            sk_a_emp = await app_a.gateway.execute_one(ToolCall("c3", "list_skills", {}), ctx_a_emp)
            sk_a_exec = await app_a.gateway.execute_one(ToolCall("c4", "list_skills", {}), ctx_a_exec)
            self.assertNotIn("exec-mna-sop", sk_a_emp.content)
            self.assertIn("exec-mna-sop", sk_a_exec.content)

            # Customer B: ZERO official RAG/Skills extensions, plugs own knowledge/skill platform
            corp_docs = [
                {"title": "员工手册", "body": "年假 15 天", "roles": {"employee", "exec"}},
                {"title": "财务底稿", "body": "毛利率 68%", "roles": {"exec"}},
            ]

            class CustomerBPlugin(AgentPlugin):
                name = "customer_b_platform"

                def register_tools(self, *, registry, store, model, gateway, obs) -> None:
                    @tool(risk_level=RiskLevel.READ)
                    async def query_internal_kb(keyword: str, ctx: ExecutionContext) -> str:
                        hits = [
                            f"{d['title']}: {d['body']}"
                            for d in corp_docs
                            if (set(ctx.auth_scope.roles) & d["roles"]) and keyword in (d["title"] + d["body"])
                        ]
                        return "\n".join(hits) if hits else "[NO_HIT]"

                    registry.register(query_internal_kb)

            app_b = AgentRuntimeApp(store=store, extensions=[CustomerBPlugin()])
            self.assertIsNone(app_b.registry.get("search_knowledge"))
            self.assertIsNone(app_b.registry.get("list_skills"))
            ctx_b_emp = ExecutionContext(uid("tr"), "th_b1", emp, uid("req"))
            ctx_b_exec = ExecutionContext(uid("tr"), "th_b2", exec_user, uid("req"))
            r_b_emp = await app_b.gateway.execute_one(ToolCall("c5", "query_internal_kb", {"keyword": "毛利率"}), ctx_b_emp)
            r_b_exec = await app_b.gateway.execute_one(ToolCall("c6", "query_internal_kb", {"keyword": "毛利率"}), ctx_b_exec)
            self.assertEqual(r_b_emp.content, "[NO_HIT]")
            self.assertIn("68%", r_b_exec.content)

        asyncio.run(_run())

    def test_05_subagent_checks_cancellation_between_steps(self):
        async def _run():
            store = get_test_store()
            cancelled = False
            calls = 0

            @tool(risk_level=RiskLevel.READ)
            async def slow_step() -> str:
                nonlocal cancelled
                cancelled = True
                return "step1_ok"

            class MultiStepSubModel:
                async def generate(self, *, messages, tool_specs, ctx):
                    nonlocal calls
                    calls += 1
                    return Message("assistant", "", [ToolCall(uid("tc"), "slow_step", {})])

            app = AgentRuntimeApp(
                model=MultiStepSubModel(),
                store=store,
                tools=[slow_step],
                subagents=[
                    SubAgentProfile(
                        subagent_name="researcher",
                        description="调研子代理",
                        system_prompt="调研",
                        allowed_tools=("slow_step",),
                        max_steps=4,
                    )
                ],
            )
            ctx = ExecutionContext(
                trace_id=uid("tr"),
                thread_id="th_sub_cancel",
                auth_scope=AuthScope("u_alice", ("employee",)),
                request_id=uid("req"),
                is_cancelled=lambda: cancelled,
            )
            with self.assertRaises(asyncio.CancelledError):
                await app.gateway.execute_one(ToolCall("tc_sub", "call_researcher", {"goal": "调研"}), ctx)
            self.assertEqual(calls, 1)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
