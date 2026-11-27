BEGIN;

-- 1. Persistent Run state machine (decouples execution from PG advisory lock & enables safe Abort)
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    thread_id text NOT NULL REFERENCES agent_threads,
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
    tenant_id text NOT NULL,
    thread_id text NOT NULL,
    logical_path text NOT NULL,
    version integer NOT NULL DEFAULT 1,
    oss_key text NOT NULL,
    sha256 text NOT NULL,
    size_bytes integer NOT NULL DEFAULT 0,
    content_type text NOT NULL DEFAULT 'text/plain; charset=utf-8',
    updated_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, thread_id, logical_path)
);

-- 3. Cloud-native versioned declarative Skills (PG catalog + immutable OSS versions)
CREATE TABLE IF NOT EXISTS agent_skills (
    tenant_id text NOT NULL,
    slug text NOT NULL,
    active_version_id text,
    revision integer NOT NULL DEFAULT 0,
    acl_roles text[] NOT NULL DEFAULT ARRAY['user'],
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, slug)
);

CREATE TABLE IF NOT EXISTS agent_skill_versions (
    version_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    slug text NOT NULL,
    version_num integer NOT NULL,
    description text NOT NULL,
    tags text[] NOT NULL DEFAULT ARRAY[]::text[],
    manifest jsonb NOT NULL DEFAULT '{}'::jsonb,
    oss_key text NOT NULL,
    sha256 text NOT NULL,
    resources jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, slug) REFERENCES agent_skills(tenant_id, slug) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS agent_skill_versions_by_slug
    ON agent_skill_versions(tenant_id, slug, version_num DESC);

GRANT SELECT ON agent_runs TO grafana_reader;

COMMIT;
