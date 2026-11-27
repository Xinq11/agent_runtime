BEGIN;

-- 1. Persistent Run state machine (decouples execution from PG advisory lock & enables safe Abort)
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id text PRIMARY KEY,
    thread_id text NOT NULL REFERENCES agent_threads ON DELETE CASCADE,
    actor_id text NOT NULL,
    trace_id text NOT NULL,
    request_id text NOT NULL,
    status text NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'paused', 'completed', 'cancelled', 'failed')),
    owner_epoch integer NOT NULL DEFAULT 1,
    cancel_requested_at timestamptz,
    error_detail text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_runs_by_thread ON agent_runs(thread_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS agent_runs_active_per_thread
    ON agent_runs(thread_id) WHERE status IN ('running', 'paused');

-- 2. Cloud-native workspace artifacts for immutable built-in `read` / `write` tools (PG metadata + OSS body)
CREATE TABLE IF NOT EXISTS workspace_files (
    thread_id text NOT NULL,
    logical_path text NOT NULL,
    version integer NOT NULL DEFAULT 1,
    oss_key text NOT NULL,
    sha256 text NOT NULL,
    size_bytes integer NOT NULL DEFAULT 0,
    content_type text NOT NULL DEFAULT 'text/plain; charset=utf-8',
    updated_by text NOT NULL,
    extracted_oss_key text,
    extracted_chars integer NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (thread_id, logical_path)
);

GRANT SELECT ON agent_runs TO grafana_reader;

COMMIT;
