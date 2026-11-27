BEGIN;

-- Persisted user feedback (thumbs-up / thumbs-down) on assistant replies
CREATE TABLE IF NOT EXISTS message_feedback (
    feedback_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    thread_id text NOT NULL REFERENCES agent_threads(thread_id) ON DELETE CASCADE,
    actor_id text NOT NULL,
    trace_id text NOT NULL,
    msg_id bigint REFERENCES session_messages(msg_id) ON DELETE SET NULL,
    rating text NOT NULL CHECK (rating IN ('up', 'down')),
    reason text NOT NULL DEFAULT '',
    answer_excerpt text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, thread_id, actor_id, trace_id)
);
ALTER TABLE message_feedback DROP CONSTRAINT IF EXISTS message_feedback_rating_check;
ALTER TABLE message_feedback ADD CONSTRAINT message_feedback_rating_check CHECK (rating IN ('up', 'down'));

CREATE INDEX IF NOT EXISTS message_feedback_by_tenant_time
    ON message_feedback(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS message_feedback_by_thread
    ON message_feedback(thread_id, trace_id);

GRANT SELECT ON message_feedback TO grafana_reader;

COMMIT;
