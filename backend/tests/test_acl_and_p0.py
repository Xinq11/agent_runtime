"""P0 security regression tests & Phase 1 pluggable ACL contract tests."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from agent_runtime import (
    AgentRuntimeApp,
    ApprovalDecision,
    AuthScope,
    ExecutionContext,
    HeaderIdentityResolver,
    NoopAccessController,
    RAGChunk,
    RagExtension,
    RagRetrievalService,
    RbacAccessController,
    RiskLevel,
    SkillsExtension,
    ToolCall,
    tool,
)
from agent_runtime.api import approvals, ops, skills, state, system
from agent_runtime.framework.core.config import ModelSettings, uid
from agent_runtime.infra.llm.mock import MockScriptedLLM
from agent_runtime.infra.plg.hub import ObservabilityHub
from tests._pg_harness import get_test_store, reset_test_db, validate_test_db_name


class _TestTelemetry(ObservabilityHub):
    def __init__(self, store) -> None:
        super().__init__(store)
        self.prom = CollectorRegistry()
        self.loki_events = []

    async def trace_logs(self, trace_id):
        return []


class BrokenAccessController:
    """Simulates a failing remote policy engine (OPA/IAM timeout) to verify fail-closed."""

    def check(self, scope, action, resource):
        raise RuntimeError("policy_engine_timeout")

    def filter_for(self, scope, action, resource_kind):
        raise RuntimeError("policy_engine_timeout")

    def policy_rev(self, scope) -> int:
        raise RuntimeError("policy_engine_timeout")


def _make_http_app(runtime: AgentRuntimeApp, *, resolver=None) -> FastAPI:
    app = FastAPI()
    app.state.runtime = runtime
    app.state.store = runtime.store
    app.state.obs = runtime.obs
    app.state.model = runtime.model
    app.state.graph = runtime.graph
    if resolver is not None:
        app.state.identity_resolver = resolver
    v1 = APIRouter()
    for sub in (system.router, approvals.router, state.router, skills.router, ops.router):
        v1.include_router(sub)
    app.include_router(v1, prefix="/api/v1")
    return app


class TestPluggableAclAndP0Fixes(unittest.TestCase):
    def setUp(self) -> None:
        reset_test_db()

    def test_p0_subagent_empty_intersection_denies_visibility_and_execution(self):
        async def _run():
            @tool(risk_level=RiskLevel.READ)
            async def alpha(q: str) -> str:
                return q

            @tool(risk_level=RiskLevel.READ)
            async def beta(q: str) -> str:
                return q

            app = AgentRuntimeApp(store=get_test_store(), tools=[alpha, beta])

            # Parent restricted to ("alpha",), subagent asks for ("beta",) -> empty intersection ()
            parent = AuthScope(tenant_id="t1", user_id="u1", roles=("employee",), allowed_tools=("alpha",))
            sub = parent.narrowed(allowed_tools=("beta",), allow_write=False)
            self.assertEqual(sub.allowed_tools, ())
            self.assertEqual(app.registry.visible_specs(sub), [])

            sub_ctx = ExecutionContext(
                trace_id=uid("tr"),
                thread_id="th_sub_empty",
                request_id=uid("req"),
                auth_scope=sub,
                depth=1,
            )
            res = await app.gateway.execute_one(ToolCall("tc_1", "alpha", {"q": "hi"}), sub_ctx)
            self.assertFalse(res.ok)
            self.assertEqual(res.error_code, "permission_denied")

            # Explicit empty allowlist at depth=0 also grants zero tools
            zero_scope = AuthScope(tenant_id="t1", user_id="u1", allowed_tools=())
            self.assertEqual(app.registry.visible_specs(zero_scope), [])
            # Default allowed_tools=None grants read tools
            open_scope = AuthScope(tenant_id="t1", user_id="u1")
            self.assertIn("alpha", [s.name for s in app.registry.visible_specs(open_scope)])

        asyncio.run(_run())

    def test_p0_empty_roles_semantics_for_rag_and_skills(self):
        async def _run():
            store = get_test_store()
            rag_rbac = RagRetrievalService(
                store, min_rerank_score=0.35, access_controller=RbacAccessController(admin_roles=("admin",))
            )
            rag_noop = RagRetrievalService(
                store, min_rerank_score=0.35, access_controller=NoopAccessController()
            )
            rag_rbac.index_chunk(
                RAGChunk(
                    chunk_id="pub_1",
                    parent_id="p_pub",
                    tenant_id="t_acme",
                    doc_title="公开手册",
                    section_path="总则",
                    doc_url="kb://pub",
                    content="公司门禁卡补办流程说明",
                    parent_text="公司门禁卡补办流程说明",
                    acl_roles=(),
                )
            )
            rag_rbac.index_chunk(
                RAGChunk(
                    chunk_id="sec_1",
                    parent_id="p_sec",
                    tenant_id="t_acme",
                    doc_title="高管机密",
                    section_path="薪酬",
                    doc_url="kb://sec",
                    content="高管期权激励方案细则",
                    parent_text="高管期权激励方案细则",
                    acl_roles=("exec",),
                )
            )
            rag_rbac.index_chunk(
                RAGChunk(
                    chunk_id="other_1",
                    parent_id="p_other",
                    tenant_id="t_other",
                    doc_title="他租户资料",
                    section_path="总则",
                    doc_url="kb://other",
                    content="高管期权激励方案细则",
                    parent_text="高管期权激励方案细则",
                    acl_roles=(),
                )
            )

            admin = AuthScope("t_acme", "u_admin", ("admin",))
            store.publish_skill(
                scope=admin,
                slug="public-guide",
                description="全员公开流程",
                content="# 公开内容",
                acl_roles=[],
            )
            store.publish_skill(
                scope=admin,
                slug="exec-secret",
                description="高管专属流程",
                content="# 机密内容",
                acl_roles=["exec"],
            )

            no_role_scope = AuthScope("t_acme", "u_guest", ())

            # Under RBAC: empty caller roles sees ONLY public docs/skills, never exec-only!
            pub_hit = await rag_rbac.retrieve(scope=no_role_scope, query="门禁卡补办流程")
            self.assertEqual(pub_hit.status, "ok")
            sec_hit = await rag_rbac.retrieve(scope=no_role_scope, query="高管期权激励方案")
            self.assertEqual(sec_hit.hits, [])

            rbac_flt = RbacAccessController(admin_roles=("admin",)).filter_for(
                no_role_scope, "skill:read", "skill"
            )
            visible_slugs = [s.slug for s in store.list_skills(no_role_scope, access_filter=rbac_flt)]
            self.assertEqual(visible_slugs, ["public-guide"])

            # Under Noop: sees exec-only within tenant, NEVER cross-tenant
            noop_hit = await rag_noop.retrieve(scope=no_role_scope, query="高管期权激励方案")
            self.assertEqual(noop_hit.status, "ok")
            self.assertIn("p_sec", [h.parent_id for h in noop_hit.hits])
            self.assertTrue(all(h.tenant_id == "t_acme" for h in noop_hit.hits))

        asyncio.run(_run())

    def test_p0_http_include_disabled_ops_and_state_acl_gating(self):
        store = get_test_store()
        obs = _TestTelemetry(store)
        rbac = RbacAccessController(admin_roles=("admin",))
        model = MockScriptedLLM(obs=obs)
        model.config = ModelSettings(api_key="", mode="mock")
        runtime = AgentRuntimeApp(
            model=model,
            store=store,
            obs=obs,
            extensions=[RagExtension(), SkillsExtension()],
            access_controller=rbac,
        )
        admin_scope = AuthScope("t_acme", "u_admin", ("admin",))
        store.publish_skill(
            scope=admin_scope,
            slug="secret-sop",
            description="高管机密 SOP",
            content="# 绝密流程",
            acl_roles=["exec"],
        )
        store.set_skill_status(admin_scope, "secret-sop", "disabled")
        doc = store.index_document(admin_scope, "高管密件", "仅高管可见内容", ["exec"])
        store.index_document(admin_scope, "全员公告", "全员公开内容", [])

        emp_headers = {"X-Tenant-Id": "t_acme", "X-User-Id": "u_emp", "X-User-Roles": "employee"}
        admin_headers = {"X-Tenant-Id": "t_acme", "X-User-Id": "u_admin", "X-User-Roles": "admin"}
        client = TestClient(_make_http_app(runtime))

        # Non-admin MUST NOT bypass ACL using ?include_disabled=true
        r_bypass = client.get("/api/v1/skills/secret-sop?include_disabled=true", headers=emp_headers)
        self.assertEqual(r_bypass.status_code, 403)
        self.assertEqual(client.get("/api/v1/ops/overview", headers=emp_headers).status_code, 403)
        self.assertEqual(
            client.post("/api/v1/config/model", headers=emp_headers, json={"mode": "mock"}).status_code, 403
        )
        self.assertEqual(
            client.patch(
                f"/api/v1/knowledge/{doc['parent_id']}/status",
                headers=emp_headers,
                json={"status": "disabled"},
            ).status_code,
            403,
        )
        self.assertEqual(
            client.patch(
                "/api/v1/skills/secret-sop/status", headers=emp_headers, json={"status": "active"}
            ).status_code,
            403,
        )

        # /state for employee must NOT leak exec-only knowledge chunks
        r_state = client.get("/api/v1/state", headers=emp_headers)
        self.assertEqual(r_state.status_code, 200)
        titles = {c["doc_title"] for c in r_state.json()["knowledge_chunks"]}
        self.assertIn("全员公告", titles)
        self.assertNotIn("高管密件", titles)

        # Admin succeeds on disabled preview, ops overview (with real acl_roles, NO process-global logs), and status toggle
        obs.loki_events.append({"msg": "tenant_b_secret_log"})
        r_admin_sk = client.get("/api/v1/skills/secret-sop?include_disabled=true", headers=admin_headers)
        self.assertEqual(r_admin_sk.status_code, 200)
        self.assertIn("绝密流程", r_admin_sk.json()["content"])

        # Tenant admin cannot mutate shared process model config or read process-global Loki logs
        self.assertEqual(
            client.post("/api/v1/config/model", headers=admin_headers, json={"mode": "mock"}).status_code,
            403,
        )
        r_ops = client.get("/api/v1/ops/overview", headers=admin_headers)
        self.assertEqual(r_ops.status_code, 200)
        self.assertEqual(r_ops.json()["loki_logs"], [])
        self.assertEqual(r_ops.json()["metrics_text"], "")
        kb_by_title = {k["doc_title"]: k for k in r_ops.json()["knowledge_chunks"]}
        self.assertEqual(kb_by_title["高管密件"]["acl_roles"], ["exec"])

        # Platform admin CAN read process telemetry and update model config
        plat_headers = {
            "X-Tenant-Id": "t_acme",
            "X-User-Id": "u_plat",
            "X-User-Roles": "platform_admin",
        }
        r_plat_ops = client.get("/api/v1/ops/overview", headers=plat_headers)
        self.assertEqual(r_plat_ops.status_code, 200)
        self.assertEqual(len(r_plat_ops.json()["loki_logs"]), 1)
        self.assertEqual(
            client.post("/api/v1/config/model", headers=plat_headers, json={"mode": "mock"}).status_code,
            200,
        )

    def test_p0_hmac_gateway_headers_cross_user_approval_and_server_approver_id(self):
        store = get_test_store()
        obs = ObservabilityHub(store)
        writes = []

        @tool(risk_level=RiskLevel.HIGH_RISK)
        async def submit_payout(amount: int, ctx: ExecutionContext) -> str:
            writes.append((amount, ctx.auth_scope.user_id))
            return f"PAID:{amount}"

        class OneShotModel:
            def __init__(self):
                self.called = 0

            async def generate(self, *, messages, tool_specs, ctx):
                from agent_runtime.framework.core.contracts import Message

                self.called += 1
                if self.called == 1:
                    return Message("assistant", "", [ToolCall("tc_pay", "submit_payout", {"amount": 900})])
                return Message("assistant", "已发放 900 元")

        rbac = RbacAccessController(
            admin_roles=("admin",),
            approver_roles=("manager",),
            forbid_self_approval=True,
        )
        runtime = AgentRuntimeApp(
            model=OneShotModel(),
            store=store,
            obs=obs,
            tools=[submit_payout],
            access_controller=rbac,
        )
        secret = "unit-test-gateway-secret"
        resolver = HeaderIdentityResolver(hmac_secret=secret)
        client = TestClient(_make_http_app(runtime, resolver=resolver))

        # Unsigned / tampered request -> 401
        self.assertEqual(
            client.get(
                "/api/v1/meta",
                headers={"X-Tenant-Id": "t_acme", "X-User-Id": "u_alice", "X-User-Roles": "employee"},
            ).status_code,
            401,
        )

        # Start paused turn as employee u_alice
        emp_scope = AuthScope("t_acme", "u_alice", ("employee",), authenticated=True)
        paused = asyncio.run(runtime.run_turn("发放 900 元", thread_id="th_pay", scope=emp_scope))
        review_id = paused["__interrupt__"][0]["review_id"]

        # Self-approval as u_alice (even spoofing body.approver_id="mgr_bob") -> 403!
        alice_hdr = HeaderIdentityResolver.sign_headers(
            secret=secret, tenant_id="t_acme", user_id="u_alice", roles=("employee", "manager")
        )
        r_self = client.post(
            f"/api/v1/approvals/{review_id}/resume",
            headers=alice_hdr,
            json={"approved": True, "thread_id": "th_pay", "approver_id": "spoofed_ceo"},
        )
        self.assertEqual(r_self.status_code, 403)
        self.assertEqual(writes, [])

        # Authorized manager u_boss approves while spoofing body.approver_id -> recorded as u_boss!
        boss_hdr = HeaderIdentityResolver.sign_headers(
            secret=secret, tenant_id="t_acme", user_id="u_boss", roles=("manager",)
        )
        r_ok = client.post(
            f"/api/v1/approvals/{review_id}/resume",
            headers=boss_hdr,
            json={"approved": True, "thread_id": "th_pay", "approver_id": "spoofed_ceo"},
        )
        self.assertEqual(r_ok.status_code, 200)
        self.assertEqual(r_ok.json()["status"], "completed")
        self.assertEqual(writes, [(900, "u_alice")])
        ticket = store.get_approval_ticket(review_id, "t_acme")
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.approver_id, "u_boss")

    def test_phase1_policy_rev_invalidation_and_fail_closed_controller(self):
        async def _run():
            store = get_test_store()
            obs = ObservabilityHub(store)
            writes = []

            @tool(risk_level=RiskLevel.HIGH_RISK)
            async def wire_transfer(amount: int) -> str:
                writes.append(amount)
                return "OK"

            class WireModel:
                def __init__(self):
                    self.n = 0

                async def generate(self, *, messages, tool_specs, ctx):
                    from agent_runtime.framework.core.contracts import Message

                    self.n += 1
                    if self.n == 1:
                        return Message("assistant", "", [ToolCall("tc_w", "wire_transfer", {"amount": 500})])
                    return Message("assistant", "done")

            rbac = RbacAccessController(admin_roles=("admin",))
            app = AgentRuntimeApp(
                model=WireModel(), store=store, obs=obs, tools=[wire_transfer], access_controller=rbac
            )
            scope = AuthScope("t_acme", "u_alice", ("employee",))
            paused = await app.run_turn("转账", thread_id="th_rev", scope=scope)
            review_id = paused["__interrupt__"][0]["review_id"]

            # Bump policy_rev while paused -> resume MUST NOT execute wire_transfer!
            rbac.bump_policy_rev()
            res = await app.resume_approval(
                thread_id="th_rev",
                decision=ApprovalDecision(review_id=review_id, approved=True, approver_id="u_alice"),
                scope=scope,
            )
            self.assertEqual(writes, [])
            self.assertTrue(res.get("final_answer"))

            # Broken controller -> fail-closed everywhere (never falls back to Noop)
            broken = BrokenAccessController()
            rag_broken = RagRetrievalService(store, access_controller=broken)
            rag_res = await rag_broken.retrieve(scope=scope, query="任何内容")
            self.assertEqual(rag_res.status, "acl_error")

            app_broken = AgentRuntimeApp(store=store, obs=obs, access_controller=broken)
            ctx = ExecutionContext(uid("tr"), "th_brk", scope, uid("req"))
            tool_res = await app_broken.gateway.execute_one(ToolCall("tc_b", "read", {"path": "."}), ctx)
            self.assertFalse(tool_res.ok)
            self.assertEqual(tool_res.error_code, "acl_error")

            http_client = TestClient(_make_http_app(app_broken))
            self.assertEqual(http_client.get("/api/v1/ops/overview").status_code, 503)
            self.assertEqual(http_client.get("/api/v1/meta").status_code, 503)

        asyncio.run(_run())

    def test_p0_pg_harness_db_guard_and_run_turn_start_failure_cleanup(self):
        for bad in ("agent_runtime", "postgres", "template0", "template1", "", "prod_db"):
            with self.assertRaises(RuntimeError):
                validate_test_db_name(bad)
        self.assertEqual(validate_test_db_name("agent_runtime_test"), "agent_runtime_test")
        self.assertEqual(validate_test_db_name("test_custom"), "test_custom")

        async def _run():
            store = get_test_store()
            app = AgentRuntimeApp(store=store)
            scope = AuthScope("t_acme", "u_alice", ("employee",))

            with patch.object(store, "record_turn_start", side_effect=RuntimeError("db_write_blip")):
                with self.assertRaises(RuntimeError):
                    await app.run_turn("hello", thread_id="th_fail_start", scope=scope)

        # Run must have converged to failed so next turn is NOT blocked with 409 conflict
            self.assertIsNone(store.get_active_run("th_fail_start"))
            ok_turn = await app.run_turn("hello again", thread_id="th_fail_start", scope=scope)
            self.assertTrue(ok_turn["final_answer"])

        asyncio.run(_run())

    def test_p0_mixed_acl_parent_unsupported_filter_signature_boundaries_and_sdk_approver(self):
        from agent_runtime import (
            AccessControlError,
            AccessDecision,
            AccessFilter,
            AuthenticationError,
        )
        from agent_runtime.infra.pg.acl_sql import compile_acl_sql

        async def _run():
            store = get_test_store()
            # 1. Mixed-ACL parent doc: public chunk + exec-only chunk share same parent_id
            rag = RagRetrievalService(
                store, min_rerank_score=0.35, access_controller=RbacAccessController(admin_roles=("admin",))
            )
            full_parent = "公开章节：普通差旅每天补助 180 元。\n绝密章节：CEO 特别津贴 99999 元。"
            rag.index_chunk(
                RAGChunk(
                    chunk_id="mix_1",
                    parent_id="p_mix",
                    tenant_id="t_acme",
                    doc_title="混合制度",
                    section_path="补助",
                    doc_url="kb://mix",
                    content="公开章节：普通差旅每天补助 180 元。",
                    parent_text=full_parent,
                    acl_roles=(),
                )
            )
            rag.index_chunk(
                RAGChunk(
                    chunk_id="mix_2",
                    parent_id="p_mix",
                    tenant_id="t_acme",
                    doc_title="混合制度",
                    section_path="高管",
                    doc_url="kb://mix",
                    content="绝密章节：CEO 特别津贴 99999 元。",
                    parent_text=full_parent,
                    acl_roles=("exec",),
                )
            )
            emp = AuthScope("t_acme", "u_emp", ("employee",))
            res_emp = await rag.retrieve(scope=emp, query="普通差旅每天补助")
            self.assertEqual(res_emp.status, "ok")
            self.assertIn("180", res_emp.formatted_context)
            self.assertNotIn("99999", res_emp.formatted_context)
            self.assertNotIn("CEO 特别津贴", res_emp.formatted_context)

            # 2. Unsupported AccessFilter predicates MUST fail closed (never silently widen)
            with self.assertRaises(AccessControlError):
                compile_acl_sql(AccessFilter(tenant_id="t_acme", owner_id="u_other"), scope=emp)
            with self.assertRaises(AccessControlError):
                compile_acl_sql(AccessFilter(tenant_id="t_acme", any_depts=("d_fin",)), scope=emp)
            with self.assertRaises(AccessControlError):
                compile_acl_sql(AccessFilter(tenant_id="t_other"), scope=emp)

            # 3. Signed gateway header encoding is unambiguous & rejects control/delimiter abuse
            secret = "strict-hmac-secret"
            resolver = HeaderIdentityResolver(hmac_secret=secret)
            hdr1 = HeaderIdentityResolver.sign_headers(
                secret=secret, tenant_id="a|b", user_id="c", roles=("employee",)
            )
            tampered = {**hdr1, "X-Tenant-Id": "a", "X-User-Id": "b|c"}
            with self.assertRaises(AuthenticationError):
                resolver.resolve(tampered)
            with self.assertRaises(AuthenticationError):
                HeaderIdentityResolver.sign_headers(
                    secret=secret, tenant_id="t_acme", user_id="u1", roles=("a,b",)
                )
            with self.assertRaises(AuthenticationError):
                HeaderIdentityResolver.sign_headers(
                    secret=secret, tenant_id="t_acme\n", user_id="u1", roles=("employee",)
                )

            # 4. SDK resume_approval stamps authoritative caller.user_id (ignoring spoofed decision.approver_id)
            @tool(risk_level=RiskLevel.HIGH_RISK)
            async def approve_me(x: int) -> str:
                return f"ok:{x}"

            class OnceModel:
                def __init__(self):
                    self.i = 0

                async def generate(self, *, messages, tool_specs, ctx):
                    from agent_runtime.framework.core.contracts import Message

                    self.i += 1
                    if self.i == 1:
                        return Message("assistant", "", [ToolCall("tc_a1", "approve_me", {"x": 1})])
                    return Message("assistant", "done")

            class DenyArtifactController(RbacAccessController):
                def check(self, scope, action, resource):
                    if action == "artifact:read":
                        return AccessDecision(False, "artifact_blocked")
                    return super().check(scope, action, resource)

            ac = DenyArtifactController(admin_roles=("admin",), approver_roles=("manager",))
            app = AgentRuntimeApp(model=OnceModel(), store=store, tools=[approve_me], access_controller=ac)
            paused = await app.run_turn("approve", thread_id="th_sdk_app", scope=emp)
            rid = paused["__interrupt__"][0]["review_id"]
            mgr = AuthScope("t_acme", "u_real_mgr", ("manager",))
            await app.resume_approval(
                thread_id="th_sdk_app",
                decision=ApprovalDecision(review_id=rid, approved=True, approver_id="spoofed_id"),
                scope=mgr,
            )
            tkt = store.get_approval_ticket(rid, "t_acme")
            self.assertIsNotNone(tkt)
            self.assertEqual(tkt.approver_id, "u_real_mgr")

            # 5. Builtin read_tool_output enforces artifact:read consistently with HTTP /ops/outputs
            ctx = ExecutionContext(uid("tr"), "th_sdk_app", emp, uid("req"))
            art_res = await app.gateway.execute_one(
                ToolCall("tc_art", "read_tool_output", {"ref_id": "out_any"}), ctx
            )
            self.assertFalse(art_res.ok)
            self.assertEqual(art_res.error_code, "permission_denied")

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
