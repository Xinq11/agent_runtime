"""Conversation thread CRUD & message history routes."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request

from agent_runtime.api.deps import caller_scope
from agent_runtime.api.schemas import CreateThreadRequest, MessageFeedbackRequest
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
