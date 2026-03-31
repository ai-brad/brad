-- Migration 001: Initial schema
CREATE TABLE IF NOT EXISTS executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_key TEXT NOT NULL,
    summary TEXT DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT DEFAULT 'running',
    pr_number INTEGER,
    pr_url TEXT,
    total_prompt_tokens INTEGER DEFAULT 0,
    total_completion_tokens INTEGER DEFAULT 0,
    total_cost REAL DEFAULT 0.0,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id INTEGER NOT NULL,
    phase TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT DEFAULT 'running',
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cost REAL DEFAULT 0.0,
    result_summary TEXT,
    FOREIGN KEY (execution_id) REFERENCES executions(id)
);

CREATE TABLE IF NOT EXISTS ci_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id INTEGER NOT NULL,
    run_id INTEGER,
    workflow_name TEXT,
    conclusion TEXT,
    logs_summary TEXT,
    checked_at TEXT NOT NULL,
    FOREIGN KEY (execution_id) REFERENCES executions(id)
);

CREATE INDEX IF NOT EXISTS idx_executions_issue ON executions(issue_key);
CREATE INDEX IF NOT EXISTS idx_steps_execution ON steps(execution_id);
CREATE INDEX IF NOT EXISTS idx_ci_runs_execution ON ci_runs(execution_id);
