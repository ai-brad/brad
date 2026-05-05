"""Tests for persistent PR-comment dedupe (pr_processed_comments).

Covers:
- DB helpers: filter/mark round-trip, kind isolation, PR isolation, idempotency.
- Orchestrator integration: already-processed comments never reach the batch
  processors or the review-fix agent, eliminating the attempt-1→N loop seen
  on PR #2363 when the agent reports 'already addressed'.
"""

from unittest.mock import Mock, MagicMock, patch

import pytest

from brad import db
from test_helpers import make_test_config


@pytest.fixture
def fresh_db(tmp_path):
    """Initialize a fresh SQLite DB for each test."""
    db_path = str(tmp_path / "test_brad.db")
    db.init_db(db_path)
    return db_path


@pytest.fixture
def temp_git_repo(tmp_path):
    """Minimal 'git repo' layout for tests that build a BradOrchestrator —
    matches the fixture in test_speed_optimizations.py so RepoManager's
    bootstrap-clone check is satisfied."""
    (tmp_path / ".git").mkdir()
    return tmp_path


# =========================================================================
# DB helpers
# =========================================================================


class TestProcessedCommentsDB:
    def test_empty_db_returns_all_comments(self, fresh_db):
        comments = [{"id": 1}, {"id": 2}, {"id": 3}]
        assert db.filter_unprocessed_comments(100, "review", comments) == comments

    def test_mark_then_filter_drops_known(self, fresh_db):
        comments = [
            {"id": 1, "user": {"login": "alice"}},
            {"id": 2, "user": {"login": "bob"}},
            {"id": 3, "user": {"login": "alice"}},
        ]
        assert db.mark_comments_processed(100, "review", comments[:2]) == 2

        remaining = db.filter_unprocessed_comments(100, "review", comments)
        assert [c["id"] for c in remaining] == [3]

    def test_kinds_are_isolated(self, fresh_db):
        """An id marked under 'review' must not dedupe the same id under 'issue'."""
        db.mark_comments_processed(100, "review", [{"id": 42}])

        assert db.filter_unprocessed_comments(100, "review", [{"id": 42}]) == []
        assert db.filter_unprocessed_comments(100, "issue", [{"id": 42}]) == [
            {"id": 42}
        ]
        assert db.filter_unprocessed_comments(100, "review_level", [{"id": 42}]) == [
            {"id": 42}
        ]

    def test_pr_numbers_are_isolated(self, fresh_db):
        db.mark_comments_processed(100, "review", [{"id": 42}])
        assert db.filter_unprocessed_comments(200, "review", [{"id": 42}]) == [
            {"id": 42}
        ]

    def test_mark_is_idempotent(self, fresh_db):
        """Re-marking the same (pr, kind, id) must not create duplicate rows,
        preserving the original skip_reason/processed_at."""
        assert db.mark_comments_processed(100, "review", [{"id": 1}]) == 1
        # Second call: INSERT OR IGNORE -> 0 rows inserted.
        assert db.mark_comments_processed(100, "review", [{"id": 1}]) == 0
        assert db.filter_unprocessed_comments(100, "review", [{"id": 1}]) == []

    def test_mark_skips_comments_without_id(self, fresh_db):
        inserted = db.mark_comments_processed(
            100, "review", [{"id": 1}, {"no_id": True}, {"id": 3}]
        )
        assert inserted == 2

    def test_unknown_kind_is_fail_open_on_filter(self, fresh_db):
        """A caller bug (typo'd kind) must never silently skip all comments."""
        comments = [{"id": 1}]
        assert db.filter_unprocessed_comments(100, "revview", comments) == comments

    def test_unknown_kind_raises_on_mark(self, fresh_db):
        """But marking with a bad kind is a programming error — raise."""
        with pytest.raises(ValueError):
            db.mark_comments_processed(100, "bogus", [{"id": 1}])

    def test_skip_reason_is_stored(self, fresh_db):
        """skip_reason is persisted so later layers (bot allowlist, nitpick
        classifier) can reuse the same table without a schema change."""
        db.mark_comments_processed(100, "review", [{"id": 1}], skip_reason="bot_author")
        # Still deduped, regardless of skip_reason value.
        assert db.filter_unprocessed_comments(100, "review", [{"id": 1}]) == []


