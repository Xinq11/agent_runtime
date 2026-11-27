"""Optional reference Operations & Admin console routes (mounted only when `AGENT_ENABLE_OPS=1`)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from prometheus_client import generate_latest

from agent_runtime.api.deps import (
    caller_scope,
    require_access,
    resolve_filter,
)
from agent_runtime.framework.core.contracts import ExecutionContext
from agent_runtime.framework.core.security import AccessDecision, ResourceRef

router = APIRouter(tags=["Reference Admin Console (Optional)"])


def _build_ops_overview(req: Request, scope, skills_filter) -> Dict[str, Any]:
    store = req.app.state.store
    runtime = req.app.state.runtime

    threads_rows = store.rows(
        """
        SELECT t.thread_id, t.title, t.created_at,
               (SELECT count(*) FROM session_messages m WHERE m.thread_id=t.thread_id) AS message_count,
               COALESCE(s.covered_up_to_msg_id, 0) AS covered_up_to_msg_id,
               COALESCE(s.summary_text, '') AS summary_text
        FROM agent_threads t
        LEFT JOIN session_summaries s ON s.thread_id=t.thread_id
        WHERE EXISTS (SELECT 1 FROM session_messages m WHERE m.thread_id=t.thread_id)
        ORDER BY t.created_at DESC
        LIMIT 80
        """
    )
    approval_rows = store.rows(
        """SELECT review_id, thread_id, actor_id, run_id, op_key, tool_name, args_hash, policy_rev,
                  summary, EXTRACT(EPOCH FROM expires_at)::float8 AS expires_at, decision, approver_id,
                  EXTRACT(EPOCH FROM consumed_at)::float8 AS consumed_at
           FROM tool_approvals ORDER BY expires_at DESC LIMIT 50"""
    )
    run_rows = store.rows(
        """SELECT op_key, actor_id, tool_name, args_hash, status, receipt,
                  preview, result_ref, error_code, lease_until
           FROM tool_runs ORDER BY lease_until DESC LIMIT 50"""
    )
    output_rows = store.rows(
        """SELECT ref_id, thread_id, resource_owner, tool_name, total_chars, oss_key,
                  left(preview, 260) AS preview, expires_at
           FROM tool_outputs ORDER BY expires_at DESC LIMIT 50"""
    )
    trace_rows = store.rows(
        """SELECT rt.trace_id, rt.thread_id, rt.created_at,
                  (SELECT count(*) FROM agent_spans sp WHERE sp.trace_id=rt.trace_id) AS span_count,
                  COALESCE((SELECT max(start_ms + duration_ms) - min(start_ms) FROM agent_spans sp WHERE sp.trace_id=rt.trace_id), 0) AS total_ms
           FROM runtime_traces rt
           ORDER BY rt.created_at DESC LIMIT 30"""
    )

    kb_rows = []
    rag_ext = runtime.get_extension("rag")
    if rag_ext is not None and getattr(rag_ext, "repo", None) is not None:
        kb_rows = rag_ext.repo.list_documents_overview()

    skills = []
    skills_ext = runtime.get_extension("skills")
    if skills_ext is not None and getattr(skills_ext, "repo", None) is not None:
        skills = [asdict(s) for s in skills_ext.repo.list_skills(scope, access_filter=skills_filter)]

    return {
        "counts": {
            "threads": len(threads_rows),
            "tool_approvals": len(approval_rows),
            "tool_runs": len(run_rows),
            "tool_outputs": len(output_rows),
            "knowledge_chunks": len(kb_rows),
            "skills": len(skills),
            "traces": len(trace_rows),
        },
        "threads": threads_rows,
        "tool_approvals": approval_rows,
        "tool_runs": run_rows,
        "tool_outputs": output_rows,
        "knowledge_chunks": kb_rows,
        "skills": skills,
        "recent_traces": trace_rows,
    }


@router.get("/ops/overview", summary="全局监控总览（PLG / 会话上下文 / OSS 工件 / 可选知识库与 Skills / Runs）")
def ops_overview(req: Request):
    scope, obs = caller_scope(req), req.app.state.obs
    require_access(req, scope, "ops:view", ResourceRef("ops"))
    sk_flt = resolve_filter(req, scope, "skill:manage", "skill")
    snap = _build_ops_overview(req, scope, sk_flt)
    model_cfg = getattr(req.app.state.model, "config", None)
    model_pub = model_cfg.public() if model_cfg is not None else {"mode": "custom"}

    include_platform_telemetry = False
    ac = req.app.state.runtime.access_controller
    try:
        tele_dec = ac.check(scope, "platform:view_telemetry", ResourceRef("platform"))
        include_platform_telemetry = isinstance(tele_dec, AccessDecision) and tele_dec.allowed
    except Exception:
        include_platform_telemetry = False

    return {
        "health": {"ok": True, "postgres": "ok", "minio": "ok", **model_pub},
        **snap,
        "metrics_text": generate_latest(obs.prom).decode() if include_platform_telemetry else "",
        "loki_logs": list(obs.loki_events)[-80:] if include_platform_telemetry else [],
    }


@router.get("/ops/outputs/{ref_id}", summary="分页读取当前用户有权访问的 MinIO OSS 大工具输出工件")
def ops_read_output(ref_id: str, req: Request, offset: int = 0, limit: int = 1200):
    scope = caller_scope(req)
    require_access(req, scope, "artifact:read", ResourceRef("artifact", id=ref_id))
    ctx = ExecutionContext(trace_id="ops", thread_id="ops", request_id="ops", auth_scope=scope)
    page, err = req.app.state.store.read_tool_output_page(ctx, ref_id, offset=offset, limit=limit)
    if err:
        raise HTTPException(404, err)
    return {"ref_id": ref_id, "offset": offset, "content": page}
