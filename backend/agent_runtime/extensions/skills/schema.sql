-- Optional declarative Skills extension schema (PG catalog + immutable OSS versions).
-- Bootstrapped ONLY when `SkillsExtension` is enabled; core SDK has zero Skills table dependency.
CREATE TABLE IF NOT EXISTS agent_skills (
    slug text PRIMARY KEY,
    active_version_id text,
    revision integer NOT NULL DEFAULT 0,
    acl_roles text[] NOT NULL DEFAULT ARRAY['user'],
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_skill_versions (
    version_id text PRIMARY KEY,
    slug text NOT NULL REFERENCES agent_skills (slug) ON DELETE CASCADE,
    version_num integer NOT NULL,
    description text NOT NULL,
    tags text[] NOT NULL DEFAULT ARRAY[]::text[],
    manifest jsonb NOT NULL DEFAULT '{}'::jsonb,
    oss_key text NOT NULL,
    sha256 text NOT NULL,
    resources jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agent_skill_versions_by_slug
    ON agent_skill_versions (slug, version_num DESC);
