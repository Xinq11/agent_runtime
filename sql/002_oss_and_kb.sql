BEGIN;
ALTER TABLE agent_threads ADD COLUMN IF NOT EXISTS title text NOT NULL DEFAULT '新对话';
ALTER TABLE tool_outputs ADD COLUMN IF NOT EXISTS oss_key text;
ALTER TABLE tool_outputs ADD COLUMN IF NOT EXISTS thread_id text REFERENCES agent_threads;
ALTER TABLE rag_parents ADD COLUMN IF NOT EXISTS oss_key text;
CREATE TABLE IF NOT EXISTS oa_approvals (
    approval_id text PRIMARY KEY,
    thread_id text NOT NULL REFERENCES agent_threads,
    tenant_id text NOT NULL, applicant text NOT NULL,
    op_key text NOT NULL, title text NOT NULL, region text NOT NULL,
    amount numeric NOT NULL CHECK (amount >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, op_key)
);
CREATE TABLE IF NOT EXISTS runtime_traces (
    trace_id text PRIMARY KEY, thread_id text NOT NULL REFERENCES agent_threads,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS runtime_traces_by_thread ON runtime_traces(thread_id,created_at DESC);
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='grafana_reader') THEN
        CREATE ROLE grafana_reader LOGIN;
    END IF;
END $$;
GRANT USAGE ON SCHEMA public TO grafana_reader;
GRANT SELECT ON agent_spans TO grafana_reader;
COMMIT;
