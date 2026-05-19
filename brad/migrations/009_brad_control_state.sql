-- Migration 009: Global Brad control state for stop/resume.
CREATE TABLE IF NOT EXISTS brad_control (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL DEFAULT 'running',
    reason TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT ''
);

INSERT OR IGNORE INTO brad_control (id, state, reason, updated_at, updated_by)
VALUES (1, 'running', '', '', '');
