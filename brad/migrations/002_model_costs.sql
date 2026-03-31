-- Migration 002: Model costs table with TTL
CREATE TABLE IF NOT EXISTS model_costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_pattern TEXT NOT NULL UNIQUE,
    prompt_cost_per_1k REAL NOT NULL DEFAULT 0.0,
    completion_cost_per_1k REAL NOT NULL DEFAULT 0.0,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    source TEXT DEFAULT 'default'
);
