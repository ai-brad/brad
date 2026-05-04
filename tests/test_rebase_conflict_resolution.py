"""Tests for the orchestrator's _resolve_rebase_conflicts loop.

The orchestrator is heavy to construct, so these tests instantiate
:class:`BradOrchestrator` only enough to exercise ``_resolve_rebase_conflicts``
with stubbed-in ``self.repo``, ``self.agent``, ``self.code_repo`` and
``self.ticketing`` collaborators. The point is to verify the control-flow
contract: success force-pushes + watches CI, stuck aborts and comments,
remaining markers abort and comment, iteration cap aborts.
"""

from unittest.mock import Mock, patch

import pytest

from brad.orchestrator import BradOrchestrator


@pytest.fixture
def orch():
    """Build a BradOrchestrator without going through __init__."""
    o = BradOrchestrator.__new__(BradOrchestrator)
    o.logger = Mock()
    o.repo = Mock()
    o.repo.repo_path = "/tmp/fake-repo"
    # Defaults for context-gathering (each method must return a string).
    o.repo.read_conflicted_file.return_value = "no markers"
    o.repo.show_stage_blob.return_value = ""
    o.repo.blame_range.return_value = ""
    o.repo.log_messages.return_value = ""
    o.repo.list_unmerged_files.return_value = []
    o.agent = Mock()
    o.code_repo = Mock()
    o.ticketing = Mock()
    o.cfg = Mock()
    o.cfg.azure_openai_model = "gpt-4o"
    o._repo_dev_instructions = ""
    # _calculate_cost reads from db.get_model_cost — stub it instead.
    o._calculate_cost = lambda usage: 0.0
    o._comment_on_pr = Mock()
    o._watch_ci_after_push = Mock()
    return o


def _ai_resolved(summary="merged both sides"):
    return {
        "action": "resolved",
        "message": f"RESOLVED: {summary}",
        "summary": summary,
        "_response_id": "resp-1",
        "_usage": None,
    }


def _ai_stuck(summary="cannot tell"):
    return {
        "action": "stuck",
        "message": f"STUCK: {summary}",
        "summary": summary,
        "_response_id": "resp-1",
        "_usage": None,
    }


# ---------------------------------------------------------------------------


def test_happy_path_resolves_pushes_and_watches_ci(orch):
    """Single conflicted file, agent resolves cleanly, force-push succeeds."""
    orch.repo.read_conflicted_file.return_value = "no markers here"
    orch.repo.list_unmerged_files.return_value = ["foo.py"]
    orch.repo.show_stage_blob.return_value = ""
    orch.repo.blame_range.return_value = ""
    orch.repo.log_messages.return_value = ""
    orch.repo.continue_rebase.return_value = {"rebased": True}
    orch.repo.force_push_with_lease.return_value = {"pushed": True}
    orch.agent.invoke_conflict_resolution.return_value = _ai_resolved()

    with patch("brad.orchestrator.db") as fake_db:
        fake_db.create_execution.return_value = 1
        fake_db.create_step.return_value = 11

        orch._resolve_rebase_conflicts(
            pr_number=42,
            branch_name="DEV-42",
            base_branch="main",
            conflicted_files=["foo.py"],
        )

    assert orch.agent.invoke_conflict_resolution.call_count == 1
    orch.repo.continue_rebase.assert_called_once()
    orch.repo.force_push_with_lease.assert_called_once_with("DEV-42")
    orch._watch_ci_after_push.assert_called_once_with(42, "DEV-42")
    # No abort on happy path.
    orch.repo.abort_rebase.assert_not_called()


def test_agent_stuck_aborts_and_comments(orch):
    orch.agent.invoke_conflict_resolution.return_value = _ai_stuck("dunno")

    with patch("brad.orchestrator.db") as fake_db:
        fake_db.create_execution.return_value = 0
        orch._resolve_rebase_conflicts(
            pr_number=42,
            branch_name="DEV-42",
            base_branch="main",
            conflicted_files=["foo.py"],
        )

    orch.repo.abort_rebase.assert_called_once()
    orch.repo.continue_rebase.assert_not_called()
    orch.repo.force_push_with_lease.assert_not_called()
    orch._comment_on_pr.assert_called_once()
    body = orch._comment_on_pr.call_args[0][1]
    assert "stuck" in body.lower()


