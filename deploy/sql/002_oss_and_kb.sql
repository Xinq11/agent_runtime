BEGIN;
ALTER TABLE agent_threads ADD COLUMN IF NOT EXISTS title text NOT NULL DEFAULT '新对话';
ALTER TABLE tool_outputs ADD COLUMN IF NOT EXISTS oss_key text;
ALTER TABLE tool_outputs ADD COLUMN IF NOT EXISTS thread_id text REFERENCES agent_threads ON DELETE CASCADE;
-- Older releases created this demo table in the shared schema. Transfer cleanup
-- to its foreign key so historical rows still follow thread deletion after upgrade.
DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = to_regclass('oa_approvals')
          AND conname = 'oa_approvals_thread_id_fkey' AND confdeltype <> 'c'
    ) THEN
        ALTER TABLE oa_approvals DROP CONSTRAINT oa_approvals_thread_id_fkey,
            ADD CONSTRAINT oa_approvals_thread_id_fkey FOREIGN KEY (thread_id)
            REFERENCES agent_threads ON DELETE CASCADE;
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS runtime_traces (
    trace_id text PRIMARY KEY,
    thread_id text NOT NULL REFERENCES agent_threads ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS runtime_traces_by_thread ON runtime_traces(thread_id, created_at DESC);
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='grafana_reader') THEN
        CREATE ROLE grafana_reader LOGIN;
    END IF;
END $$;
GRANT USAGE ON SCHEMA public TO grafana_reader;
GRANT SELECT ON agent_spans TO grafana_reader;
COMMIT;
