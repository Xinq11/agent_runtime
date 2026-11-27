-- Requires PostgreSQL with pgvector >= 0.5 installed on the server.
-- LangGraph's checkpointer owns its own migrations (AsyncPostgresSaver.setup()).
BEGIN;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE agent_threads (
    thread_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    actor_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, actor_id, thread_id)
);
CREATE TABLE session_messages (
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
CREATE INDEX session_messages_thread_cursor ON session_messages (thread_id, msg_id);
CREATE TABLE session_summaries (
    thread_id text PRIMARY KEY REFERENCES agent_threads,
    summary_text text NOT NULL,
    covered_up_to_msg_id bigint NOT NULL CHECK (covered_up_to_msg_id >= 0)
);

CREATE TABLE tool_approvals (
    review_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    thread_id text NOT NULL,
    actor_id text NOT NULL,
    op_key text NOT NULL,
    tool_name text NOT NULL,
    args_hash text NOT NULL,
    policy_rev integer NOT NULL,
    summary jsonb NOT NULL,
    expires_at timestamptz NOT NULL,
    decision text NOT NULL DEFAULT 'pending' CHECK (decision IN ('pending','approved','rejected')),
    approver_id text,
    consumed_at timestamptz,
    FOREIGN KEY (tenant_id, actor_id, thread_id)
        REFERENCES agent_threads (tenant_id, actor_id, thread_id),
    CHECK ((decision = 'pending') = (approver_id IS NULL)),
    CHECK (consumed_at IS NULL OR decision = 'approved')
);
CREATE INDEX tool_approvals_op ON tool_approvals (tenant_id, op_key);
CREATE TABLE tool_outputs (
    ref_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    resource_owner text NOT NULL,
    tool_name text NOT NULL,
    full_text text NOT NULL,
    preview text NOT NULL,
    total_chars integer NOT NULL CHECK (total_chars >= 0),
    expires_at timestamptz NOT NULL,
    UNIQUE (tenant_id, resource_owner, ref_id)
);
CREATE INDEX tool_outputs_expiry ON tool_outputs (expires_at);
CREATE TABLE tool_runs (
    tenant_id text NOT NULL,
    op_key text NOT NULL,
    actor_id text NOT NULL,
    tool_name text NOT NULL,
    args_hash text NOT NULL,
    status text NOT NULL CHECK (status IN ('running','done','failed','reconcile')),
    receipt jsonb,
    preview text,
    result_ref text,
    error_code text,
    lease_until timestamptz NOT NULL,
    UNIQUE (tenant_id, op_key),
    FOREIGN KEY (tenant_id, actor_id, result_ref)
        REFERENCES tool_outputs (tenant_id, resource_owner, ref_id)
);
CREATE INDEX tool_runs_reconciliation ON tool_runs (lease_until)
    WHERE status IN ('running','reconcile');

CREATE TABLE rag_parents (
    tenant_id text NOT NULL,
    parent_id text NOT NULL,
    doc_title text NOT NULL,
    section_path text NOT NULL,
    doc_url text NOT NULL,
    parent_text text NOT NULL,
    PRIMARY KEY (tenant_id, parent_id)
);
CREATE TABLE rag_chunks (
    tenant_id text NOT NULL,
    chunk_id text NOT NULL,
    parent_id text NOT NULL,
    content text NOT NULL,
    acl_roles text[] NOT NULL DEFAULT '{}',
    status text NOT NULL DEFAULT 'active',
    embedding vector(256) NOT NULL,
    -- Write the same space-separated Chinese n-grams / English words used by the client.
    fts_terms text NOT NULL,
    fts_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, fts_terms)) STORED,
    PRIMARY KEY (tenant_id, chunk_id),
    FOREIGN KEY (tenant_id, parent_id) REFERENCES rag_parents
);
CREATE INDEX rag_chunks_vector ON rag_chunks USING hnsw (embedding vector_cosine_ops)
    WHERE status = 'active';
CREATE INDEX rag_chunks_fts ON rag_chunks USING gin (fts_tsv);
CREATE INDEX rag_chunks_acl ON rag_chunks USING gin (acl_roles);
CREATE INDEX rag_chunks_tenant_status ON rag_chunks (tenant_id, status);

CREATE TABLE agent_spans (
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
CREATE INDEX agent_spans_waterfall ON agent_spans (trace_id, start_ms);
-- Grafana Traces panel: Table query, startTime and duration in milliseconds.
-- SELECT trace_id AS "traceID", span_id AS "spanID", parent_span_id AS "parentSpanID",
--        service_name AS "serviceName", operation_name AS "operationName",
--        start_ms AS "startTime", duration_ms AS duration
-- FROM agent_spans WHERE trace_id = ${trace_id:sqlstring} ORDER BY start_ms, span_id;
COMMIT;
