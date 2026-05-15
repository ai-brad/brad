ALTER TABLE executions ADD COLUMN total_cached_prompt_tokens INTEGER DEFAULT 0;
ALTER TABLE steps ADD COLUMN cached_prompt_tokens INTEGER DEFAULT 0;
ALTER TABLE model_costs ADD COLUMN cached_prompt_cost_per_1k REAL DEFAULT NULL;
