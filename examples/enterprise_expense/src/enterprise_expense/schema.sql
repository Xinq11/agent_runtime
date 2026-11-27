CREATE TABLE IF NOT EXISTS oa_approvals (
    approval_id text PRIMARY KEY,
    thread_id text NOT NULL REFERENCES agent_threads ON DELETE CASCADE,
    applicant text NOT NULL,
    op_key text NOT NULL UNIQUE,
    title text NOT NULL,
    region text NOT NULL,
    amount numeric NOT NULL CHECK (amount >= 0),
    created_at timestamptz NOT NULL DEFAULT now()
);
