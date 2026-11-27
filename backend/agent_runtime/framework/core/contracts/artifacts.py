"""Storage artifact, workspace file, skill catalog, and RAG retrieval records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class ToolOutputBlob:
    ref_id: str
    tenant_id: str
    resource_owner: str
    tool_name: str
    full_text: str
    preview: str
    total_chars: int
    expires_at: float


@dataclass
class WorkspaceFileRecord:
    """Cloud-native workspace file stored in OSS with PG version/owner metadata."""

    tenant_id: str
    thread_id: str
    logical_path: str
    version: int
    oss_key: str
    sha256: str
    size_bytes: int
    content_type: str = "text/plain; charset=utf-8"
    updated_by: str = ""
    extracted_oss_key: Optional[str] = None
    extracted_chars: int = 0


@dataclass
class SkillSummary:
    tenant_id: str
    slug: str
    version_id: str
    version_num: int
    description: str
    tags: Tuple[str, ...] = ()
    acl_roles: Tuple[str, ...] = ()
    resources: Tuple[str, ...] = ()
    revision: int = 1
    status: str = "active"


@dataclass
class RAGChunk:
    chunk_id: str
    parent_id: str
    tenant_id: str
    doc_title: str
    section_path: str
    doc_url: str
    content: str
    parent_text: str
    acl_roles: Tuple[str, ...]
    status: str = "active"
    embedding: Tuple[float, ...] = ()
    score: float = 0.0


@dataclass
class RetrievalResult:
    """Standardized RAG output separating grounded hits, low-confidence, no-hit, and errors."""

    status: str  # ok | no_hit | low_confidence | error
    hits: List[RAGChunk] = field(default_factory=list)
    formatted_context: str = ""
    rewritten_query: Optional[str] = None
    corrected: bool = False
    error: Optional[str] = None
