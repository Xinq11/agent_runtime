"""Thread Trace waterfall, summary cursor, workspace files, and event log snapshot route."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request

from agent_runtime.api.deps import caller_scope

router = APIRouter(tags=["Conversation & Runs"])


@router.get("/state", summary="获取会话 Trace 瀑布图、摘要游标、工作区文件与事件日志快照")
async def runtime_state(req: Request, thread_id: str = "", trace_id: str = ""):
    scope = caller_scope(req)
    store, obs = req.app.state.store, req.app.state.obs
    try:
        snapshot = store.get_thread_state_snapshot(scope, thread_id, trace_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc

    active_run = (
        req.app.state.runtime.run_service.get_active_run_for_thread(scope, thread_id)
        if thread_id
        else None
    )
    logs = await obs.trace_logs(snapshot["trace_id"])
    return {
        **snapshot,
        "active_run": asdict(active_run) if active_run else None,
        "loki_logs": logs,
    }
