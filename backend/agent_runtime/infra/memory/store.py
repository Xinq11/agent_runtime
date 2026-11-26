"""In-memory store facade composing messages, spans, runs/approvals, artifacts/skills, and RAG."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import replace
import threading
from typing import Dict, List, Optional, Tuple

from agent_runtime.framework.core.contracts import (
    ApprovalTicket,
    Message,
    RAGChunk,
    RunRecord,
    SpanRecord,
    ToolOutputBlob,
    ToolRunRecord,
    WorkspaceFileRecord,
)
from agent_runtime.infra.memory.approvals import MemoryApprovalsMixin
from agent_runtime.infra.memory.artifacts_skills import MemoryArtifactsSkillsMixin
from agent_runtime.infra.memory.rag import MemoryRagMixin


class InMemoryStore(MemoryApprovalsMixin, MemoryArtifactsSkillsMixin, MemoryRagMixin):
    """Thread-safe in-memory implementation of all storage ports for offline unit tests."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._msg_seq = 0
        self._messages: Dict[str, List[Message]] = defaultdict(list)
        self._summaries: Dict[str, Tuple[str, int]] = {}
        self._approvals: Dict[str, ApprovalTicket] = {}
        self._tool_runs: Dict[Tuple[str, str], ToolRunRecord] = {}
        self._outputs: Dict[str, ToolOutputBlob] = {}
        self._chunks: Dict[Tuple[str, str], RAGChunk] = {}
        self._spans: Dict[str, Dict[str, SpanRecord]] = defaultdict(dict)
        self._runs: Dict[str, RunRecord] = {}
        self._workspace_files: Dict[Tuple[str, str, str], Tuple[WorkspaceFileRecord, str]] = {}
        self._skills: Dict[Tuple[str, str], dict] = {}

    # --- Messages & Summaries ---
    def append_message(self, thread_id: str, msg: Message) -> int:
        with self._lock:
            self._msg_seq += 1
            saved = replace(deepcopy(msg), msg_id=self._msg_seq)
            self._messages[thread_id].append(saved)
            return self._msg_seq

    def save_turn_messages(
        self,
        thread_id: str,
        trace_id: str,
        messages: List[Message],
        *,
        step_key: Optional[str] = None,
    ) -> None:
        if not messages:
            return
        with self._lock:
            existing = self._messages[thread_id]
            if step_key is not None:
                if any(
                    m.metadata.get("trace_id") == trace_id and m.metadata.get("step_key") == step_key
                    for m in existing
                ):
                    return
            else:
                if any(
                    m.role == "assistant"
                    and m.metadata.get("trace_id") == trace_id
                    and m.metadata.get("step_key") is None
                    for m in existing
                ):
                    return
            for m in messages:
                saved = deepcopy(m)
                extra = {"trace_id": trace_id}
                if step_key is not None:
                    extra["step_key"] = step_key
                saved.metadata = {**saved.metadata, **extra}
                self.append_message(thread_id, saved)

    def list_messages(self, thread_id: str, after_msg_id: int = 0) -> List[Message]:
        with self._lock:
            return [deepcopy(m) for m in self._messages.get(thread_id, []) if (m.msg_id or 0) > after_msg_id]

    def get_summary(self, thread_id: str) -> Tuple[str, int]:
        with self._lock:
            return self._summaries.get(thread_id, ("", 0))

    def save_summary(self, thread_id: str, summary_text: str, covered_up_to_msg_id: int) -> None:
        with self._lock:
            _, cur = self._summaries.get(thread_id, ("", 0))
            if covered_up_to_msg_id >= cur:
                self._summaries[thread_id] = (summary_text, covered_up_to_msg_id)

    def delete_thread(self, scope, thread_id: str) -> bool:
        with self._lock:
            existed = thread_id in self._messages or thread_id in self._summaries
            self._messages.pop(thread_id, None)
            self._summaries.pop(thread_id, None)
            for key in [k for k in self._workspace_files if k[0] == scope.tenant_id and k[1] == thread_id]:
                self._workspace_files.pop(key, None)
            for rid in [rid for rid, r in self._runs.items() if r.thread_id == thread_id]:
                self._runs.pop(rid, None)
            return existed

    # --- Spans ---
    def save_span(self, span: SpanRecord) -> None:
        with self._lock:
            self._spans[span.trace_id][span.span_id] = deepcopy(span)

    def list_spans(self, trace_id: str) -> List[SpanRecord]:
        with self._lock:
            spans = self._spans.get(trace_id, {}).values()
            return deepcopy(sorted(spans, key=lambda s: (s.start_ms, s.span_id)))
