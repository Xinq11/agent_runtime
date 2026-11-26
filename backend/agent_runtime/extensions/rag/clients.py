"""Default zero-dependency local EmbeddingClient & RerankClient implementations."""

from __future__ import annotations

from typing import List, Sequence, Tuple

from agent_runtime.extensions.rag.models import RerankItem
from agent_runtime.extensions.rag.text_utils import cosine, embed_text, terms


class LocalHashEmbeddingClient:
    """Deterministic zero-dependency local embedding client (swap with EAS/OpenAI in prod)."""

    def embed(self, text: str) -> Tuple[float, ...]:
        return embed_text(text)


class LocalLexicalRerankClient:
    """Deterministic local cross-scoring reranker (swap with EAS/BGE RerankClient in prod)."""

    def rerank(self, query: str, documents: Sequence[str]) -> List[RerankItem]:
        q_terms = set(terms(query))
        q_vec = embed_text(query)
        out: List[RerankItem] = []
        for idx, doc in enumerate(documents):
            if not q_terms:
                out.append(RerankItem(index=idx, score=0.0))
                continue
            d_terms = set(terms(doc))
            overlap = len(q_terms & d_terms) / len(q_terms)
            vec_sim = max(0.0, cosine(q_vec, embed_text(doc)))
            score = round(0.65 * overlap + 0.35 * vec_sim, 4)
            out.append(RerankItem(index=idx, score=score))
        return out
