-- Migration 004: Repository metadata cache
-- Stores learned knowledge about target repositories (dev instructions, test commands, etc.)
CREATE TABLE IF NOT EXISTS repo_metadata (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_path TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    source_file TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(repo_path, key)
);

CREATE INDEX IF NOT EXISTS idx_repo_metadata_repo ON repo_metadata(repo_path);
