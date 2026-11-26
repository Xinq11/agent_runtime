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
                """INSERT INTO tool_outputs(ref_id,tenant_id,resource_owner,tool_name,full_text,
                      preview,total_chars,expires_at,oss_key,thread_id)
                   VALUES (%s,%s,%s,%s,'',%s,%s,to_timestamp(%s),%s,
                           (SELECT thread_id FROM runtime_traces WHERE trace_id=%s))""",
                (
                    ref,
                    ctx.auth_scope.tenant_id,
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
            ctx.auth_scope.tenant_id,
            ctx.auth_scope.user_id,
            tool_name,
            full_text,
            preview,
            len(full_text),
            expires,
        )

    def read_tool_output_page(self, ctx, ref_id, offset=0, limit=1200) -> Tuple[Optional[str], Optional[str]]:
        if type(offset) is not int or offset < 0 or type(limit) is not int or limit < 1:
            return None, "invalid_page"
        rows = self.rows(
            """SELECT oss_key,total_chars FROM tool_outputs WHERE ref_id=%s AND tenant_id=%s
               AND resource_owner=%s AND expires_at>now()""",
            (ref_id, ctx.auth_scope.tenant_id, ctx.auth_scope.user_id),
        )
        if not rows or not rows[0]["oss_key"]:
            return None, "output_not_found"
        if offset >= rows[0]["total_chars"]:
            return "", None
        return self.oss.get_text(rows[0]["oss_key"])[offset : offset + min(limit, 1200)], None

    def write_workspace_file(
        self,
        tenant_id: str,
        thread_id: str,
        logical_path: str,
        content: str,
        *,
        actor_id: str,
        content_type: str = "text/plain; charset=utf-8",
    ) -> WorkspaceFileRecord:
        raw = content.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        # Unique immutable OSS object key so concurrent writes never overwrite each other
        oss_key = f"workspaces/{tenant_id}/{thread_id}/{uid('wf')}/{logical_path}"
        self.oss.put_text(oss_key, content, content_type=content_type)
        with self.transaction() as cur:
            cur.execute(
                """INSERT INTO workspace_files
                   (tenant_id,thread_id,logical_path,version,oss_key,sha256,size_bytes,content_type,updated_by,updated_at)
                   VALUES (%s,%s,%s,1,%s,%s,%s,%s,%s,now())
                   ON CONFLICT (tenant_id,thread_id,logical_path) DO UPDATE SET
                   version=workspace_files.version + 1, oss_key=EXCLUDED.oss_key,
                   sha256=EXCLUDED.sha256, size_bytes=EXCLUDED.size_bytes,
                   content_type=EXCLUDED.content_type, updated_by=EXCLUDED.updated_by,
                   updated_at=now()
                   RETURNING version""",
                (tenant_id, thread_id, logical_path, oss_key, digest, len(raw), content_type, actor_id),
            )
            version = int(cur.fetchone()[0])
        return WorkspaceFileRecord(
            tenant_id=tenant_id,
            thread_id=thread_id,
            logical_path=logical_path,
            version=version,
            oss_key=oss_key,
            sha256=digest,
            size_bytes=len(raw),
            content_type=content_type,
            updated_by=actor_id,
        )

    def read_workspace_file(
        self, tenant_id: str, thread_id: str, logical_path: str
    ) -> Tuple[Optional[str], Optional[WorkspaceFileRecord], Optional[str]]:
        rows = self.rows(
            """SELECT tenant_id,thread_id,logical_path,version,oss_key,sha256,size_bytes,content_type,updated_by
               FROM workspace_files WHERE tenant_id=%s AND thread_id=%s AND logical_path=%s""",
            (tenant_id, thread_id, logical_path),
        )
        if not rows:
            return None, None, "file_not_found"
        rec = WorkspaceFileRecord(**rows[0])
        return self.oss.get_text(rec.oss_key), rec, None

    def list_workspace_files(self, tenant_id: str, thread_id: str) -> List[WorkspaceFileRecord]:
        rows = self.rows(
            """SELECT tenant_id,thread_id,logical_path,version,oss_key,sha256,size_bytes,content_type,updated_by
               FROM workspace_files WHERE tenant_id=%s AND thread_id=%s ORDER BY logical_path""",
            (tenant_id, thread_id),
        )
        return [WorkspaceFileRecord(**r) for r in rows]
