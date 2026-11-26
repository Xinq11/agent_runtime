"""Operations & observability routes: global overview, OSS paging, and KB document upload."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from prometheus_client import generate_latest
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from agent_runtime.api.deps import (
    caller_scope,
    caller_scope_async,
    require_access,
    require_access_async,
    resolve_filter,
)
from agent_runtime.api.schemas import KnowledgeUploadRequest, StatusUpdateRequest
from agent_runtime.framework.core.config import MAX_DOCUMENT
from agent_runtime.framework.core.contracts import ExecutionContext
from agent_runtime.framework.core.security import AccessDecision, ResourceRef

router = APIRouter(tags=["Operations & Knowledge"])


@router.get("/ops/overview", summary="全局监控总览（PLG / 会话上下文 / OSS 工件 / 知识库 / Skills / Runs）")
def ops_overview(req: Request):
    scope, store, obs = caller_scope(req), req.app.state.store, req.app.state.obs
    require_access(req, scope, "ops:view", ResourceRef("ops", tenant_id=scope.tenant_id))
    sk_flt = resolve_filter(req, scope, "skill:manage", "skill")
    snap = store.get_ops_overview_snapshot(scope, skills_filter=sk_flt)
    model_cfg = getattr(req.app.state.model, "config", None)
    model_pub = model_cfg.public() if model_cfg is not None else {"mode": "custom"}

    # Process-global Prometheus metrics & Loki logs require explicit platform telemetry permission
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


@router.get("/ops/outputs/{ref_id}", summary="分页读取 MinIO OSS 落盘的大工具输出工件")
def ops_read_output(ref_id: str, req: Request, offset: int = 0, limit: int = 1200):
    scope = caller_scope(req)
    require_access(req, scope, "artifact:read", ResourceRef("artifact", id=ref_id, tenant_id=scope.tenant_id))
    ctx = ExecutionContext(trace_id="ops", thread_id="ops", request_id="ops", auth_scope=scope)
    page, err = req.app.state.store.read_tool_output_page(ctx, ref_id, offset=offset, limit=limit)
    if err:
        raise HTTPException(404, err)
    return {"ref_id": ref_id, "offset": offset, "content": page}


@router.post("/knowledge/upload", summary="上传知识库文档到 MinIO OSS 并切块索引至 pgvector")
async def upload_knowledge(req: Request):
    scope = await caller_scope_async(req)
    await require_access_async(req, scope, "kb:manage", ResourceRef("rag_doc", tenant_id=scope.tenant_id))
    if req.headers.get("content-type", "").startswith("multipart/form-data"):
        async with req.form() as form:
            file = form.get("file")
            if not isinstance(file, UploadFile):
                raise HTTPException(422, "需要 UTF-8 文本文件")
            raw = await file.read(MAX_DOCUMENT + 1)
            if len(raw) > MAX_DOCUMENT:
                raise HTTPException(413, "文档上限为 2 MiB")
            try:
                content = raw.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise HTTPException(422, "文档需要 UTF-8 编码") from exc
            title = str(form.get("title") or file.filename or "")
            section = str(form.get("section") or "")
            roles = form.getlist("acl_roles")
    else:
        try:
            body = KnowledgeUploadRequest.model_validate(await req.json())
        except ValidationError as exc:
            raise HTTPException(422, exc.errors()) from exc
        title, content = body.title, body.content
        section = body.section
        roles = body.acl_roles

    if not title.strip() or not content.strip():
        raise HTTPException(422, "title 和 content 不能为空")
    if len(content.encode("utf-8")) > MAX_DOCUMENT:
        raise HTTPException(413, "文档上限为 2 MiB")
    roles = [str(r).strip() for r in (roles or []) if str(r).strip()]

    result = await asyncio.to_thread(
        req.app.state.store.index_document,
        scope,
        title.strip(),
        content,
        roles,
        section=section.strip() or title.strip(),
        upload=True,
    )
    return {"ok": True, **result, "chunks_indexed": result["chunk_count"]}


@router.patch("/knowledge/{parent_id}/status", summary="切换知识库文档生效/下架状态")
def update_knowledge_status(parent_id: str, req: Request, body: StatusUpdateRequest):
    scope = caller_scope(req)
    require_access(req, scope, "kb:manage", ResourceRef("rag_doc", id=parent_id, tenant_id=scope.tenant_id))
    ok = req.app.state.store.set_document_status(scope, parent_id, body.status)
    if not ok:
        raise HTTPException(404, "knowledge_document_not_found")
    return {"ok": True, "parent_id": parent_id, "status": body.status}
