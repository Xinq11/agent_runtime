-- Core Agent Runtime PostgreSQL schema (ZERO pgvector / RAG / Skills dependency).
-- LangGraph's checkpointer owns its own migrations (AsyncPostgresSaver.setup()).
BEGIN;

CREATE TABLE IF NOT EXISTS agent_threads (
    thread_id text PRIMARY KEY,
    actor_id text NOT NULL,
    title text NOT NULL DEFAULT '新对话',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (actor_id, thread_id)
);

CREATE TABLE IF NOT EXISTS session_messages (
    msg_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    thread_id text NOT NULL REFERENCES agent_threads,
    role text NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    content text NOT NULL DEFAULT '',
    tool_calls jsonb NOT NULL DEFAULT '[]',
    tool_call_id text,
    name text,
    metadata jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS session_messages_thread_cursor ON session_messages (thread_id, msg_id);

CREATE TABLE IF NOT EXISTS session_summaries (
    thread_id text PRIMARY KEY REFERENCES agent_threads,
    summary_text text NOT NULL,
    covered_up_to_msg_id bigint NOT NULL CHECK (covered_up_to_msg_id >= 0)
);

CREATE TABLE IF NOT EXISTS tool_approvals (
    review_id text PRIMARY KEY,
    thread_id text NOT NULL,
    actor_id text NOT NULL,
    run_id text NOT NULL DEFAULT '',
    op_key text NOT NULL,
    tool_name text NOT NULL,
    args_hash text NOT NULL,
    policy_rev integer NOT NULL,
    summary jsonb NOT NULL,
    expires_at timestamptz NOT NULL,
    decision text NOT NULL DEFAULT 'pending' CHECK (decision IN ('pending','approved','rejected')),
    approver_id text,
    consumed_at timestamptz,
    FOREIGN KEY (actor_id, thread_id)
        REFERENCES agent_threads (actor_id, thread_id) ON DELETE CASCADE,
    CHECK ((decision = 'pending') = (approver_id IS NULL)),
    CHECK (consumed_at IS NULL OR decision = 'approved')
);
CREATE INDEX IF NOT EXISTS tool_approvals_op ON tool_approvals (op_key);

CREATE TABLE IF NOT EXISTS tool_outputs (
    ref_id text PRIMARY KEY,
    resource_owner text NOT NULL,
    tool_name text NOT NULL,
    full_text text NOT NULL,
    preview text NOT NULL,
    total_chars integer NOT NULL CHECK (total_chars >= 0),
    expires_at timestamptz NOT NULL,
    oss_key text,
    thread_id text REFERENCES agent_threads ON DELETE CASCADE,
    UNIQUE (resource_owner, ref_id)
);
CREATE INDEX IF NOT EXISTS tool_outputs_expiry ON tool_outputs (expires_at);

CREATE TABLE IF NOT EXISTS tool_runs (
    op_key text PRIMARY KEY,
    actor_id text NOT NULL,
    tool_name text NOT NULL,
    args_hash text NOT NULL,
    status text NOT NULL CHECK (status IN ('running','done','failed','reconcile')),
    receipt jsonb,
    preview text,
    result_ref text,
    error_code text,
    lease_until timestamptz NOT NULL,
    FOREIGN KEY (actor_id, result_ref)
        REFERENCES tool_outputs (resource_owner, ref_id)
);
CREATE INDEX IF NOT EXISTS tool_runs_reconciliation ON tool_runs (lease_until)
    WHERE status IN ('running','reconcile');

CREATE TABLE IF NOT EXISTS agent_spans (
    trace_id text NOT NULL,
    span_id text NOT NULL,
    parent_span_id text,
    service_name text NOT NULL,
    operation_name text NOT NULL,
    start_ms bigint NOT NULL,
    duration_ms bigint NOT NULL CHECK (duration_ms >= 0),
    status text NOT NULL DEFAULT 'ok',
    attributes jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (trace_id, span_id)
);
CREATE INDEX IF NOT EXISTS agent_spans_waterfall ON agent_spans (trace_id, start_ms);

COMMIT;