# =========================================================================
# Orchestrator integration
# =========================================================================


def _build_orch(git_repo):
    """Build a BradOrchestrator with enough mocks for the review-comment paths.

    ``git_repo`` must be a path that already contains a ``.git`` dir so
    ``RepoManager`` does not try to self-bootstrap during construction.
    """
    from brad.orchestrator import BradOrchestrator

    cfg = make_test_config(git_repo)
    db.init_db(cfg.db_path)
    orch = BradOrchestrator(cfg)
    orch.ticketing = Mock()
    orch.code_repo = Mock()
    orch.ci = Mock()
    orch.observability = Mock()
    orch.agent = Mock()
    orch.repo = MagicMock()
    orch.repo.repo_path = str(git_repo)
    orch.repo.is_clean_working_tree.return_value = True
    return orch


class TestOrchestratorDedupe:
    def test_process_review_comments_skips_already_processed(self, temp_git_repo):
        """All three kinds pre-populated in DB; orchestrator must see zero
        work left and never invoke the batch processors."""
        orch = _build_orch(temp_git_repo)
        orch.code_repo.get_brad_prs.return_value = [
            {"number": 2363, "head": {"ref": "DEV-3517"}}
        ]
        orch.code_repo.get_review_comments_needing_response.return_value = [
            {"id": 10, "body": "nit", "user": {"login": "rev"}},
        ]
        orch.code_repo.get_review_level_comments_needing_response.return_value = [
            {"id": 20, "body": "lgtm w/ nits", "user": {"login": "rev"}},
        ]
        orch.code_repo.get_issue_comments_needing_response.return_value = [
            {"id": 30, "body": "ping", "user": {"login": "rev"}},
        ]

        db.mark_comments_processed(2363, "review", [{"id": 10}])
        db.mark_comments_processed(2363, "review_level", [{"id": 20}])
        db.mark_comments_processed(2363, "issue", [{"id": 30}])

        with (
            patch.object(orch, "_process_review_comments_batch") as rcb,
            patch.object(orch, "_process_issue_comments_batch") as icb,
        ):
            orch._process_review_comments()

        rcb.assert_not_called()
        icb.assert_not_called()
        # No git activity either — we bailed before the branch checkout.
        orch.repo.checkout_branch.assert_not_called()

    def test_process_review_comments_marks_new_comments_processed(self, temp_git_repo):
        """After a successful batch, the comments must land in the DB so the
        next cycle is a no-op."""
        orch = _build_orch(temp_git_repo)
        orch.code_repo.get_brad_prs.return_value = [
            {"number": 2363, "head": {"ref": "DEV-3517"}}
        ]
        orch.code_repo.get_review_comments_needing_response.return_value = [
            {"id": 11, "body": "fix", "user": {"login": "rev"}},
        ]
        orch.code_repo.get_review_level_comments_needing_response.return_value = []
        orch.code_repo.get_issue_comments_needing_response.return_value = []

        with patch.object(orch, "_process_review_comments_batch") as rcb:
            orch._process_review_comments()

        rcb.assert_called_once()
        # Now persisted:
        assert db.filter_unprocessed_comments(2363, "review", [{"id": 11}]) == []

    def test_process_review_comments_lets_new_ids_through(self, temp_git_repo):
        """Id 10 is already processed, id 11 is new — only 11 should reach
        the batch processor."""
        orch = _build_orch(temp_git_repo)
        orch.code_repo.get_brad_prs.return_value = [
            {"number": 2363, "head": {"ref": "DEV-3517"}}
        ]
        orch.code_repo.get_review_comments_needing_response.return_value = [
            {"id": 10, "body": "old", "user": {"login": "rev"}},
            {"id": 11, "body": "new", "user": {"login": "rev"}},
        ]
        orch.code_repo.get_review_level_comments_needing_response.return_value = []
        orch.code_repo.get_issue_comments_needing_response.return_value = []
        db.mark_comments_processed(2363, "review", [{"id": 10}])

        with patch.object(orch, "_process_review_comments_batch") as rcb:
            orch._process_review_comments()

        rcb.assert_called_once()
        # Second positional arg is the comments list.
        _, _, passed_comments = rcb.call_args.args
        assert [c["id"] for c in passed_comments] == [11]
