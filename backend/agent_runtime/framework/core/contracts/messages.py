"""Message, tool invocation, hashing, and token estimation primitives."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Dict, List, Optional


@dataclass
class ToolCall:
    id: str
    name: str
    args: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Message:
    """Framework-agnostic chat message compatible with OpenAI & LangGraph schemas."""

    role: str  # system | user | assistant | tool
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    msg_id: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    tool_call_id: str
    tool_name: str
    content: str
    ok: bool = True
    truncated: bool = False
    ref_id: Optional[str] = None
    error_code: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


def canonical_hash(obj: Any) -> str:
    """Deterministic SHA-256 hash used for argument/policy/schema fingerprinting."""
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> int:
    """Fast conservative token estimator (handles mixed Chinese + ASCII)."""
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    non_ascii = len(text) - ascii_chars
    return max(1, (ascii_chars // 4) + int(non_ascii * 1.5))
