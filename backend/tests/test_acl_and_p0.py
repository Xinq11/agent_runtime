"""P0 security regression tests & minimal point-check AuthN/AuthZ contract tests."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from agent_runtime import (
    AccessDecision,
    AgentRuntimeApp,
    ApprovalDecision,
    AuthenticationError,
    AuthScope,
    ExecutionContext,
    HeaderIdentityResolver,
    Message,
    RbacAccessController,
    RiskLevel,
    ToolCall,
    tool,
)
from agent_runtime.api import approvals, state, system, threads
from agent_runtime.framework.core.config import ModelSettings, uid
from agent_runtime.infra.llm.provider import ModelNotConfiguredError, OpenAICompatibleProvider
from agent_runtime.infra.plg.hub import ObservabilityHub
from tests._pg_harness import EchoTestModel, get_test_store, reset_test_db, validate_test_db_name


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
    for sub in (system.router, threads.router, approvals.router, state.router):
        v1.include_router(sub)
    for ext_router in runtime.extension_routers():
        v1.include_router(ext_router)
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

            parent = AuthScope(user_id="u1", roles=("employee",), allowed_tools=("alpha",))
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

            zero_scope = AuthScope(user_id="u1", allowed_tools=())
            self.assertEqual(app.registry.visible_specs(zero_scope), [])
            open_scope = AuthScope(user_id="u1")
            self.assertIn("alpha", [s.name for s in app.registry.visible_specs(open_scope)])

        asyncio.run(_run())

    def test_p0_http_model_config_and_state_gating(self):
        store = get_test_store()
        obs = _TestTelemetry(store)
        rbac = RbacAccessController(admin_roles=("admin",))
        model = EchoTestModel()
        model.config = ModelSettings(api_key="sk-test", mode="deepseek")
        runtime = AgentRuntimeApp(model=model, store=store, obs=obs, access_controller=rbac)

        emp_headers = {"X-User-Id": "u_emp", "X-User-Roles": "employee"}
        admin_headers = {"X-User-Id": "u_admin", "X-User-Roles": "admin"}
        plat_headers = {"X-User-Id": "u_plat", "X-User-Roles": "platform_admin"}
        client = TestClient(_make_http_app(runtime))

        self.assertEqual(
            client.post("/api/v1/config/model", headers=emp_headers, json={"mode": "deepseek"}).status_code, 403
        )
        self.assertEqual(
            client.post("/api/v1/config/model", headers=admin_headers, json={"mode": "deepseek"}).status_code, 403
        )
        self.assertEqual(
            client.post(
                "/api/v1/config/model", headers=plat_headers, json={"mode": "deepseek", "model": "deepseek-chat"}
            ).status_code,
            200,
        )
        self.assertEqual(
            client.post("/api/v1/config/model", headers=plat_headers, json={"mode": "mock"}).status_code,
            422,
        )

        r_state = client.get("/api/v1/state", headers=emp_headers)
        self.assertEqual(r_state.status_code, 200)
        self.assertNotIn("knowledge_chunks", r_state.json())

        unconfigured = OpenAICompatibleProvider(None, obs, settings=ModelSettings(api_key=""))
        with self.assertRaises(ModelNotConfiguredError):
            asyncio.run(
                unconfigured.generate(
                    messages=[Message("user", "ping")],
                    tool_specs=[],
                    ctx=ExecutionContext(uid("tr"), uid("th"), AuthScope("u_admin", ("admin",)), uid("req")),
                )
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

        self.assertEqual(
            client.get(
                "/api/v1/meta",
                headers={"X-User-Id": "u_alice", "X-User-Roles": "employee"},
            ).status_code,
            401,
        )

        emp_scope = AuthScope("u_alice", ("employee",), authenticated=True)
        paused = asyncio.run(runtime.run_turn("发放 900 元", thread_id="th_pay", scope=emp_scope))
        review_id = paused["__interrupt__"][0]["review_id"]

        alice_hdr = HeaderIdentityResolver.sign_headers(
            secret=secret, user_id="u_alice", roles=("employee", "manager")
        )
        r_self = client.post(
            f"/api/v1/approvals/{review_id}/resume",
            headers=alice_hdr,
            json={"approved": True, "thread_id": "th_pay", "approver_id": "spoofed_ceo"},
        )
        self.assertEqual(r_self.status_code, 403)
        self.assertEqual(writes, [])

        boss_hdr = HeaderIdentityResolver.sign_headers(
            secret=secret, user_id="u_boss", roles=("manager",)
        )
        r_ok = client.post(
            f"/api/v1/approvals/{review_id}/resume",
            headers=boss_hdr,
            json={"approved": True, "thread_id": "th_pay", "approver_id": "spoofed_ceo"},
        )
        self.assertEqual(r_ok.status_code, 200)
        self.assertEqual(r_ok.json()["status"], "completed")
        self.assertEqual(writes, [(900, "u_alice")])
        ticket = store.get_approval_ticket(review_id)
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
                    self.n += 1
                    if self.n == 1:
                        return Message("assistant", "", [ToolCall("tc_w", "wire_transfer", {"amount": 500})])
                    return Message("assistant", "done")

            rbac = RbacAccessController(admin_roles=("admin",))
            app = AgentRuntimeApp(
                model=WireModel(), store=store, obs=obs, tools=[wire_transfer], access_controller=rbac
            )
            scope = AuthScope("u_alice", ("employee",))
            paused = await app.run_turn("转账", thread_id="th_rev", scope=scope)
            review_id = paused["__interrupt__"][0]["review_id"]

            rbac.bump_policy_rev()
            res = await app.resume_approval(
                thread_id="th_rev",
                decision=ApprovalDecision(review_id=review_id, approved=True, approver_id="u_alice"),
                scope=scope,
            )
            self.assertEqual(writes, [])
            self.assertTrue(res.get("final_answer"))

            broken = BrokenAccessController()
            app_broken = AgentRuntimeApp(store=store, obs=obs, access_controller=broken)
            ctx = ExecutionContext(uid("tr"), "th_brk", scope, uid("req"))
            tool_res = await app_broken.gateway.execute_one(ToolCall("tc_b", "read", {"path": "."}), ctx)
            self.assertFalse(tool_res.ok)
            self.assertEqual(tool_res.error_code, "acl_error")

            http_client = TestClient(_make_http_app(app_broken))
            self.assertEqual(http_client.get("/api/v1/outputs/out_any").status_code, 503)
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
            app = AgentRuntimeApp(model=EchoTestModel(), store=store)
            scope = AuthScope("u_alice", ("employee",))

            with patch.object(store, "record_turn_start", side_effect=RuntimeError("db_write_blip")):
                with self.assertRaises(RuntimeError):
                    await app.run_turn("hello", thread_id="th_fail_start", scope=scope)

            self.assertIsNone(store.get_active_run("th_fail_start"))
            ok_turn = await app.run_turn("hello again", thread_id="th_fail_start", scope=scope)
            self.assertTrue(ok_turn["final_answer"])

        asyncio.run(_run())

    def test_p0_signature_boundaries_sdk_approver_and_artifact_gate(self):
        async def _run():
            store = get_test_store()
            emp = AuthScope("u_emp", ("employee",))

            secret = "strict-hmac-secret"
            resolver = HeaderIdentityResolver(hmac_secret=secret)
            hdr1 = HeaderIdentityResolver.sign_headers(
                secret=secret, user_id="u_alice", roles=("employee",)
            )
            tampered = {**hdr1, "X-User-Id": "u_bob"}
            with self.assertRaises(AuthenticationError):
                resolver.resolve(tampered)
            with self.assertRaises(AuthenticationError):
                HeaderIdentityResolver.sign_headers(secret=secret, user_id="u1", roles=("a,b",))
            with self.assertRaises(AuthenticationError):
                HeaderIdentityResolver.sign_headers(secret=secret, user_id="u1\n", roles=("employee",))

            @tool(risk_level=RiskLevel.HIGH_RISK)
            async def approve_me(x: int) -> str:
                return f"ok:{x}"

            class OnceModel:
                def __init__(self):
                    self.i = 0

                async def generate(self, *, messages, tool_specs, ctx):
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
            mgr = AuthScope("u_real_mgr", ("manager",))
            await app.resume_approval(
                thread_id="th_sdk_app",
                decision=ApprovalDecision(review_id=rid, approved=True, approver_id="spoofed_id"),
                scope=mgr,
            )
            tkt = store.get_approval_ticket(rid)
            self.assertIsNotNone(tkt)
            self.assertEqual(tkt.approver_id, "u_real_mgr")

            ctx = ExecutionContext(uid("tr"), "th_sdk_app", emp, uid("req"))
            art_res = await app.gateway.execute_one(
                ToolCall("tc_art", "read_tool_output", {"ref_id": "out_any"}), ctx
            )
            self.assertFalse(art_res.ok)
            self.assertEqual(art_res.error_code, "permission_denied")

        asyncio.run(_run())

    def test_scoped_middleware_cannot_corrupt_context_budget_or_tool_pairs(self):
        from agent_runtime import AgentMiddleware
        from agent_runtime.framework.context.memory import ContextBudget

        async def _run():
            store = get_test_store()
            captured_messages = []

            class PiiAndHugeInjectMW(AgentMiddleware):
                name = "pii_and_huge_inject"

                async def transform_user_input(self, text, ctx):
                    return text.replace("13800138000", "[PHONE_REDACTED]")

                async def extra_context(self, latest_user_text, ctx):
                    return "超大背景材料：" + ("制度条款" * 4000)

                async def on_before_model(self, messages, ctx):
                    self.snapshot_type = type(messages)
                    captured_messages.append(messages)

            class CaptureModel:
                async def generate(self, *, messages, tool_specs, ctx):
                    return Message("assistant", f"收到({len(messages)}条)")

            mw = PiiAndHugeInjectMW()
            budget = ContextBudget(
                max_window_tokens=1600,
                reserve_output_tokens=200,
                max_extra_context_tokens=300,
            )
            app = AgentRuntimeApp(
                model=CaptureModel(),
                store=store,
                middlewares=[mw],
                budget=budget,
                system_prompt="BASE_SYSTEM_PROMPT",
            )
            res = await app.run_turn("联系我 13800138000", thread_id="th_mw_guard")
            self.assertIn("收到", res["final_answer"])
            self.assertIs(mw.snapshot_type, tuple)
            self.assertEqual(len(captured_messages), 1)
            msgs = captured_messages[0]
            self.assertEqual(msgs[0].role, "system")
            self.assertEqual(msgs[0].content, "BASE_SYSTEM_PROMPT")
            self.assertEqual(msgs[1].role, "system")
            self.assertIn("truncated for context budget", msgs[1].content)
            self.assertEqual(msgs[-1].role, "user")
            self.assertIn("[PHONE_REDACTED]", msgs[-1].content)
            self.assertNotIn("13800138000", msgs[-1].content)

        asyncio.run(_run())

    def test_message_feedback_persistence_upsert_isolation_and_delete(self):
        store = get_test_store()
        obs = _TestTelemetry(store)
        runtime = AgentRuntimeApp(model=EchoTestModel(), store=store, obs=obs)
        alice_scope = AuthScope("u_alice", ("employee",))
        ctx = ExecutionContext(
            trace_id=uid("tr"),
            thread_id="th_fb_1",
            auth_scope=alice_scope,
            request_id=uid("req"),
        )
        asyncio.run(runtime.run_turn("请帮我查一下数据", ctx=ctx))

        client = TestClient(_make_http_app(runtime))
        alice_h = {"X-User-Id": "u_alice", "X-User-Roles": "employee"}
        bob_h = {"X-User-Id": "u_bob", "X-User-Roles": "employee"}

        r_up = client.post(
            "/api/v1/threads/th_fb_1/feedback",
            headers=alice_h,
            json={"trace_id": ctx.trace_id, "rating": "up"},
        )
        self.assertEqual(r_up.status_code, 200)
        self.assertEqual(r_up.json()["feedback"]["rating"], "up")

        r1 = client.post(
            "/api/v1/threads/th_fb_1/feedback",
            headers=alice_h,
            json={"trace_id": ctx.trace_id, "rating": "down"},
        )
        self.assertEqual(r1.status_code, 200)
        fb1 = r1.json()["feedback"]
        self.assertEqual(fb1["rating"], "down")
        self.assertEqual(fb1["trace_id"], ctx.trace_id)
        self.assertIsNotNone(fb1["msg_id"])
        self.assertIn("已收到消息", fb1["answer_excerpt"])

        r2 = client.post(
            "/api/v1/threads/th_fb_1/feedback",
            headers=alice_h,
            json={"trace_id": ctx.trace_id, "rating": "down", "reason": "回答不够具体"},
        )
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["feedback"]["reason"], "回答不够具体")
        r_list = client.get("/api/v1/threads/th_fb_1/feedback", headers=alice_h)
        self.assertEqual(r_list.status_code, 200)
        self.assertEqual(len(r_list.json()["feedback"]), 1)

        r_msgs = client.get("/api/v1/threads/th_fb_1/messages", headers=alice_h)
        self.assertEqual(r_msgs.status_code, 200)
        asst_msgs = [m for m in r_msgs.json()["messages"] if m["role"] == "assistant"]
        self.assertEqual(len(asst_msgs), 1)
        self.assertEqual(asst_msgs[0]["feedback"]["rating"], "down")
        self.assertEqual(asst_msgs[0]["feedback"]["reason"], "回答不够具体")

        self.assertEqual(client.get("/api/v1/threads/th_fb_1/feedback", headers=bob_h).status_code, 404)
        self.assertEqual(
            client.post(
                "/api/v1/threads/th_fb_1/feedback",
                headers=bob_h,
                json={"trace_id": ctx.trace_id, "rating": "down"},
            ).status_code,
            404,
        )

        r_del = client.delete(f"/api/v1/threads/th_fb_1/feedback/{ctx.trace_id}", headers=alice_h)
        self.assertEqual(r_del.status_code, 200)
        self.assertTrue(r_del.json()["deleted"])
        self.assertEqual(len(client.get("/api/v1/threads/th_fb_1/feedback", headers=alice_h).json()["feedback"]), 0)

    def test_workspace_file_upload_parse_download_and_security(self):
        import io
        import zipfile
        from docx import Document
        import openpyxl
        from pypdf import PdfWriter

        store = get_test_store()
        obs = _TestTelemetry(store)
        runtime = AgentRuntimeApp(model=EchoTestModel(), store=store, obs=obs)
        client = TestClient(_make_http_app(runtime))
        alice_h = {"X-User-Id": "u_alice", "X-User-Roles": "employee"}
        bob_h = {"X-User-Id": "u_bob", "X-User-Roles": "employee"}
        tid = "th_files_1"

        doc_buf = io.BytesIO()
        doc = Document()
        doc.add_paragraph("2026 华东区差旅预算说明")
        tbl = doc.add_table(rows=2, cols=2)
        tbl.cell(0, 0).text = "城市"
        tbl.cell(0, 1).text = "上限"
        tbl.cell(1, 0).text = "上海"
        tbl.cell(1, 1).text = "650元"
        doc.save(doc_buf)
        docx_bytes = doc_buf.getvalue()

        r_docx = client.post(
            f"/api/v1/threads/{tid}/files",
            headers=alice_h,
            files={"file": ("预算说明 (1).docx", docx_bytes, "application/octet-stream")},
        )
        self.assertEqual(r_docx.status_code, 200, r_docx.text)
        f_docx = r_docx.json()["file"]
        self.assertEqual(f_docx["path"], "uploads/预算说明 (1).docx")
        self.assertGreater(f_docx["extracted_chars"], 10)

        xlsx_buf = io.BytesIO()
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "费用明细"
        ws.append(["月份", "超标金额"])
        ws.append(["7月", 21000])
        wb.save(xlsx_buf)
        xlsx_bytes = xlsx_buf.getvalue()

        r_xlsx = client.post(
            f"/api/v1/threads/{tid}/files",
            headers=alice_h,
            files={"file": ("q3.xlsx", xlsx_bytes, "application/octet-stream")},
        )
        self.assertEqual(r_xlsx.status_code, 200, r_xlsx.text)

        pdf_buf = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.write(pdf_buf)
        pdf_bytes = pdf_buf.getvalue()
        r_pdf = client.post(
            f"/api/v1/threads/{tid}/files",
            headers=alice_h,
            files={"file": ("scan.pdf", pdf_bytes, "application/pdf")},
        )
        self.assertEqual(r_pdf.status_code, 200, r_pdf.text)

        ctx = ExecutionContext(uid("tr"), tid, AuthScope("u_alice", ("employee",)), uid("req"))
        ls_out = asyncio.run(runtime.gateway.execute_one(ToolCall("tc_fls", "read", {"path": "."}), ctx))
        self.assertTrue(ls_out.ok)
        self.assertNotIn("oss_key", ls_out.content)
        self.assertIn("uploads/预算说明 (1).docx", ls_out.content)

        rd_docx = asyncio.run(
            runtime.gateway.execute_one(ToolCall("tc_f1", "read", {"path": "uploads/预算说明 (1).docx"}), ctx)
        )
        self.assertTrue(rd_docx.ok)
        self.assertIn("2026 华东区差旅预算说明", rd_docx.content)
        self.assertIn("| 上海 | 650元 |", rd_docx.content)
        self.assertIn("has_more=false", rd_docx.content)

        rd_xlsx = asyncio.run(
            runtime.gateway.execute_one(ToolCall("tc_f2", "read", {"path": "uploads/q3.xlsx"}), ctx)
        )
        self.assertTrue(rd_xlsx.ok)
        self.assertIn("[Sheet: 费用明细", rd_xlsx.content)
        self.assertIn("21000", rd_xlsx.content)

        rd_pdf = asyncio.run(
            runtime.gateway.execute_one(ToolCall("tc_f3", "read", {"path": "uploads/scan.pdf"}), ctx)
        )
        self.assertTrue(rd_pdf.ok)
        self.assertIn("扫描件", rd_pdf.content)

        r_dl = client.get(
            f"/api/v1/threads/{tid}/files/download",
            headers=alice_h,
            params={"path": "uploads/预算说明 (1).docx"},
        )
        self.assertEqual(r_dl.status_code, 200)
        self.assertEqual(r_dl.content, docx_bytes)
        self.assertEqual(r_dl.headers.get("x-content-type-options"), "nosniff")
        self.assertIn("sandbox", r_dl.headers.get("content-security-policy", ""))
        self.assertIn("filename*=UTF-8''", r_dl.headers.get("content-disposition", ""))

        store.write_workspace_file(tid, "reports/page.html", "<script>alert(1)</script>", actor_id="u_alice")
        r_html = client.get(
            f"/api/v1/threads/{tid}/files/download",
            headers=alice_h,
            params={"path": "reports/page.html"},
        )
        self.assertEqual(r_html.status_code, 200)
        self.assertEqual(r_html.headers.get("content-type"), "application/octet-stream")

        xxe_buf = io.BytesIO()
        with zipfile.ZipFile(xxe_buf, "w") as zf:
            zf.writestr("word/document.xml", '<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe "boom">]><w:document/>')
        r_xxe = client.post(
            f"/api/v1/threads/{tid}/files",
            headers=alice_h,
            files={"file": ("evil.docx", xxe_buf.getvalue(), "application/octet-stream")},
        )
        self.assertEqual(r_xxe.status_code, 422)

        r_fake_pdf = client.post(
            f"/api/v1/threads/{tid}/files",
            headers=alice_h,
            files={"file": ("fake.pdf", b"NOT_A_PDF", "application/pdf")},
        )
        self.assertEqual(r_fake_pdf.status_code, 422)

        r_old_xls = client.post(
            f"/api/v1/threads/{tid}/files",
            headers=alice_h,
            files={"file": ("old.xls", b"legacy", "application/vnd.ms-excel")},
        )
        self.assertEqual(r_old_xls.status_code, 422)

        self.assertEqual(client.get(f"/api/v1/threads/{tid}/files", headers=bob_h).status_code, 404)
        self.assertEqual(
            client.get(
                f"/api/v1/threads/{tid}/files/download",
                headers=bob_h,
                params={"path": "uploads/q3.xlsx"},
            ).status_code,
            404,
        )
        self.assertEqual(
            client.post(
                f"/api/v1/threads/{tid}/files",
                headers=bob_h,
                files={"file": ("hijack.txt", b"hi", "text/plain")},
            ).status_code,
            404,
        )

        self.assertTrue(store.delete_thread(AuthScope("u_alice", ("employee",)), tid))
        self.assertEqual(store.list_workspace_files(tid), [])


if __name__ == "__main__":
    unittest.main()
