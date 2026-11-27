"""PostgreSQL + OSS repository mixin for tool-output spills and versioned workspace files."""

from __future__ import annotations

import hashlib
import time
from typing import List, Optional, Tuple

from agent_runtime.framework.core.config import uid
from agent_runtime.framework.core.contracts import ToolOutputBlob, WorkspaceFileRecord


class PgArtifactsMixin:
    """OSS-backed tool outputs and atomic-versioned cloud workspace files."""

    def save_tool_output(self, ctx, tool_name, full_text, preview, ttl_sec=3600) -> ToolOutputBlob:
        ref = uid("out")
        obj = f"outputs/{ref}.txt"
        self.oss.put_text(obj, full_text)
        expires = time.time() + ttl_sec
        with self.transaction() as cur:
            cur.execute(
                """INSERT INTO tool_outputs(ref_id,resource_owner,tool_name,full_text,
                      preview,total_chars,expires_at,oss_key,thread_id)
                   VALUES (%s,%s,%s,'',%s,%s,to_timestamp(%s),%s,
                           (SELECT thread_id FROM runtime_traces WHERE trace_id=%s))""",
                (
                    ref,
                    ctx.auth_scope.user_id,
                    tool_name,
                    preview,
                    len(full_text),
                    expires,
                    obj,
                    ctx.trace_id,
                ),
            )
        return ToolOutputBlob(
            ref,
            ctx.auth_scope.user_id,
            tool_name,
            full_text,
            preview,
            len(full_text),
            expires,
        )

    def read_tool_output_page(self, ctx, ref_id, offset=0, limit=4000) -> Tuple[Optional[str], Optional[str]]:
        if type(offset) is not int or offset < 0 or type(limit) is not int or limit < 1:
            return None, "invalid_page"
        rows = self.rows(
            """SELECT oss_key,total_chars FROM tool_outputs WHERE ref_id=%s
               AND resource_owner=%s AND expires_at>now()""",
            (ref_id, ctx.auth_scope.user_id),
        )
        if not rows or not rows[0]["oss_key"]:
            return None, "output_not_found"
        total_chars = int(rows[0]["total_chars"] or 0)
        if offset >= total_chars:
            return (
                f"[tool_output ref_id={ref_id} chars={total_chars}..{total_chars}/{total_chars} has_more=false EOF]",
                None,
            )
        full_text = self.oss.get_text(rows[0]["oss_key"])
        actual_total = len(full_text)
        if offset >= actual_total:
            return (
                f"[tool_output ref_id={ref_id} chars={actual_total}..{actual_total}/{actual_total} has_more=false EOF]",
                None,
            )
        end = min(offset + min(limit, 8000), actual_total)
        chunk = full_text[offset:end]
        if end < actual_total:
            header = f"[tool_output ref_id={ref_id} chars={offset}..{end}/{actual_total} has_more=true next_offset={end}]"
        else:
            header = f"[tool_output ref_id={ref_id} chars={offset}..{end}/{actual_total} has_more=false]"
        return f"{header}\n{chunk}", None

    def write_workspace_file(
        self,
        thread_id: str,
        logical_path: str,
        content: str,
        *,
        actor_id: str,
        content_type: str = "text/plain; charset=utf-8",
    ) -> WorkspaceFileRecord:
        raw = content.encode("utf-8")
        return self.write_workspace_binary(
            thread_id,
            logical_path,
            raw,
            extracted_text=content,
            is_binary=False,
            actor_id=actor_id,
            content_type=content_type,
        )

    def write_workspace_binary(
        self,
        thread_id: str,
        logical_path: str,
        raw_bytes: bytes,
        *,
        extracted_text: str,
        is_binary: bool,
        actor_id: str,
        content_type: str = "application/octet-stream",
        max_files_per_thread: int = 50,
    ) -> WorkspaceFileRecord:
        digest = hashlib.sha256(raw_bytes).hexdigest()
        oss_key = f"workspaces/{thread_id}/{uid('wf')}/{logical_path}"
        self.oss.put_bytes(oss_key, raw_bytes, content_type=content_type)
        if is_binary:
            extracted_oss_key = f"{oss_key}.extracted.txt"
            self.oss.put_text(extracted_oss_key, extracted_text, content_type="text/plain; charset=utf-8")
        else:
            extracted_oss_key = oss_key
        extracted_chars = len(extracted_text)
        with self.transaction() as cur:
            cur.execute(
                "SELECT 1 FROM workspace_files WHERE thread_id=%s AND logical_path=%s",
                (thread_id, logical_path),
            )
            exists = cur.fetchone() is not None
            if not exists:
                cur.execute(
                    "SELECT count(*) FROM workspace_files WHERE thread_id=%s",
                    (thread_id,),
                )
                if int(cur.fetchone()[0]) >= max_files_per_thread:
                    raise ValueError(f"单会话工作区文件数已达上限（{max_files_per_thread} 个）")
            cur.execute(
                """INSERT INTO workspace_files
                   (thread_id,logical_path,version,oss_key,sha256,size_bytes,
                    content_type,updated_by,extracted_oss_key,extracted_chars,updated_at)
                   VALUES (%s,%s,1,%s,%s,%s,%s,%s,%s,%s,now())
                   ON CONFLICT (thread_id,logical_path) DO UPDATE SET
                   version=workspace_files.version + 1, oss_key=EXCLUDED.oss_key,
                   sha256=EXCLUDED.sha256, size_bytes=EXCLUDED.size_bytes,
                   content_type=EXCLUDED.content_type, updated_by=EXCLUDED.updated_by,
                   extracted_oss_key=EXCLUDED.extracted_oss_key,
                   extracted_chars=EXCLUDED.extracted_chars,
                   updated_at=now()
                   RETURNING version""",
                (
                    thread_id,
                    logical_path,
                    oss_key,
                    digest,
                    len(raw_bytes),
                    content_type,
                    actor_id,
                    extracted_oss_key,
                    extracted_chars,
                ),
            )
            version = int(cur.fetchone()[0])
        return WorkspaceFileRecord(
            thread_id=thread_id,
            logical_path=logical_path,
            version=version,
            oss_key=oss_key,
            sha256=digest,
            size_bytes=len(raw_bytes),
            content_type=content_type,
            updated_by=actor_id,
            extracted_oss_key=extracted_oss_key,
            extracted_chars=extracted_chars,
        )

    def read_workspace_file(
        self, thread_id: str, logical_path: str
    ) -> Tuple[Optional[str], Optional[WorkspaceFileRecord], Optional[str]]:
        rows = self.rows(
            """SELECT thread_id,logical_path,version,oss_key,sha256,size_bytes,
                      content_type,updated_by,extracted_oss_key,extracted_chars
               FROM workspace_files WHERE thread_id=%s AND logical_path=%s""",
            (thread_id, logical_path),
        )
        if not rows:
            return None, None, "file_not_found"
        rec = WorkspaceFileRecord(**rows[0])
        text_key = rec.extracted_oss_key or rec.oss_key
        return self.oss.get_text(text_key), rec, None

    def read_workspace_bytes(
        self, thread_id: str, logical_path: str
    ) -> Tuple[Optional[bytes], Optional[WorkspaceFileRecord], Optional[str]]:
        rows = self.rows(
            """SELECT thread_id,logical_path,version,oss_key,sha256,size_bytes,
                      content_type,updated_by,extracted_oss_key,extracted_chars
               FROM workspace_files WHERE thread_id=%s AND logical_path=%s""",
            (thread_id, logical_path),
        )
        if not rows:
            return None, None, "file_not_found"
        rec = WorkspaceFileRecord(**rows[0])
        return self.oss.get_bytes(rec.oss_key), rec, None

    def list_workspace_files(self, thread_id: str) -> List[WorkspaceFileRecord]:
        rows = self.rows(
            """SELECT thread_id,logical_path,version,oss_key,sha256,size_bytes,
                      content_type,updated_by,extracted_oss_key,extracted_chars
               FROM workspace_files WHERE thread_id=%s ORDER BY logical_path""",
            (thread_id,),
        )
        return [WorkspaceFileRecord(**r) for r in rows]
