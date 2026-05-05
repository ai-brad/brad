-- Migration 005: Processed PR comments dedupe
--
-- Persistent record of which PR comments Brad has already acted on so we do
-- NOT re-invoke the review-fix agent on the same set every loop cycle.
-- Every GitHub comment has a stable integer id per kind, and the (pr, kind,
-- id) tuple is globally unique for Brad's dedupe purposes.
--
-- kind values:
--   'review'       : line-pinned review comments   (/pulls/{n}/comments)
--   'review_level' : PR review body comments       (/pulls/{n}/reviews)
--   'issue'        : PR-level issue comments       (/issues/{n}/comments)
--
-- skip_reason is NULL for comments Brad actually processed; it carries a
-- short tag (e.g. 'bot_author', 'nitpick') when we short-circuited the
-- processing, so future layers (bot allowlist, nitpick classifier) can share
-- the same table without introducing new state.
CREATE TABLE IF NOT EXISTS pr_processed_comments (
    pr_number    INTEGER NOT NULL,
    kind         TEXT    NOT NULL,
    comment_id   INTEGER NOT NULL,
    author       TEXT,
    processed_at TEXT    NOT NULL,
    skip_reason  TEXT,
    PRIMARY KEY (pr_number, kind, comment_id)
);

CREATE INDEX IF NOT EXISTS idx_pr_processed_comments_pr
    ON pr_processed_comments(pr_number);
