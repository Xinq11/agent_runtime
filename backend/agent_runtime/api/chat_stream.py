"""Streaming SSE chat, durable Run inspection, and safe Abort routes."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import asdict
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from agent_runtime.api.deps import caller_scope, caller_scope_async
from agent_runtime.api.schemas import AbortRequest, ChatRequest
from agent_runtime.framework.context.memory import ContextWindowExceeded
from agent_runtime.framework.core.config import publish_sse, sse_meta, sse_sink
from agent_runtime.framework.core.contracts import SSEEvent
from agent_runtime.framework.core.runs import RunConflictError, RunNotFoundError
from agent_runtime.infra.llm.provider import ModelNotConfiguredError

log = logging.getLogger("agent-runtime.chat")
router = APIRouter(tags=["Conversation & Runs"])


@router.get("/runs/{run_id}", summary="查询单次 Run 的持久状态")
def get_run(run_id: str, req: Request):
    try:
        rec = req.app.state.runtime.run_service.get_run(caller_scope(req), run_id)
    except RunNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    return asdict(rec)


def _parse_last_event_id(req: Request, after_seq: int) -> int:
    cursor = max(0, int(after_seq))
    raw = req.headers.get("Last-Event-ID") or req.headers.get("last-event-id")
    if raw is not None:
        try:
            cursor = max(cursor, int(str(raw).strip()))
        except ValueError:
            pass
    return cursor


@router.get("/runs/{run_id}/events", summary="按 Last-Event-ID 断点续接并补帧单次 Run 的 SSE 事件流")
async def resume_run_events(run_id: str, req: Request, after_seq: int = 0):
    scope = await caller_scope_async(req)
    runtime = req.app.state.runtime
    try:
        runtime.run_service.get_run(scope, run_id)
    except RunNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc

    stream = runtime.sse_broker.get(run_id)
    if stream is None:
        raise HTTPException(410, "该运行的 SSE 事件缓冲已过期，请直接拉取会话消息与运行状态")

    cursor = _parse_last_event_id(req, after_seq)

    async def replay():
        async for ev in stream.subscribe(after_seq=cursor, is_disconnected=req.is_disconnected):
            yield ev.to_sse_wire()

    return StreamingResponse(
        replay(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/chat/abort", status_code=202, summary="主动中止正在执行或待审批的 Run")
async def abort_chat(req: Request, body: AbortRequest):
    scope = await caller_scope_async(req)
    try:
        rec = await req.app.state.runtime.abort_run(
            scope=scope, run_id=body.run_id, thread_id=body.thread_id
        )
    except RunNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    req.app.state.obs.log_event(
        "turn_aborted",
        rec.trace_id,
        rec.thread_id,
        run_id=rec.run_id,
        status=rec.status,
        msg=f"Run {rec.run_id} abort requested -> status={rec.status}",
    )
    return {"ok": True, "run": asdict(rec)}


@router.post("/chat/sse", summary="发起流式对话（返回标准 v1 SSE 信封）")
async def chat_sse(req: Request, body: ChatRequest):
    text = body.message or body.user_text
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(422, "message 不能为空")

    scope = await caller_scope_async(req)
    runtime = req.app.state.runtime
    store, obs = req.app.state.store, req.app.state.obs
    run_service = runtime.run_service

    try:
        ctx, run_rec = await runtime.prepare_turn_run(
            text,
            scope=scope,
            thread_id=body.thread_id or None,
            request_id=body.request_id or None,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RunConflictError as exc:
        raise HTTPException(409, str(exc)) from exc

    tid, trace, request_id = ctx.thread_id, ctx.trace_id, ctx.request_id
    run_stream = runtime.sse_broker.open(run_rec.run_id)
    stream_meta = {"run_id": run_rec.run_id, "stream": run_stream}

    async def worker():
        token_sink = sse_sink.set(None)
        token_meta = sse_meta.set(stream_meta)
        try:
            model_cfg = getattr(req.app.state.model, "config", None)
            if model_cfg is not None and not getattr(model_cfg, "api_key", ""):
                run_service.mark_finished(
                    run_rec.run_id, status="failed", error_detail="ModelNotConfiguredError"
                )
                await publish_sse(
                    SSEEvent(
                        "error",
                        {
                            "error": "未配置模型 API Key，请先在 .env 设置 DEEPSEEK_API_KEY 或点击左下角设置填写密钥。",
                            "trace_id": trace,
                            "run_id": run_rec.run_id,
                        },
                    )
                )
                return

            await asyncio.to_thread(store.record_turn_start, tid, trace, text)
            await publish_sse(
                SSEEvent(
                    "meta",
                    {
                        "run_id": run_rec.run_id,
                        "thread_id": tid,
                        "trace_id": trace,
                        **req.app.state.model.config.public(),
                    },
                )
            )
            obs.log_event("turn_start", trace, tid, run_id=run_rec.run_id)

            final = await runtime.execute_run_state(
                run_id=run_rec.run_id,
                scope=scope,
                thread_id=tid,
                trace_id=trace,
                request_id=request_id,
            )
            if final.get("__interrupt__"):
                await publish_sse(
                    SSEEvent(
                        "approval_required",
                        {
                            "run_id": run_rec.run_id,
                            "thread_id": tid,
                            "trace_id": trace,
                            "interrupts": final["__interrupt__"],
                        },
                    )
                )
            else:
                await publish_sse(
                    SSEEvent(
                        "done",
                        {
                            "run_id": run_rec.run_id,
                            "thread_id": tid,
                            "trace_id": trace,
                            "answer": final["final_answer"],
                        },
                    )
                )
        except asyncio.CancelledError:
            run_service.mark_finished(run_rec.run_id, status="cancelled")
            obs.log_event("turn_cancelled", trace, tid, run_id=run_rec.run_id)
            with suppress(Exception):
                await publish_sse(
                    SSEEvent("cancelled", {"run_id": run_rec.run_id, "thread_id": tid, "trace_id": trace})
                )
        except Exception as exc:
            log.exception("Turn failed: %s", trace)
            run_service.mark_finished(
                run_rec.run_id, status="failed", error_detail=type(exc).__name__
            )
            obs.log_event("turn_error", trace, tid, run_id=run_rec.run_id, error=type(exc).__name__)
            if isinstance(exc, HTTPException):
                detail = exc.detail
            elif isinstance(exc, (ModelNotConfiguredError, ContextWindowExceeded)):
                detail = str(exc)
            else:
                detail = "执行失败，请检查模型配置和服务日志"
            await publish_sse(SSEEvent("error", {"error": detail, "trace_id": trace, "run_id": run_rec.run_id}))
        finally:
            run_service.detach_task(run_rec.run_id)
            runtime.sse_broker.close(run_rec.run_id)
            sse_meta.reset(token_meta)
            sse_sink.reset(token_sink)

    task = asyncio.create_task(worker())
    run_service.attach_task(run_rec.run_id, task)

    async def stream():
        async for ev in run_stream.subscribe(after_seq=0, is_disconnected=req.is_disconnected):
            yield ev.to_sse_wire()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
