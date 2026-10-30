-- Additions to `exports` for the Vault page and the worker queue.
--
-- The base schema holds the request and its status. The Vault page also
-- shows the download filename and when the job finished, and the worker needs
-- to find the one job it should run next.

ALTER TABLE exports ADD COLUMN name TEXT;            -- download filename
ALTER TABLE exports ADD COLUMN duration_ms INTEGER;
ALTER TABLE exports ADD COLUMN finished_at TEXT;
ALTER TABLE exports ADD COLUMN segment_count INTEGER;
ALTER TABLE exports ADD COLUMN has_gaps INTEGER DEFAULT 0;

-- The worker runs one job at a time and asks for the oldest queued one.
CREATE INDEX idx_exports_status ON exports(status, created_at);
