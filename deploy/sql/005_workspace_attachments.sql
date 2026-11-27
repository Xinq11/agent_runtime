BEGIN;

-- Sidecar extracted text pointer for binary workspace attachments (PDF / Word / Excel)
ALTER TABLE workspace_files
    ADD COLUMN IF NOT EXISTS extracted_oss_key text,
    ADD COLUMN IF NOT EXISTS extracted_chars integer NOT NULL DEFAULT 0;

UPDATE workspace_files
SET extracted_oss_key = oss_key
WHERE extracted_oss_key IS NULL;

COMMIT;
