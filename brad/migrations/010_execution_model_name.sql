-- Migration 010: Persist the model used for each execution.
ALTER TABLE executions ADD COLUMN model_name TEXT DEFAULT NULL;

UPDATE executions
   SET model_name = COALESCE(NULLIF(model_name, ''), 'gpt-5.4')
 WHERE model_name IS NULL OR model_name = '';
