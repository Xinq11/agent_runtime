-- Idempotent migration: remove `tenant_id` from core (and any existing extension/demo) tables
-- and add `tool_approvals.run_id` for per-run LangGraph checkpoint isolation.
-- Runs pre-flight cross-tenant collision checks before modifying constraints so historical
-- approval records and OSS object keys are preserved safely without silent data loss.
BEGIN;

ALTER TABLE tool_approvals ADD COLUMN IF NOT EXISTS run_id text NOT NULL DEFAULT '';

DO $$
DECLARE
    dup_count bigint;
BEGIN
    -- 1. Pre-flight collision checks (abort cleanly if historical multi-tenant data collides)
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='tool_runs' AND column_name='tenant_id') THEN
        SELECT count(*) INTO dup_count FROM (SELECT op_key FROM tool_runs GROUP BY op_key HAVING count(*) > 1) s;
        IF dup_count > 0 THEN
            RAISE EXCEPTION 'Migration aborted: % duplicate op_key(s) detected across tenants in tool_runs', dup_count;
        END IF;
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='workspace_files' AND column_name='tenant_id') THEN
        SELECT count(*) INTO dup_count FROM (SELECT thread_id, logical_path FROM workspace_files GROUP BY thread_id, logical_path HAVING count(*) > 1) s;
        IF dup_count > 0 THEN
            RAISE EXCEPTION 'Migration aborted: % duplicate (thread_id, logical_path) detected across tenants in workspace_files', dup_count;
        END IF;
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='message_feedback' AND column_name='tenant_id') THEN
        SELECT count(*) INTO dup_count FROM (SELECT thread_id, actor_id, trace_id FROM message_feedback GROUP BY thread_id, actor_id, trace_id HAVING count(*) > 1) s;
        IF dup_count > 0 THEN
            RAISE EXCEPTION 'Migration aborted: % duplicate (thread_id, actor_id, trace_id) detected across tenants in message_feedback', dup_count;
        END IF;
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='rag_parents' AND column_name='tenant_id') THEN
        SELECT count(*) INTO dup_count FROM (SELECT parent_id FROM rag_parents GROUP BY parent_id HAVING count(*) > 1) s;
        IF dup_count > 0 THEN
            RAISE EXCEPTION 'Migration aborted: % duplicate parent_id(s) detected across tenants in rag_parents', dup_count;
        END IF;
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='rag_chunks' AND column_name='tenant_id') THEN
        SELECT count(*) INTO dup_count FROM (SELECT chunk_id FROM rag_chunks GROUP BY chunk_id HAVING count(*) > 1) s;
        IF dup_count > 0 THEN
            RAISE EXCEPTION 'Migration aborted: % duplicate chunk_id(s) detected across tenants in rag_chunks', dup_count;
        END IF;
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agent_skills' AND column_name='tenant_id') THEN
        SELECT count(*) INTO dup_count FROM (SELECT slug FROM agent_skills GROUP BY slug HAVING count(*) > 1) s;
        IF dup_count > 0 THEN
            RAISE EXCEPTION 'Migration aborted: % duplicate slug(s) detected across tenants in agent_skills', dup_count;
        END IF;
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oa_approvals' AND column_name='tenant_id') THEN
        SELECT count(*) INTO dup_count FROM (SELECT op_key FROM oa_approvals GROUP BY op_key HAVING count(*) > 1) s;
        IF dup_count > 0 THEN
            RAISE EXCEPTION 'Migration aborted: % duplicate op_key(s) detected across tenants in oa_approvals', dup_count;
        END IF;
    END IF;

    -- 2. Drop legacy composite FKs and migrate core tables
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='tool_approvals' AND column_name='tenant_id') THEN
        ALTER TABLE tool_approvals DROP CONSTRAINT IF EXISTS tool_approvals_tenant_id_actor_id_thread_id_fkey;
        DROP INDEX IF EXISTS tool_approvals_op;
        ALTER TABLE tool_approvals DROP COLUMN tenant_id;
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='tool_runs' AND column_name='tenant_id') THEN
        ALTER TABLE tool_runs DROP CONSTRAINT IF EXISTS tool_runs_tenant_id_actor_id_result_ref_fkey;
        ALTER TABLE tool_runs DROP CONSTRAINT IF EXISTS tool_runs_tenant_id_op_key_key;
        ALTER TABLE tool_runs DROP COLUMN tenant_id;
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='tool_outputs' AND column_name='tenant_id') THEN
        ALTER TABLE tool_outputs DROP CONSTRAINT IF EXISTS tool_outputs_tenant_id_resource_owner_ref_id_key;
        ALTER TABLE tool_outputs DROP COLUMN tenant_id;
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agent_threads' AND column_name='tenant_id') THEN
        ALTER TABLE agent_threads DROP CONSTRAINT IF EXISTS agent_threads_tenant_id_actor_id_thread_id_key;
        ALTER TABLE agent_threads DROP COLUMN tenant_id;
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agent_runs' AND column_name='tenant_id') THEN
        ALTER TABLE agent_runs DROP COLUMN tenant_id;
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='workspace_files' AND column_name='tenant_id') THEN
        ALTER TABLE workspace_files DROP CONSTRAINT IF EXISTS workspace_files_pkey;
        ALTER TABLE workspace_files DROP COLUMN tenant_id;
        ALTER TABLE workspace_files ADD PRIMARY KEY (thread_id, logical_path);
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='message_feedback' AND column_name='tenant_id') THEN
        ALTER TABLE message_feedback DROP CONSTRAINT IF EXISTS message_feedback_tenant_id_thread_id_actor_id_trace_id_key;
        DROP INDEX IF EXISTS message_feedback_by_tenant_time;
        ALTER TABLE message_feedback DROP COLUMN tenant_id;
    END IF;

    -- Ensure clean core constraints exist after migration
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'agent_threads'::regclass AND conname = 'agent_threads_actor_id_thread_id_key') THEN
        ALTER TABLE agent_threads ADD CONSTRAINT agent_threads_actor_id_thread_id_key UNIQUE (actor_id, thread_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'tool_approvals'::regclass AND conname = 'tool_approvals_actor_id_thread_id_fkey') THEN
        ALTER TABLE tool_approvals ADD CONSTRAINT tool_approvals_actor_id_thread_id_fkey
            FOREIGN KEY (actor_id, thread_id) REFERENCES agent_threads (actor_id, thread_id) ON DELETE CASCADE;
    END IF;
    CREATE INDEX IF NOT EXISTS tool_approvals_op ON tool_approvals (op_key);

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'tool_outputs'::regclass AND conname = 'tool_outputs_resource_owner_ref_id_key') THEN
        ALTER TABLE tool_outputs ADD CONSTRAINT tool_outputs_resource_owner_ref_id_key UNIQUE (resource_owner, ref_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'tool_runs'::regclass AND contype = 'p') THEN
        ALTER TABLE tool_runs ADD PRIMARY KEY (op_key);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'tool_runs'::regclass AND conname = 'tool_runs_actor_id_result_ref_fkey') THEN
        ALTER TABLE tool_runs ADD CONSTRAINT tool_runs_actor_id_result_ref_fkey
            FOREIGN KEY (actor_id, result_ref) REFERENCES tool_outputs (resource_owner, ref_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'message_feedback'::regclass AND conname = 'message_feedback_thread_id_actor_id_trace_id_key') THEN
        ALTER TABLE message_feedback ADD CONSTRAINT message_feedback_thread_id_actor_id_trace_id_key UNIQUE (thread_id, actor_id, trace_id);
    END IF;
    CREATE INDEX IF NOT EXISTS message_feedback_by_created_at ON message_feedback(created_at DESC);

    -- 3. Migrate optional extension & sample tables if already present in an upgraded DB
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='rag_chunks' AND column_name='tenant_id') THEN
        ALTER TABLE rag_chunks DROP CONSTRAINT IF EXISTS rag_chunks_tenant_id_parent_id_fkey;
        ALTER TABLE rag_chunks DROP CONSTRAINT IF EXISTS rag_chunks_pkey;
        DROP INDEX IF EXISTS rag_chunks_tenant_status;
        ALTER TABLE rag_chunks DROP COLUMN tenant_id;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='rag_parents' AND column_name='tenant_id') THEN
        ALTER TABLE rag_parents DROP CONSTRAINT IF EXISTS rag_parents_pkey;
        ALTER TABLE rag_parents DROP COLUMN tenant_id;
        ALTER TABLE rag_parents ADD PRIMARY KEY (parent_id);
    END IF;
    IF to_regclass('rag_chunks') IS NOT NULL THEN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'rag_chunks'::regclass AND contype = 'p') THEN
            ALTER TABLE rag_chunks ADD PRIMARY KEY (chunk_id);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'rag_chunks'::regclass AND conname = 'rag_chunks_parent_id_fkey') THEN
            ALTER TABLE rag_chunks ADD CONSTRAINT rag_chunks_parent_id_fkey
                FOREIGN KEY (parent_id) REFERENCES rag_parents (parent_id) ON DELETE CASCADE;
        END IF;
        CREATE INDEX IF NOT EXISTS rag_chunks_status_idx ON rag_chunks (status);
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agent_skill_versions' AND column_name='tenant_id') THEN
        ALTER TABLE agent_skill_versions DROP CONSTRAINT IF EXISTS agent_skill_versions_tenant_id_slug_fkey;
        DROP INDEX IF EXISTS agent_skill_versions_by_slug;
        ALTER TABLE agent_skill_versions DROP COLUMN tenant_id;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agent_skills' AND column_name='tenant_id') THEN
        ALTER TABLE agent_skills DROP CONSTRAINT IF EXISTS agent_skills_pkey;
        ALTER TABLE agent_skills DROP COLUMN tenant_id;
        ALTER TABLE agent_skills ADD PRIMARY KEY (slug);
    END IF;
    IF to_regclass('agent_skill_versions') IS NOT NULL THEN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'agent_skill_versions'::regclass AND conname = 'agent_skill_versions_slug_fkey') THEN
            ALTER TABLE agent_skill_versions ADD CONSTRAINT agent_skill_versions_slug_fkey
                FOREIGN KEY (slug) REFERENCES agent_skills (slug) ON DELETE CASCADE;
        END IF;
        CREATE INDEX IF NOT EXISTS agent_skill_versions_by_slug ON agent_skill_versions (slug, version_num DESC);
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oa_approvals' AND column_name='tenant_id') THEN
        ALTER TABLE oa_approvals DROP CONSTRAINT IF EXISTS oa_approvals_tenant_id_op_key_key;
        ALTER TABLE oa_approvals DROP COLUMN tenant_id;
        ALTER TABLE oa_approvals ADD CONSTRAINT oa_approvals_op_key_key UNIQUE (op_key);
    END IF;
END $$;

COMMIT;
