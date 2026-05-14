-- Migration 006: execution liveness, ownership, and memory diagnostics
ALTER TABLE executions ADD COLUMN worker_id TEXT DEFAULT NULL;
ALTER TABLE executions ADD COLUMN worker_pid INTEGER DEFAULT NULL;
ALTER TABLE executions ADD COLUMN last_progress_at TEXT DEFAULT NULL;
ALTER TABLE executions ADD COLUMN last_heartbeat_at TEXT DEFAULT NULL;
ALTER TABLE executions ADD COLUMN last_memory_rss_bytes INTEGER DEFAULT NULL;
ALTER TABLE executions ADD COLUMN peak_memory_rss_bytes INTEGER DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_executions_status_heartbeat
ON executions(status, last_heartbeat_at);
