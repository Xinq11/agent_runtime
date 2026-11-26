"""PostgreSQL repository mixin for threads, session messages, traces, and summaries."""

from __future__ import annotations

from dataclasses import asdict
import json
from typing import Any, Dict, List, Optional, Tuple

from agent_runtime.framework.core.contracts import AuthScope, Message, ToolCall


class PgSessionsMixin:
    """Thread metadata, message history, trace records, and compaction summary persistence."""

    def own_thread(self, scope: AuthScope, tid: str) -> Dict[str, Any]:
        rows = self.rows(
            "SELECT * FROM agent_threads WHERE thread_id=%s AND tenant_id=%s AND actor_id=%s",
            (tid, scope.tenant_id, scope.user_id),
        )
        if not rows:
            raise LookupError("对话不存在")
        return rows[0]

    def create_thread(self, scope: AuthScope, tid: str, title: str) -> Dict[str, Any]:
        with self.transaction() as cur:
            cur.execute(
                """INSERT INTO agent_threads(thread_id,tenant_id,actor_id,title)
                   VALUES (%s,%s,%s,%s) ON CONFLICT(thread_id) DO NOTHING""",
                (tid, scope.tenant_id, scope.user_id, title),
            )
        return self.own_thread(scope, tid)

    def list_threads(self, scope: AuthScope, limit: int = 80) -> List[Dict[str, Any]]:
        return self.rows(
            """
            SELECT t.thread_id, t.title, t.created_at,
                   (SELECT count(*) FROM session_messages m WHERE m.thread_id=t.thread_id) AS message_count
            FROM agent_threads t
            WHERE t.tenant_id=%s AND t.actor_id=%s
              AND EXISTS (SELECT 1 FROM session_messages m WHERE m.thread_id=t.thread_id)
            ORDER BY t.created_at DESC
            LIMIT %s
            """,
            (scope.tenant_id, scope.user_id, limit),
        )

    def record_turn_start(self, thread_id: str, trace_id: str, user_text: str) -> None:
        with self.transaction() as cur:
            cur.execute("INSERT INTO runtime_traces(trace_id,thread_id) VALUES (%s,%s)", (trace_id, thread_id))
            cur.execute(
                "INSERT INTO session_messages(thread_id,role,content) VALUES (%s,'user',%s)",
                (thread_id, user_text),
            )

    def append_message(self, thread_id: str, msg: Message) -> int:
        with self.transaction() as cur:
            cur.execute("SELECT 1 FROM agent_threads WHERE thread_id=%s FOR UPDATE", (thread_id,))
            cur.execute(
                """INSERT INTO session_messages
                   (thread_id,role,content,tool_calls,tool_call_id,name,metadata)
                   VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s::jsonb) RETURNING msg_id""",
                (
                    thread_id,
                    msg.role,
                    msg.content,
                    json.dumps([asdict(t) for t in msg.tool_calls]),
                    msg.tool_call_id,
                    msg.name,
                    json.dumps(msg.metadata),
                ),
            )
            return cur.fetchone()[0]

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
        with self.transaction() as cur:
            if step_key is not None:
                cur.execute(
                    """SELECT 1 FROM session_messages
                       WHERE thread_id=%s AND metadata->>'trace_id'=%s AND metadata->>'step_key'=%s
                       LIMIT 1""",
                    (thread_id, trace_id, step_key),
                )
            else:
                cur.execute(
                    """SELECT 1 FROM session_messages
                       WHERE thread_id=%s AND metadata->>'trace_id'=%s
                         AND metadata->>'step_key' IS NULL AND role='assistant'
                       LIMIT 1""",
                    (thread_id, trace_id),
                )
            if cur.fetchone():
                return
            for m in messages:
                meta = {**m.metadata, "trace_id": trace_id}
                if step_key is not None:
                    meta["step_key"] = step_key
                cur.execute(
                    """INSERT INTO session_messages(thread_id,role,content,tool_calls,tool_call_id,name,metadata)
                       VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s::jsonb)""",
                    (
                        thread_id,
                        m.role,
                        m.content,
                        json.dumps([asdict(t) for t in m.tool_calls]),
                        m.tool_call_id,
                        m.name,
                        json.dumps(meta),
                    ),
                )

    def list_messages(self, thread_id: str, after_msg_id: int = 0) -> List[Message]:
        with self.transaction() as cur:
            cur.execute(
                """SELECT role,content,tool_calls,tool_call_id,name,msg_id,metadata
                   FROM session_messages WHERE thread_id=%s AND msg_id>%s ORDER BY msg_id""",
                (thread_id, after_msg_id),
            )
            return [
                Message(role, body, [ToolCall(**c) for c in calls], cid, name, mid, meta)
                for role, body, calls, cid, name, mid, meta in cur.fetchall()
            ]

    def get_summary(self, thread_id: str) -> Tuple[str, int]:
        with self.transaction() as cur:
            cur.execute(
                "SELECT summary_text,covered_up_to_msg_id FROM session_summaries WHERE thread_id=%s",
                (thread_id,),
            )
            return cur.fetchone() or ("", 0)

    def save_summary(self, thread_id: str, summary_text: str, covered_up_to_msg_id: int) -> None:
        with self.transaction() as cur:
            cur.execute(
                """INSERT INTO session_summaries (thread_id,summary_text,covered_up_to_msg_id)
                   VALUES (%s,%s,%s) ON CONFLICT (thread_id) DO UPDATE
                   SET summary_text=EXCLUDED.summary_text, covered_up_to_msg_id=EXCLUDED.covered_up_to_msg_id
                   WHERE session_summaries.covered_up_to_msg_id <= EXCLUDED.covered_up_to_msg_id""",
                (thread_id, summary_text, covered_up_to_msg_id),
            )

    def delete_thread(self, scope: AuthScope, thread_id: str) -> bool:
        """Delete a thread and all dependent messages, traces, spans, runs, approvals, and artifacts."""
        with self.transaction() as cur:
            cur.execute(
                "SELECT 1 FROM agent_threads WHERE thread_id=%s AND tenant_id=%s AND actor_id=%s FOR UPDATE",
                (thread_id, scope.tenant_id, scope.user_id),
            )
            if not cur.fetchone():
                return False

            cur.execute(
                "DELETE FROM agent_spans WHERE trace_id IN (SELECT trace_id FROM runtime_traces WHERE thread_id=%s)",
                (thread_id,),
            )
            cur.execute(
                """DELETE FROM tool_runs r
                   WHERE r.tenant_id=%s AND (
                       EXISTS (SELECT 1 FROM tool_approvals a WHERE a.tenant_id=r.tenant_id AND a.op_key=r.op_key AND a.thread_id=%s)
                       OR EXISTS (SELECT 1 FROM tool_outputs o WHERE o.tenant_id=r.tenant_id AND o.ref_id=r.result_ref AND o.thread_id=%s)
                   )""",
                (scope.tenant_id, thread_id, thread_id),
            )
            cur.execute("DELETE FROM tool_outputs WHERE tenant_id=%s AND thread_id=%s", (scope.tenant_id, thread_id))
            cur.execute("DELETE FROM tool_approvals WHERE tenant_id=%s AND thread_id=%s", (scope.tenant_id, thread_id))
            cur.execute("DELETE FROM oa_approvals WHERE tenant_id=%s AND thread_id=%s", (scope.tenant_id, thread_id))
            cur.execute("DELETE FROM agent_runs WHERE tenant_id=%s AND thread_id=%s", (scope.tenant_id, thread_id))
            cur.execute("DELETE FROM workspace_files WHERE tenant_id=%s AND thread_id=%s", (scope.tenant_id, thread_id))
            cur.execute("DELETE FROM runtime_traces WHERE thread_id=%s", (thread_id,))
            cur.execute("DELETE FROM session_summaries WHERE thread_id=%s", (thread_id,))
            cur.execute("DELETE FROM session_messages WHERE thread_id=%s", (thread_id,))
            for tbl in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                cur.execute(f"SELECT to_regclass(%s)", (tbl,))
                if cur.fetchone()[0]:
                    cur.execute(f"DELETE FROM {tbl} WHERE thread_id=%s", (thread_id,))
            cur.execute(
                "DELETE FROM agent_threads WHERE thread_id=%s AND tenant_id=%s AND actor_id=%s",
                (thread_id, scope.tenant_id, scope.user_id),
            )
            return True
