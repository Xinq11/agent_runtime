"""Tokenization, deterministic local hash embedding, cosine similarity, and citation formatting."""

from __future__ import annotations

from collections import Counter
import hashlib
import math
import re
from typing import Iterable, List, Optional, Tuple
import unicodedata

from agent_runtime.framework.core.contracts import RAGChunk

VECTOR_DIM = 256
_WORDS = re.compile(r"[\u3400-\u9fff]+|[^\W_]+", re.UNICODE)


def terms(text: str) -> List[str]:
    """Chinese unigrams and adjacent bigrams, plus normalized ASCII words/numbers."""
    tokens = []
    for word in _WORDS.findall(unicodedata.normalize("NFKC", text).casefold()):
        for part in re.findall(r"[\u3400-\u9fff]+|[^\u3400-\u9fff]+", word):
            if "\u3400" <= part[0] <= "\u9fff":
                tokens.extend(part)
                tokens.extend(part[i : i + 2] for i in range(len(part) - 1))
            else:
                tokens.append(part)
    return tokens


def embed_text(text: str) -> Tuple[float, ...]:
    vector = [0.0] * VECTOR_DIM
    for term, frequency in Counter(terms(text)).items():
        digest = hashlib.blake2b(term.encode(), digest_size=4).digest()
        index = int.from_bytes(digest[:3], "big") % VECTOR_DIM
        vector[index] += (1 if digest[3] & 1 else -1) * (1 + math.log(frequency))
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return tuple(x / norm for x in vector)


def chunk_text(chunk: RAGChunk) -> str:
    return f"{chunk.doc_title} {chunk.section_path} {chunk.content}"


def cosine(left: Iterable[float], right: Iterable[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def format_grounded_context(hits: List[RAGChunk], *, rewritten_query: Optional[str] = None) -> str:
    blocks = [
        f"[{i}] 《{h.doc_title}》 - {h.section_path} (来源: {h.doc_url})\n{h.parent_text}"
        for i, h in enumerate(hits, 1)
    ]
    return "\n\n".join(blocks)