def test_remaining_markers_after_resolved_aborts(orch):
    """Agent says RESOLVED but markers remain → abort, do not push."""
    orch.repo.read_conflicted_file.return_value = (
        "<<<<<<< HEAD\nstill here\n>>>>>>> branch\n"
    )
    orch.repo.list_unmerged_files.return_value = ["foo.py"]
    orch.repo.show_stage_blob.return_value = ""
    orch.repo.blame_range.return_value = ""
    orch.repo.log_messages.return_value = ""
    orch.agent.invoke_conflict_resolution.return_value = _ai_resolved()

    with patch("brad.orchestrator.db") as fake_db:
        fake_db.create_execution.return_value = 0
        orch._resolve_rebase_conflicts(
            pr_number=42,
            branch_name="DEV-42",
            base_branch="main",
            conflicted_files=["foo.py"],
        )

    orch.repo.abort_rebase.assert_called_once()
    orch.repo.continue_rebase.assert_not_called()
    orch.repo.force_push_with_lease.assert_not_called()
    orch._comment_on_pr.assert_called_once()
    assert "marker" in orch._comment_on_pr.call_args[0][1].lower()


def test_too_many_files_skips_resolution(orch):
    files = [f"f{i}.py" for i in range(BradOrchestrator.MAX_CONFLICT_FILES + 1)]
    orch._resolve_rebase_conflicts(
        pr_number=42,
        branch_name="DEV-42",
        base_branch="main",
        conflicted_files=files,
    )
    orch.repo.abort_rebase.assert_called_once()
    orch.agent.invoke_conflict_resolution.assert_not_called()
    orch._comment_on_pr.assert_called_once()


def test_iterates_when_continue_surfaces_more_conflicts(orch):
    """First continue_rebase reports new conflicts → loop with new file set."""
    orch.repo.read_conflicted_file.return_value = "no markers"
    orch.repo.list_unmerged_files.return_value = []
    orch.repo.show_stage_blob.return_value = ""
    orch.repo.blame_range.return_value = ""
    orch.repo.log_messages.return_value = ""
    orch.repo.continue_rebase.side_effect = [
        {"rebased": False, "conflict": True, "conflicted_files": ["bar.py"]},
        {"rebased": True},
    ]
    orch.repo.force_push_with_lease.return_value = {"pushed": True}
    orch.agent.invoke_conflict_resolution.return_value = _ai_resolved()

    with patch("brad.orchestrator.db") as fake_db:
        fake_db.create_execution.return_value = 0
        orch._resolve_rebase_conflicts(
            pr_number=42,
            branch_name="DEV-42",
            base_branch="main",
            conflicted_files=["foo.py"],
        )

    assert orch.agent.invoke_conflict_resolution.call_count == 2
    assert orch.repo.continue_rebase.call_count == 2
    orch.repo.force_push_with_lease.assert_called_once_with("DEV-42")
    orch._watch_ci_after_push.assert_called_once_with(42, "DEV-42")


def test_push_failure_does_not_watch_ci(orch):
    orch.repo.read_conflicted_file.return_value = "no markers"
    orch.repo.list_unmerged_files.return_value = []
    orch.repo.show_stage_blob.return_value = ""
    orch.repo.blame_range.return_value = ""
    orch.repo.log_messages.return_value = ""
    orch.repo.continue_rebase.return_value = {"rebased": True}
    orch.repo.force_push_with_lease.return_value = {"pushed": False, "error": "stale"}
    orch.agent.invoke_conflict_resolution.return_value = _ai_resolved()

    with patch("brad.orchestrator.db") as fake_db:
        fake_db.create_execution.return_value = 0
        orch._resolve_rebase_conflicts(
            pr_number=42,
            branch_name="DEV-42",
            base_branch="main",
            conflicted_files=["foo.py"],
        )

    orch._watch_ci_after_push.assert_not_called()
    orch._comment_on_pr.assert_called_once()


def test_files_still_have_conflict_markers(orch):
    orch.repo.read_conflicted_file.side_effect = lambda p: {
        "clean.py": "def x(): pass\n",
        "dirty.py": "<<<<<<< HEAD\nfoo\n=======\nbar\n>>>>>>> theirs\n",
    }[p]
    bad = orch._files_still_have_conflict_markers(["clean.py", "dirty.py"])
    assert bad == ["dirty.py"]
