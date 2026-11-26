"""Conversation thread CRUD & message history routes."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request

from agent_runtime.api.deps import caller_scope
from agent_runtime.api.schemas import CreateThreadRequest
from agent_runtime.framework.core.config import uid
from agent_runtime.framework.core.runs import RunNotFoundError

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
    try:
        req.app.state.store.own_thread(caller_scope(req), thread_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    rows = [asdict(m) for m in req.app.state.store.list_messages(thread_id)]
    for m in rows:
        m["metadata"].pop("reasoning_content", None)
    return {"thread_id": thread_id, "messages": rows}


@router.delete("/threads/{thread_id}", summary="删除历史会话及其消息、轨迹与工作区记录")
def delete_thread(thread_id: str, req: Request):
    scope = caller_scope(req)
    with suppress(RunNotFoundError):
        req.app.state.runtime.run_service.request_abort(scope=scope, thread_id=thread_id)
    ok = req.app.state.store.delete_thread(scope, thread_id)
    if not ok:
        raise HTTPException(404, "对话不存在")
    return {"ok": True, "thread_id": thread_id}
