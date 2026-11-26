"""PostgreSQL read-model queries powering `/state` and `/ops/overview` without SQL in API routes."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, Optional

from agent_runtime.framework.core.contracts import AuthScope
from agent_runtime.framework.core.security import AccessFilter
from agent_runtime.infra.pg.acl_sql import compile_acl_sql, default_access_filter


class PgOpsQueriesMixin:
    """Aggregated read snapshots for Trace drawer and Ops console."""

    def get_thread_state_snapshot(
        self,
        scope: AuthScope,
        thread_id: str,
        trace_id: str = "",
        *,
        kb_filter: Optional[AccessFilter] = None,
    ) -> Dict[str, Any]:
        owner = self.rows("SELECT tenant_id,actor_id FROM agent_threads WHERE thread_id=%s", (thread_id,))
        if owner and (owner[0]["tenant_id"], owner[0]["actor_id"]) != (scope.tenant_id, scope.user_id):
            raise LookupError("对话不存在")

        traces = self.rows(
            "SELECT trace_id FROM runtime_traces WHERE thread_id=%s ORDER BY created_at DESC LIMIT 20",
            (thread_id,),
        )
        recent = [r["trace_id"] for r in traces]
        if trace_id and not self.rows(
            "SELECT 1 FROM runtime_traces WHERE thread_id=%s AND trace_id=%s", (thread_id, trace_id)
        ):
            trace_id = ""
        target = trace_id or next(iter(recent), None)
        summary, cursor = self.get_summary(thread_id)
        args = (scope.tenant_id, scope.user_id, thread_id)
        approvals = self.rows(
            """SELECT review_id,tenant_id,thread_id,actor_id,op_key,tool_name,args_hash,policy_rev,
               summary,EXTRACT(EPOCH FROM expires_at)::float8 AS expires_at,decision,approver_id,
               EXTRACT(EPOCH FROM consumed_at)::float8 AS consumed_at FROM tool_approvals
               WHERE tenant_id=%s AND actor_id=%s AND thread_id=%s ORDER BY expires_at DESC LIMIT 50""",
            args,
        )
        runs = self.rows(
            """SELECT r.* FROM tool_runs r WHERE tenant_id=%s AND actor_id=%s AND EXISTS
               (SELECT 1 FROM tool_approvals a WHERE a.tenant_id=r.tenant_id AND a.op_key=r.op_key
                AND a.thread_id=%s) ORDER BY lease_until DESC LIMIT 50""",
            args,
        )
        outputs = self.rows(
            """SELECT ref_id,tenant_id,resource_owner,tool_name,total_chars,oss_key,left(preview,200) AS preview
               FROM tool_outputs WHERE tenant_id=%s AND resource_owner=%s AND thread_id=%s
               AND expires_at>now() ORDER BY expires_at DESC LIMIT 50""",
            args,
        )
        flt = kb_filter or default_access_filter(scope)
        kb_acl_sql, kb_acl_params = compile_acl_sql(flt, scope=scope, alias="c")
        chunks = self.rows(
            f"""SELECT c.chunk_id,c.tenant_id,p.doc_title,p.section_path,c.acl_roles,p.doc_url,p.oss_key
               FROM rag_chunks c JOIN rag_parents p USING (tenant_id,parent_id)
               WHERE {kb_acl_sql}
               ORDER BY p.doc_title,c.chunk_id LIMIT 100""",
            kb_acl_params,
        )
        workspace_files = (
            [asdict(f) for f in self.list_workspace_files(scope.tenant_id, thread_id)] if thread_id else []
        )
        return {
            "trace_id": target,
            "recent_traces": recent,
            "spans": [asdict(s) for s in self.list_spans(target)] if target else [],
            "summary": {"covered_up_to_msg_id": cursor, "text": summary},
            "approvals": approvals,
            "tool_runs": runs,
            "tool_outputs": outputs,
            "workspace_files": workspace_files,
            "knowledge_chunks": chunks,
        }

    def get_ops_overview_snapshot(
        self,
        scope: AuthScope,
        *,
        skills_filter: Optional[AccessFilter] = None,
    ) -> Dict[str, Any]:
        threads_rows = self.rows(
            """
            SELECT t.thread_id, t.title, t.created_at,
                   (SELECT count(*) FROM session_messages m WHERE m.thread_id=t.thread_id) AS message_count,
                   COALESCE(s.covered_up_to_msg_id, 0) AS covered_up_to_msg_id,
                   COALESCE(s.summary_text, '') AS summary_text
            FROM agent_threads t
            LEFT JOIN session_summaries s ON s.thread_id=t.thread_id
            WHERE t.tenant_id=%s
              AND EXISTS (SELECT 1 FROM session_messages m WHERE m.thread_id=t.thread_id)
            ORDER BY t.created_at DESC
            LIMIT 80
            """,
            (scope.tenant_id,),
        )
        approval_rows = self.rows(
            """SELECT review_id, tenant_id, thread_id, actor_id, op_key, tool_name, args_hash, policy_rev,
                      summary, EXTRACT(EPOCH FROM expires_at)::float8 AS expires_at, decision, approver_id,
                      EXTRACT(EPOCH FROM consumed_at)::float8 AS consumed_at
               FROM tool_approvals WHERE tenant_id=%s ORDER BY expires_at DESC LIMIT 50""",
            (scope.tenant_id,),
        )
        run_rows = self.rows(
            """SELECT tenant_id, op_key, actor_id, tool_name, args_hash, status, receipt,
                      preview, result_ref, error_code, lease_until
               FROM tool_runs WHERE tenant_id=%s ORDER BY lease_until DESC LIMIT 50""",
            (scope.tenant_id,),
        )
        output_rows = self.rows(
            """SELECT ref_id, tenant_id, thread_id, resource_owner, tool_name, total_chars, oss_key,
                      left(preview, 260) AS preview, expires_at
               FROM tool_outputs WHERE tenant_id=%s ORDER BY expires_at DESC LIMIT 50""",
            (scope.tenant_id,),
        )
        kb_rows = self.rows(
            """SELECT p.parent_id AS chunk_id, p.parent_id, p.tenant_id, p.doc_title, p.section_path,
                      p.doc_url, p.oss_key,
                      COALESCE(
                          (SELECT array_agg(DISTINCT r ORDER BY r)
                           FROM rag_chunks c2, unnest(c2.acl_roles) AS r
                           WHERE c2.tenant_id=p.tenant_id AND c2.parent_id=p.parent_id),
                          ARRAY[]::text[]
                      ) AS acl_roles,
                      left(COALESCE(NULLIF(p.parent_text, ''), MIN(c.content), ''), 240) AS snippet,
                      count(c.chunk_id)::int AS chunk_count,
                      CASE WHEN bool_or(c.status = 'active') THEN 'active' ELSE 'disabled' END AS status
               FROM rag_parents p
               JOIN rag_chunks c USING (tenant_id, parent_id)
               WHERE p.tenant_id=%s
               GROUP BY p.tenant_id, p.parent_id, p.doc_title, p.section_path, p.doc_url, p.oss_key, p.parent_text
               ORDER BY (bool_or(c.status = 'active')) DESC, p.doc_title
               LIMIT 100""",
            (scope.tenant_id,),
        )
        trace_rows = self.rows(
            """SELECT rt.trace_id, rt.thread_id, rt.created_at,
                      (SELECT count(*) FROM agent_spans sp WHERE sp.trace_id=rt.trace_id) AS span_count,
                      COALESCE((SELECT max(start_ms + duration_ms) - min(start_ms) FROM agent_spans sp WHERE sp.trace_id=rt.trace_id), 0) AS total_ms
               FROM runtime_traces rt
               JOIN agent_threads t ON t.thread_id=rt.thread_id
               WHERE t.tenant_id=%s
               ORDER BY rt.created_at DESC LIMIT 30""",
            (scope.tenant_id,),
        )
        sk_flt = skills_filter or default_access_filter(scope, unrestricted=True, include_disabled=True)
        skills = [asdict(s) for s in self.list_skills(scope, access_filter=sk_flt)]
        return {
            "counts": {
                "threads": len(threads_rows),
                "tool_approvals": len(approval_rows),
                "tool_runs": len(run_rows),
                "tool_outputs": len(output_rows),
                "knowledge_chunks": len(kb_rows),
                "skills": len(skills),
                "traces": len(trace_rows),
            },
            "threads": threads_rows,
            "tool_approvals": approval_rows,
            "tool_runs": run_rows,
            "tool_outputs": output_rows,
            "knowledge_chunks": kb_rows,
            "skills": skills,
            "recent_traces": trace_rows,
        }
