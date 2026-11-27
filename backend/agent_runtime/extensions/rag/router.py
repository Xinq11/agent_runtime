"""Optional FastAPI admin router owned by `RagExtension` (lazy FastAPI import)."""

import asyncio

from agent_runtime.extensions.rag.repo import PgRagRepository
from agent_runtime.framework.core.config import MAX_DOCUMENT
from agent_runtime.framework.core.security import ResourceRef


def build_rag_router():
    from fastapi import APIRouter, HTTPException, Request
    from pydantic import ValidationError
    from starlette.datastructures import UploadFile

    from agent_runtime.api.deps import caller_scope, caller_scope_async, require_access, require_access_async
    from agent_runtime.api.schemas import KnowledgeUploadRequest, StatusUpdateRequest

    router = APIRouter(tags=["Knowledge Management (RAG Extension)"])

    def _repo(req: Request) -> PgRagRepository:
        ext = req.app.state.runtime.get_extension("rag")
        if ext is not None and getattr(ext, "repo", None) is not None:
            return ext.repo
        return PgRagRepository(req.app.state.store)

    @router.post("/knowledge/upload", summary="上传知识库文档到 MinIO OSS 并切块索引至 pgvector")
    async def upload_knowledge(req: Request):
        scope = await caller_scope_async(req)
        await require_access_async(req, scope, "kb:manage", ResourceRef("rag_doc"))
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
            _repo(req).index_document,
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
        require_access(req, scope, "kb:manage", ResourceRef("rag_doc", id=parent_id))
        ok = _repo(req).set_document_status(scope, parent_id, body.status)
        if not ok:
            raise HTTPException(404, "knowledge_document_not_found")
        return {"ok": True, "parent_id": parent_id, "status": body.status}

    return router
