"""
Tests for the requirements grooming phase:
- _parse_requirements_response parser
- _handle_requirements_phase routing (clarify / propose_scenarios / ready)
- BradSkipGrooming label bypass
- grooming status recorded in DB
"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from brad import db
from brad.agents.interface import AIAgentInterface
from brad.orchestrator import BradOrchestrator, IssueState
from test_helpers import make_test_config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    yield path


@pytest.fixture
def temp_git_repo(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    return tmp_path


@pytest.fixture
def orchestrator(temp_db, temp_git_repo):
    cfg = make_test_config(temp_git_repo)
    with patch("brad.orchestrator.build_harness") as mock_build:
        mock_harness = Mock()
        mock_harness.model_name = "gpt-4o-test"
        mock_harness.run.return_value = SimpleNamespace(
            text="",
            response_id="r1",
            usage=SimpleNamespace(
                prompt_tokens=0, completion_tokens=0, cached_tokens=0
            ),
        )
        mock_build.return_value = mock_harness
        orc = BradOrchestrator(cfg)

    orc.ticketing = Mock()
    orc.code_repo = Mock()
    orc.ci = Mock()
    orc.repo = Mock()
    orc.repo.repo_path = temp_git_repo
    orc.repo.get_head_commit.return_value = "abc123"
    return orc


def _make_state(execution_id, issue_key="TEST-1"):
    return IssueState(
        issue_key=issue_key,
        description="Add a dark-mode toggle to the settings page.",
        attachments=[],
        attachment_paths=[],
        branch_name=issue_key,
        execution_id=execution_id,
        cost_budget=150.0,
    )


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------


class TestParseRequirementsResponse:
    """Unit tests for _parse_requirements_response."""

    def setup_method(self):
        mock_harness = Mock()
        mock_harness.run.return_value = SimpleNamespace(
            text="",
            response_id="r1",
            usage=SimpleNamespace(
                prompt_tokens=0, completion_tokens=0, cached_tokens=0
            ),
        )
        self.iface = AIAgentInterface(mock_harness, Mock())

    def test_ready_first_line(self):
        result = self.iface._parse_requirements_response(
            "READY TO IMPLEMENT\nI will build the feature."
        )
        assert result["action"] == "ready"

    def test_ready_full_text(self):
        result = self.iface._parse_requirements_response(
            "I've analyzed the codebase.\n\nREADY TO IMPLEMENT: I'll add a toggle."
        )
        assert result["action"] == "ready"

    def test_ready_variant_phrase(self):
        result = self.iface._parse_requirements_response(
            "Requirements are clear. Proceeding."
        )
        assert result["action"] == "ready"

    def test_clarify_first_line(self):
        result = self.iface._parse_requirements_response(
            "CLARIFYING QUESTIONS:\n1. Which users should see this?"
        )
        assert result["action"] == "clarify"

    def test_clarify_early_in_output(self):
        # Model may write one sentence before the header
        result = self.iface._parse_requirements_response(
            "I need more information.\n\nCLARIFYING QUESTIONS:\n1. What should happen on mobile?"
        )
        assert result["action"] == "clarify"

    def test_propose_scenarios(self):
        result = self.iface._parse_requirements_response(
            "ACCEPTANCE CRITERIA:\nGIVEN a logged-in user\nWHEN they click settings\nTHEN they see the toggle."
        )
        assert result["action"] == "propose_scenarios"

    def test_error_prefix(self):
        result = self.iface._parse_requirements_response("ERROR: something went wrong")
        assert result["action"] == "error"

    def test_default_ready_on_unrecognized(self):
        result = self.iface._parse_requirements_response(
            "I looked at the code and it all seems clear."
        )
        assert result["action"] == "ready"

    def test_ready_takes_priority_over_given_when_then(self):
        """If READY TO IMPLEMENT appears before acceptance criteria keywords, prefer ready."""
        result = self.iface._parse_requirements_response(
            "READY TO IMPLEMENT\nGiven the existing code, when the user toggles, then it works."
        )
        assert result["action"] == "ready"


# ---------------------------------------------------------------------------
# _handle_requirements_phase tests
# ---------------------------------------------------------------------------


class TestHandleRequirementsPhase:
    def test_returns_true_on_ready(self, orchestrator, temp_db):
        exec_id = db.create_execution("TEST-1", "Test", cost_budget=150.0)
        state = _make_state(exec_id)

        orchestrator.agent.invoke_requirements_analysis = Mock(
            return_value={
                "action": "ready",
                "message": "READY TO IMPLEMENT",
                "details": "",
            }
        )

        with (
            patch("brad.orchestrator.get_cached_phase", return_value=None),
            patch("brad.orchestrator.set_cached_phase"),
        ):
            result = orchestrator._handle_requirements_phase(state)

        assert result is True
        orchestrator.ticketing.comment.assert_not_called()

    def test_returns_false_and_posts_comment_on_clarify(self, orchestrator, temp_db):
        exec_id = db.create_execution("TEST-2", "Test", cost_budget=150.0)
        state = _make_state(exec_id, "TEST-2")

        orchestrator.agent.invoke_requirements_analysis = Mock(
            return_value={
                "action": "clarify",
                "message": "CLARIFYING QUESTIONS:\n1. Which users?",
                "details": "",
            }
        )

        with (
            patch("brad.orchestrator.get_cached_phase", return_value=None),
            patch("brad.orchestrator.set_cached_phase"),
        ):
            result = orchestrator._handle_requirements_phase(state)

        assert result is False
        orchestrator.ticketing.comment.assert_called_once()
        comment_text = orchestrator.ticketing.comment.call_args[0][1]
        assert "CLARIFYING QUESTIONS" in comment_text

        # Execution should be finished with grooming status
        row = db.get_execution(exec_id)
        assert row["status"] == "grooming"
        assert row["current_phase"] == "awaiting_clarification"

    def test_clarification_count_increments(self, orchestrator, temp_db):
        exec_id = db.create_execution("TEST-3", "Test", cost_budget=150.0)
        state = _make_state(exec_id, "TEST-3")
        assert state.clarification_count == 0

        orchestrator.agent.invoke_requirements_analysis = Mock(
            return_value={
                "action": "clarify",
                "message": "CLARIFYING QUESTIONS:\n1. Scope?",
                "details": "",
            }
        )

        with (
            patch("brad.orchestrator.get_cached_phase", return_value=None),
            patch("brad.orchestrator.set_cached_phase"),
        ):
            orchestrator._handle_requirements_phase(state)

        assert state.clarification_count == 1

    def test_too_many_clarification_cycles_marks_stuck(self, orchestrator, temp_db):
        exec_id = db.create_execution("TEST-4", "Test", cost_budget=150.0)
        state = _make_state(exec_id, "TEST-4")
        state.clarification_count = 2  # one below max (max_clarification_cycles=3)

        orchestrator.agent.invoke_requirements_analysis = Mock(
            return_value={
                "action": "clarify",
                "message": "CLARIFYING QUESTIONS:\n1. Still unclear.",
                "details": "",
            }
        )

        with (
            patch("brad.orchestrator.get_cached_phase", return_value=None),
            patch("brad.orchestrator.set_cached_phase"),
        ):
            result = orchestrator._handle_requirements_phase(state)

        assert result is False
        # Should have posted two comments: the questions + the stuck message
        assert orchestrator.ticketing.comment.call_count == 2
        stuck_msg = orchestrator.ticketing.comment.call_args_list[1][0][1]
        assert "BradSkipGrooming" in stuck_msg

        row = db.get_execution(exec_id)
        assert row["status"] == "stuck"

    def test_returns_false_and_posts_on_propose_scenarios(self, orchestrator, temp_db):
        exec_id = db.create_execution("TEST-5", "Test", cost_budget=150.0)
        state = _make_state(exec_id, "TEST-5")

        orchestrator.agent.invoke_requirements_analysis = Mock(
            return_value={
                "action": "propose_scenarios",
                "message": "ACCEPTANCE CRITERIA:\nGIVEN ... WHEN ... THEN ...",
                "details": "",
            }
        )

        with (
            patch("brad.orchestrator.get_cached_phase", return_value=None),
            patch("brad.orchestrator.set_cached_phase"),
        ):
            result = orchestrator._handle_requirements_phase(state)

        assert result is False
        orchestrator.ticketing.comment.assert_called_once()

        row = db.get_execution(exec_id)
        assert row["status"] == "grooming"


# ---------------------------------------------------------------------------
# BradSkipGrooming label bypass
# ---------------------------------------------------------------------------


class TestBradSkipGrooming:
    def _make_issue(self, labels):
        return {
            "key": "TEST-10",
            "fields": {
                "summary": "Add feature X",
                "description": "Do the thing.",
                "updated": "2026-01-01",
                "attachment": [],
                "labels": labels,
            },
        }

    def test_skip_grooming_label_bypasses_requirements_phase(
        self, orchestrator, temp_db
    ):
        issue = self._make_issue(["BradSkipGrooming"])

        orchestrator.ticketing.fetch_issue.return_value = issue
        orchestrator.ticketing.remove_label = Mock()
        orchestrator.code_repo.pr_exists_for_branch.return_value = None
        orchestrator.repo.reset_to_clean_state = Mock()
        orchestrator.repo.branch_exists_remote.return_value = False
        orchestrator.repo.prepare_branch = Mock()
        orchestrator.repo.get_head_commit.return_value = "abc"
        orchestrator.repo.checkout_branch = Mock()

        # Make implementation finish cleanly
        orchestrator.agent.invoke_implementation = Mock(
            return_value={
                "action": "success",
                "pr_number": 42,
                "pr_url": "https://github.com/owner/repo/pull/42",
                "message": "Done",
                "_response_id": "r1",
                "_usage": SimpleNamespace(
                    prompt_tokens=10, completion_tokens=5, cached_tokens=0
                ),
            }
        )
        orchestrator.code_repo.pr_exists_for_branch.side_effect = [None, 42]
        orchestrator.code_repo.get_pr.return_value = {
            "state": "open",
            "head": {"ref": "TEST-10"},
        }
        orchestrator.ci.get_pr_checks.return_value = []

        # _handle_requirements_phase should never be called
        orchestrator._handle_requirements_phase = Mock(return_value=True)

        orchestrator._process_issue(issue)

        orchestrator._handle_requirements_phase.assert_not_called()
        # BradSkipGrooming label should have been removed
        remove_calls = [
            str(c) for c in orchestrator.ticketing.remove_label.call_args_list
        ]
        assert any("BradSkipGrooming" in c for c in remove_calls)

    def test_existing_pr_bypasses_grooming(self, orchestrator, temp_db):
        """If there's already a PR, skip grooming regardless of labels."""
        issue = self._make_issue([])  # no skip label

        orchestrator.ticketing.remove_label = Mock()
        orchestrator.code_repo.pr_exists_for_branch.return_value = 99  # existing PR

        orchestrator.agent.invoke_implementation = Mock(
            return_value={
                "action": "success",
                "pr_number": 99,
                "pr_url": "https://github.com/owner/repo/pull/99",
                "message": "Done",
                "_response_id": "r1",
                "_usage": SimpleNamespace(
                    prompt_tokens=10, completion_tokens=5, cached_tokens=0
                ),
            }
        )
        orchestrator.code_repo.get_pr.return_value = {
            "state": "open",
            "head": {"ref": "TEST-10"},
        }
        orchestrator.ci.get_pr_checks.return_value = []
        orchestrator.repo.reset_to_clean_state = Mock()
        orchestrator.repo.branch_exists_remote.return_value = True
        orchestrator.repo.prepare_branch = Mock()
        orchestrator.repo.checkout_branch = Mock()
        orchestrator.repo.get_head_commit.return_value = "abc"

        orchestrator._handle_requirements_phase = Mock(return_value=True)

        orchestrator._process_issue(issue)

        orchestrator._handle_requirements_phase.assert_not_called()
