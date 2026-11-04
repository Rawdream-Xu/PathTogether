-- Existing requests remain readable; new submissions require evidence in the HTTP layer.
ALTER TABLE format_requests
    ADD COLUMN IF NOT EXISTS manufacturer TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS sample_url TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS sample_access_code TEXT NOT NULL DEFAULT '';
