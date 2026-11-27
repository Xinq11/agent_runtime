-- Optional RAG extension schema (requires pgvector >= 0.5).
-- Bootstrapped ONLY when `RagExtension` is enabled; core SDK has zero pgvector dependency.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS rag_parents (
    parent_id text PRIMARY KEY,
    doc_title text NOT NULL,
    section_path text NOT NULL,
    doc_url text NOT NULL,
    parent_text text NOT NULL,
    oss_key text
);

CREATE TABLE IF NOT EXISTS rag_chunks (
    chunk_id text PRIMARY KEY,
    parent_id text NOT NULL REFERENCES rag_parents (parent_id) ON DELETE CASCADE,
    content text NOT NULL,
    acl_roles text[] NOT NULL DEFAULT '{}',
    status text NOT NULL DEFAULT 'active',
    embedding vector(256) NOT NULL,
    fts_terms text NOT NULL,
    fts_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, fts_terms)) STORED
);

CREATE INDEX IF NOT EXISTS rag_chunks_vector ON rag_chunks USING hnsw (embedding vector_cosine_ops)
    WHERE status = 'active';
CREATE INDEX IF NOT EXISTS rag_chunks_fts ON rag_chunks USING gin (fts_tsv);
CREATE INDEX IF NOT EXISTS rag_chunks_acl ON rag_chunks USING gin (acl_roles);
CREATE INDEX IF NOT EXISTS rag_chunks_status_idx ON rag_chunks (status);
