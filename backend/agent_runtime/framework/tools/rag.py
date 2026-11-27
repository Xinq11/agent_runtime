"""Thin RAG Harness adapter: wires any customer `RagProvider` into `search_knowledge` & citations."""

from __future__ import annotations

import asyncio
import inspect
from typing import Optional, Sequence

from agent_runtime.framework.core.contracts import ExecutionContext, RiskLevel
from agent_runtime.framework.core.protocols import AgentMiddleware, RagHit, RagProvider, RagResult
from agent_runtime.framework.tools.registry import ToolRegistry, ToolSpec


async def _call_retrieve(provider: RagProvider, *, scope, query: str, top_k: int, ctx: ExecutionContext) -> RagResult:
    fn = provider.retrieve
    if inspect.iscoroutinefunction(fn):
        res = await fn(scope=scope, query=query, top_k=top_k, ctx=ctx)
    else:
        val = await asyncio.to_thread(fn, scope=scope, query=query, top_k=top_k, ctx=ctx)
        res = await val if inspect.isawaitable(val) else val
    if isinstance(res, RagResult):
        return res
    raise TypeError(f"RagProvider.retrieve must return RagResult, got {type(res).__name__}")


def format_rag_hits(hits: Sequence[RagHit]) -> str:
    """Format grounded retrieval hits into numbered [1], [2]... reference blocks."""
    blocks = []
    for i, h in enumerate(hits, 1):
        loc = f" - {h.section}" if h.section else ""
        src = f" (来源: {h.source_url})" if h.source_url else ""
        blocks.append(f"[{i}] 《{h.title}》{loc}{src}\n{h.content}")
    return "\n\n".join(blocks)


class RagAutoInjectMiddleware(AgentMiddleware):
    """Optional `extra_context` auto-injection mode bounded by kernel `ContextBudget`."""

    name = "rag_auto_inject"

    def __init__(self, provider: RagProvider, *, top_k: int = 3) -> None:
        self._provider = provider
        self._top_k = top_k

    async def extra_context(self, latest_user_text: str, ctx: ExecutionContext) -> Optional[str]:
        query = (latest_user_text or "").strip()
        if not query:
            return None
        res = await _call_retrieve(
            self._provider, scope=ctx.auth_scope, query=query, top_k=self._top_k, ctx=ctx
        )
        if res.status != "ok" or (not res.hits and not res.formatted_context):
            return None
        grounded = res.formatted_context or format_rag_hits(res.hits)
        return f"[知识库参考资料 - 仅在引用时标注来源编号]\n{grounded}"


def register_rag_tool(
    registry: ToolRegistry,
    provider: RagProvider,
    *,
    top_k: int = 4,
) -> None:
    """Register standard read-only `search_knowledge` tool backed by `provider`."""

    async def _search_knowledge(args: dict, ctx: ExecutionContext) -> str:
        q = args.get("query")
        if not isinstance(q, str) or not q.strip():
            return "[NO_HIT] 缺少检索词。"
        res = await _call_retrieve(
            provider, scope=ctx.auth_scope, query=q.strip(), top_k=top_k, ctx=ctx
        )
        if res.status == "ok":
            return res.formatted_context or format_rag_hits(res.hits) or "[NO_HIT] 知识库中未检索到相关文档，请勿编造结论。"
        if res.formatted_context:
            return res.formatted_context
        if res.status == "no_hit":
            return "[NO_HIT] 知识库中未检索到相关文档，请勿编造结论。"
        return f"[{res.status.upper()}] {res.error or '未找到足够依据。'}"

    registry.register(
        ToolSpec(
            name="search_knowledge",
            description="检索业务知识库文档并返回带编号出处的参考依据。",
            required_args=("query",),
            allowed_args=("query",),
            risk_level=RiskLevel.READ,
            handler=_search_knowledge,
            parameters_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        )
    )
