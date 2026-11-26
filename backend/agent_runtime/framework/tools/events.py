"""SSE & PLG telemetry wrapper around `ToolGateway`."""

from __future__ import annotations

import asyncio

from agent_runtime.framework.core.contracts import ExecutionContext, ToolCall, ToolResult
from agent_runtime.framework.core.langgraph_compat import GRAPH_INTERRUPT_TYPES
from agent_runtime.framework.tools.gateway import ToolGateway


class EventGateway(ToolGateway):
    """ToolGateway subclass emitting live SSE `tool_status` events and PLG telemetry."""

    async def execute_one(
        self, call: ToolCall, ctx: ExecutionContext, loop_counts=None
    ) -> ToolResult:
        fields = {
            "tool": call.name,
            "tool_name": call.name,
            "tool_call_id": call.id,
            "agent": ctx.agent_name,
            "args": call.args,
        }
        await ctx.emit("tool_status", {**fields, "phase": "started", "status": "running"})
        status = "error"
        try:
            result = await super().execute_one(call, ctx, loop_counts)
            status = "ok" if result.ok else result.error_code or "error"
            fields.update(
                ok=result.ok,
                truncated=result.truncated,
                ref_id=result.ref_id,
                oss_key=f"outputs/{result.ref_id}.txt" if result.ref_id else None,
                preview=result.content[:240],
            )
            return result
        except GRAPH_INTERRUPT_TYPES:
            status = "approval_required"
            raise
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            prom_tools = getattr(self.tracer, "tools", None)
            if prom_tools is not None:
                prom_tools.labels(call.name if self.registry.get(call.name) else "unknown", status).inc()
            if self.tracer is not None and hasattr(self.tracer, "log_event"):
                self.tracer.log_event("tool_status", ctx.trace_id, ctx.thread_id, tool=call.name, status=status)
            await ctx.emit(
                "tool_status",
                {**fields, "phase": "paused" if status == "approval_required" else "done", "status": status},
            )
