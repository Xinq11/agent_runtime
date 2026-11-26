"""High-risk tool approval resume route delegating to `AgentRuntimeApp.resume_approval`."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from agent_runtime.api.deps import caller_scope
from agent_runtime.api.schemas import ApprovalResumeRequest
from agent_runtime.framework.core.contracts import ApprovalDecision
from agent_runtime.framework.core.runs import RunNotFoundError, RunStateError

router = APIRouter(tags=["Conversation & Runs"])


@router.post("/approvals/{review_id}/resume", summary="审批同意/拒绝并恢复暂停的 Run")
async def resume_approval(review_id: str, req: Request, body: ApprovalResumeRequest):
    store = req.app.state.store
    runtime = req.app.state.runtime
    scope = caller_scope(req)

    tid = store.find_approval_thread(review_id, scope)
    if not tid or (body.thread_id and body.thread_id != tid):
        raise HTTPException(404, "审批单不存在")

    decision = ApprovalDecision(
        review_id,
        body.approved,
        str(body.approver_id or scope.user_id),
        str(body.reason or ""),
    )
    try:
        final = await runtime.resume_approval(thread_id=tid, decision=decision, scope=scope)
    except RunNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (RunStateError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc

    if final.get("status") == "cancelled":
        raise HTTPException(409, "审批恢复已被取消")

    tickets = final.get("__interrupt__", [])
    return {
        "ok": True,
        "run_id": final.get("run_id"),
        "thread_id": tid,
        "trace_id": final.get("trace_id"),
        "final_answer": final.get("final_answer", ""),
        "status": "interrupted" if tickets else "completed",
        "interrupts": tickets,
    }
