"""Conversation thread CRUD & message history routes."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import asdict

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response

from agent_runtime.api.deps import caller_scope, caller_scope_async
from agent_runtime.api.schemas import CreateThreadRequest, MessageFeedbackRequest
from agent_runtime.framework.core.config import uid
from agent_runtime.framework.core.runs import RunNotFoundError
from agent_runtime.framework.tools.builtins import normalize_logical_path
from agent_runtime.infra.documents import (
    MAX_ATTACHMENT_BYTES,
    MAX_FILES_PER_THREAD,
    DocumentValidationError,
    build_content_disposition,
    parse_uploaded_document,
    safe_download_content_type,
)

router = APIRouter(tags=["Conversation & Runs"])


@router.get("/threads", summary="列出当前用户的非空历史会话")
def list_threads(req: Request):
    return {"threads": req.app.state.store.list_threads(caller_scope(req))}


@router.post("/threads", summary="显式创建会话记录")
def create_thread(req: Request, body: CreateThreadRequest):
    tid = body.thread_id or uid("th")
    try:
        row = req.app.state.store.create_thread(caller_scope(req), tid, str(body.title or "新对话")[:150])
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {k: row[k] for k in ("thread_id", "title", "created_at")}


@router.get("/threads/{thread_id}/messages", summary="获取会话完整历史消息")
def list_messages(thread_id: str, req: Request):
    scope = caller_scope(req)
    try:
        req.app.state.store.own_thread(scope, thread_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    rows = [asdict(m) for m in req.app.state.store.list_messages(thread_id)]
    feedback_rows = req.app.state.store.list_thread_feedback(scope, thread_id)
    feedback_by_trace = {
        str(f["trace_id"]): {
            "feedback_id": f["feedback_id"],
            "rating": f["rating"],
            "reason": f["reason"],
            "updated_at": f["updated_at"],
        }
        for f in feedback_rows
    }
    for m in rows:
        m["metadata"].pop("reasoning_content", None)
        tr_id = str((m.get("metadata") or {}).get("trace_id") or "")
        if m.get("role") == "assistant" and tr_id and tr_id in feedback_by_trace:
            m["feedback"] = feedback_by_trace[tr_id]
    return {"thread_id": thread_id, "messages": rows, "feedback_by_trace": feedback_by_trace}


@router.get("/threads/{thread_id}/feedback", summary="查询当前会话的点赞/点踩反馈记录")
def list_feedback(thread_id: str, req: Request):
    scope = caller_scope(req)
    try:
        items = req.app.state.store.list_thread_feedback(scope, thread_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"thread_id": thread_id, "feedback": items}


@router.post("/threads/{thread_id}/feedback", summary="保存或更新助手回复的点赞/点踩反馈")
def submit_feedback(thread_id: str, body: MessageFeedbackRequest, req: Request):
    scope = caller_scope(req)
    try:
        row = req.app.state.store.save_message_feedback(
            scope,
            thread_id=thread_id,
            trace_id=body.trace_id.strip(),
            rating=body.rating,
            reason=body.reason,
            msg_id=body.msg_id,
            answer_excerpt=body.answer_excerpt,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    obs = getattr(req.app.state, "obs", None)
    if obs is not None and hasattr(obs, "log_event"):
        obs.log_event(
            "message_feedback",
            row["trace_id"],
            thread_id,
            rating=row["rating"],
            reason=row["reason"],
            feedback_id=row["feedback_id"],
        )
    return {"ok": True, "feedback": row}


@router.delete("/threads/{thread_id}/feedback/{trace_id}", summary="取消对某条助手回复的点赞/点踩反馈")
def cancel_feedback(thread_id: str, trace_id: str, req: Request):
    scope = caller_scope(req)
    try:
        deleted = req.app.state.store.delete_message_feedback(scope, thread_id=thread_id, trace_id=trace_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True, "deleted": deleted, "trace_id": trace_id}


@router.delete("/threads/{thread_id}", summary="删除历史会话及其消息、轨迹与工作区记录")
def delete_thread(thread_id: str, req: Request):
    scope = caller_scope(req)
    with suppress(RunNotFoundError):
        req.app.state.runtime.run_service.request_abort(scope=scope, thread_id=thread_id)
    ok = req.app.state.store.delete_thread(scope, thread_id)
    if not ok:
        raise HTTPException(404, "对话不存在")
    return {"ok": True, "thread_id": thread_id}


def _public_workspace_file(rec) -> dict:
    return {
        "path": rec.logical_path,
        "logical_path": rec.logical_path,
        "version": rec.version,
        "size_bytes": rec.size_bytes,
        "content_type": rec.content_type,
        "extracted_chars": rec.extracted_chars,
        "updated_by": rec.updated_by,
    }


@router.get("/threads/{thread_id}/files", summary="列出当前会话工作区文件（含上传附件与生成报告）")
def list_thread_files(thread_id: str, req: Request):
    scope = caller_scope(req)
    try:
        req.app.state.store.own_thread(scope, thread_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    files = req.app.state.store.list_workspace_files(thread_id)
    return {"thread_id": thread_id, "files": [_public_workspace_file(f) for f in files]}


@router.post("/threads/{thread_id}/files", summary="上传附件到当前会话工作区（支持文本与 PDF/Word/Excel）")
async def upload_thread_file(thread_id: str, req: Request, file: UploadFile = File(...)):
    scope = await caller_scope_async(req)
    raw = await file.read(MAX_ATTACHMENT_BYTES + 1)
    if len(raw) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            413, f"文件超过大小上限（最大支持 {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MiB）"
        )
    try:
        parsed = await asyncio.to_thread(parse_uploaded_document, file.filename or "attachment.txt", raw)
    except DocumentValidationError as exc:
        raise HTTPException(exc.status_code, exc.message) from exc

    logical_path = normalize_logical_path(f"uploads/{parsed.filename}")
    store = req.app.state.store
    try:
        await asyncio.to_thread(store.create_thread, scope, thread_id, "新对话")
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc

    try:
        rec = await asyncio.to_thread(
            store.write_workspace_binary,
            thread_id,
            logical_path,
            raw,
            extracted_text=parsed.extracted_text,
            is_binary=parsed.is_binary,
            actor_id=scope.user_id,
            content_type=parsed.content_type,
            max_files_per_thread=MAX_FILES_PER_THREAD,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    obs = getattr(req.app.state, "obs", None)
    if obs is not None and hasattr(obs, "log_event"):
        obs.log_event(
            "workspace_file_uploaded",
            "upload",
            thread_id,
            path=rec.logical_path,
            size_bytes=rec.size_bytes,
            extracted_chars=rec.extracted_chars,
        )
    return {"ok": True, "file": _public_workspace_file(rec)}


@router.get("/threads/{thread_id}/files/download", summary="下载当前会话工作区文件（原样安全下载）")
def download_thread_file(thread_id: str, req: Request, path: str = Query(..., min_length=1)):
    scope = caller_scope(req)
    try:
        req.app.state.store.own_thread(scope, thread_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc

    try:
        norm_path = normalize_logical_path(path)
    except ValueError as exc:
        raise HTTPException(400, f"非法文件路径：{exc}") from exc
    if norm_path == ".":
        raise HTTPException(400, "不能下载目录根路径")

    raw_bytes, rec, err = req.app.state.store.read_workspace_bytes(thread_id, norm_path)
    if err or raw_bytes is None or rec is None:
        raise HTTPException(404, "文件不存在")

    media_type = safe_download_content_type(rec.logical_path, rec.content_type)
    return Response(
        content=raw_bytes,
        media_type=media_type,
        headers={
            "Content-Disposition": build_content_disposition(rec.logical_path),
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "private, no-store",
        },
    )
