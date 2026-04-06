-- Migration 003: Granular execution phases and step detail tracking
-- Add current phase tracking to executions for live dashboard display
ALTER TABLE executions ADD COLUMN current_phase TEXT DEFAULT NULL;
ALTER TABLE executions ADD COLUMN current_phase_detail TEXT DEFAULT NULL;
ALTER TABLE executions ADD COLUMN cost_budget REAL DEFAULT 2.0;
-- Add detail and iteration columns to steps for richer tracking
ALTER TABLE steps ADD COLUMN detail TEXT DEFAULT NULL;
ALTER TABLE steps ADD COLUMN iteration INTEGER DEFAULT 0;
