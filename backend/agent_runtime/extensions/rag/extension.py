"""Pluggable `RagExtension`: defaults to the built-in 7-stage RAG pipeline, or accepts any custom `RAGStrategy`."""

from __future__ import annotations

from typing import Any, List, Optional, Sequence

from agent_runtime.extensions.rag.models import EmbeddingClient, RerankClient
from agent_runtime.extensions.rag.retrieval import RagRetrievalService
from agent_runtime.extensions.rag.text_utils import format_grounded_context
from agent_runtime.framework.core.contracts import ExecutionContext, Message, RiskLevel
from agent_runtime.framework.core.protocols import AgentMiddleware, AgentPlugin, RAGStrategy
from agent_runtime.framework.tools.registry import ToolSpec


class _AutoInjectRAGMiddleware(AgentMiddleware):
    """Optional `before_model` auto-injection mode for strong KB-first assistants."""

    name = "rag_auto_inject"

    def __init__(self, get_strategy, top_k: int = 3) -> None:
        self._get_strategy = get_strategy
        self._top_k = top_k

    async def before_model(self, messages: List[Message], ctx: ExecutionContext) -> List[Message]:
        strategy = self._get_strategy()
        if strategy is None or not messages or messages[-1].role != "user":
            return messages
        query = (messages[-1].content or "").strip()
        if not query:
            return messages
        result = await strategy.retrieve(
            scope=ctx.auth_scope, query=query, top_k=self._top_k, ctx=ctx
        )
        if result.status != "ok" or not result.hits:
            return messages
        grounded = result.formatted_context or format_grounded_context(result.hits)
        injected = Message(
            role="system",
            content=f"[知识库参考资料 - 回答请标注来源编号]\n{grounded}",
        )
        return [*messages[:-1], injected, messages[-1]]


class RagExtension(AgentPlugin):
    """Optional RAG extension decoupled from core kernel."""

    name = "rag"

    def __init__(
        self,
        *,
        strategy: Optional[RAGStrategy] = None,
        embedding_client: Optional[EmbeddingClient] = None,
        rerank_client: Optional[RerankClient] = None,
        mode: str = "tool",  # "tool" | "auto_inject" | "both"
        top_k: int = 4,
        min_rerank_score: float = 0.35,
    ) -> None:
        if mode not in ("tool", "auto_inject", "both"):
            raise ValueError(f"Invalid RagExtension mode: {mode}")
        self._active_strategy: Optional[RAGStrategy] = strategy
        self._embedding_client = embedding_client
        self._rerank_client = rerank_client
        self.mode = mode
        self.top_k = top_k
        self.min_rerank_score = min_rerank_score

    def register_tools(self, *, registry: Any, store: Any, model: Any, gateway: Any, obs: Any) -> None:
        if self._active_strategy is None:
            self._active_strategy = RagRetrievalService(
                store,
                embedding_client=self._embedding_client,
                rerank_client=self._rerank_client,
                min_rerank_score=self.min_rerank_score,
            )

        if self.mode in ("tool", "both"):
            strategy = self._active_strategy

            async def _search_knowledge(args: dict, ctx: ExecutionContext) -> str:
                q = args.get("query")
                if not isinstance(q, str) or not q.strip():
                    return "[NO_HIT] 缺少检索词。"
                res = await strategy.retrieve(
                    scope=ctx.auth_scope, query=q.strip(), top_k=self.top_k, ctx=ctx
                )
                if res.status == "ok":
                    return res.formatted_context or format_grounded_context(res.hits)
                return res.formatted_context or f"[{res.status.upper()}] {res.error or '未找到足够依据。'}"

            registry.register(
                ToolSpec(
                    name="search_knowledge",
                    description="检索企业知识库文档（默认 7 阶段混合召回与父证据聚合，支持自定义策略）。",
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

    def middlewares(self) -> Sequence[AgentMiddleware]:
        if self.mode in ("auto_inject", "both"):
            return (_AutoInjectRAGMiddleware(lambda: self._active_strategy, top_k=self.top_k),)
        return ()
