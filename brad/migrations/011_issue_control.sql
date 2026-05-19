-- Migration 011: Per-issue Brad stop control.
CREATE TABLE IF NOT EXISTS issue_control (
    issue_key TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'running',
    reason TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT ''
);
