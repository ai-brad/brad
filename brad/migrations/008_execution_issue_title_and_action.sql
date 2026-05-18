-- Migration 008: Separate execution issue title from action label.
-- issue_title stores the Jira ticket title.
-- action stores the short label for what this execution is doing.
ALTER TABLE executions ADD COLUMN issue_title TEXT DEFAULT NULL;
ALTER TABLE executions ADD COLUMN action TEXT DEFAULT NULL;
