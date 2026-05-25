-- Migration 012: Persist restart-summary metadata on executions.
ALTER TABLE executions ADD COLUMN continuation_summary TEXT DEFAULT '';
ALTER TABLE executions ADD COLUMN continuation_summary_source TEXT DEFAULT '';
ALTER TABLE executions ADD COLUMN continuation_summary_model_name TEXT DEFAULT '';
ALTER TABLE executions ADD COLUMN continuation_summary_prompt_tokens INTEGER DEFAULT 0;
ALTER TABLE executions ADD COLUMN continuation_summary_cached_prompt_tokens INTEGER DEFAULT 0;
ALTER TABLE executions ADD COLUMN continuation_summary_completion_tokens INTEGER DEFAULT 0;
ALTER TABLE executions ADD COLUMN continuation_summary_total_tokens INTEGER DEFAULT 0;
ALTER TABLE executions ADD COLUMN continuation_summary_cost REAL DEFAULT 0.0;
ALTER TABLE executions ADD COLUMN continuation_summary_updated_at TEXT DEFAULT '';
