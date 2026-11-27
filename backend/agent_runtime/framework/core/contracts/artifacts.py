"""Core storage artifact and cloud-native workspace file records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ToolOutputBlob:
    ref_id: str
    resource_owner: str
    tool_name: str
    full_text: str
    preview: str
    total_chars: int
    expires_at: float


@dataclass
class WorkspaceFileRecord:
    """Cloud-native workspace file stored in OSS with PG version/owner metadata."""

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
