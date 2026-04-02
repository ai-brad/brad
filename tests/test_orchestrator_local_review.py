from types import SimpleNamespace
from unittest.mock import Mock

from brad import db
from brad.orchestrator import BradOrchestrator, IssueState
from test_helpers import make_test_config


def make_orchestrator(temp_git_repo):
    cfg = make_test_config(temp_git_repo)
    orchestrator = BradOrchestrator(cfg)
    orchestrator.ticketing = Mock()
    orchestrator.code_repo = Mock()
    orchestrator.ci = Mock()
    orchestrator.observability = Mock()
    orchestrator.agent = Mock()
    orchestrator.repo = Mock()
    orchestrator.repo.repo_path = temp_git_repo
    return orchestrator, cfg


def make_state():
    return IssueState(
        issue_key="DEV-123",
        description="Implement feature",
        attachments=[],
        attachment_paths=[],
        branch_name="DEV-123",
        execution_id=1,
        pr_number=123,
    )


def test_handle_local_review_returns_changes_requested(temp_git_repo, monkeypatch):
    orchestrator, _ = make_orchestrator(temp_git_repo)
    state = make_state()

    monkeypatch.setattr(
        "brad.orchestrator.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout="diff --git a/x b/x\n+change"),
    )
    monkeypatch.setattr(db, "create_step", lambda *args, **kwargs: 1)
    monkeypatch.setattr(db, "finish_step", lambda *args, **kwargs: None)
    monkeypatch.setattr(db, "update_execution_costs", lambda *args, **kwargs: None)

    invoke_local_review = Mock(
        return_value={
            "action": "changes_requested",
            "message": "Need better error handling",
        }
    )
    orchestrator.agent.invoke_local_review = invoke_local_review
    ticket_comment = Mock()
    orchestrator.ticketing.comment = ticket_comment

    result = orchestrator._handle_local_review(state)

    assert result == {
        "action": "changes_requested",
        "message": "Need better error handling",
    }
    ticket_comment.assert_called_once()


def test_implementation_phase_routes_failed_local_review_into_fix_loop(
    temp_git_repo, monkeypatch
):
    orchestrator, _ = make_orchestrator(temp_git_repo)
    state = make_state()
    state.pr_number = None

    orchestrator.repo.reset_to_clean_state = Mock()
    orchestrator.repo.branch_exists_remote.return_value = False
    orchestrator.repo.prepare_branch = Mock()
    invoke_implementation = Mock(
        return_value={
            "action": "success",
            "message": "Created PR #123",
            "pr_number": 123,
            "pr_url": "https://github.com/owner/repo/pull/123",
            "_response_id": "resp-1",
        }
    )
    orchestrator.agent.invoke_implementation = invoke_implementation
    verify_and_ensure_pr = Mock(
        return_value=(123, "https://github.com/owner/repo/pull/123")
    )
    orchestrator._verify_and_ensure_pr = verify_and_ensure_pr
    handle_local_review = Mock(
        return_value={
            "action": "changes_requested",
            "message": "Please add a guard clause",
        }
    )
    orchestrator._handle_local_review = handle_local_review
    handle_local_review_fix = Mock(return_value=True)
    orchestrator._handle_local_review_fix = handle_local_review_fix
    handle_ci_monitoring = Mock()
    orchestrator._handle_ci_monitoring = handle_ci_monitoring

    monkeypatch.setattr(orchestrator, "_record_step", lambda *args, **kwargs: None)
    monkeypatch.setattr(db, "update_execution_pr", lambda *args, **kwargs: None)

    orchestrator._handle_implementation_phase(state)

    handle_local_review_fix.assert_called_once_with(state, "Please add a guard clause")
    handle_ci_monitoring.assert_not_called()


def test_local_review_fix_reruns_review_and_then_starts_ci(temp_git_repo, monkeypatch):
    orchestrator, _ = make_orchestrator(temp_git_repo)
    state = make_state()

    orchestrator.repo.checkout_branch = Mock()
    orchestrator.repo._run_git = Mock()
    invoke_local_review_fix = Mock(
        return_value={
            "action": "fixed",
            "message": "Added the missing guard clause",
            "_response_id": "resp-2",
        }
    )
    orchestrator.agent.invoke_local_review_fix = invoke_local_review_fix
    handle_local_review = Mock(
        return_value={
            "action": "approved",
            "message": "Looks good now",
        }
    )
    orchestrator._handle_local_review = handle_local_review
    handle_ci_monitoring = Mock()
    orchestrator._handle_ci_monitoring = handle_ci_monitoring

    monkeypatch.setattr(orchestrator, "_record_step", lambda *args, **kwargs: None)

    result = orchestrator._handle_local_review_fix(state, "Please add a guard clause")

    assert result is True
    assert state.local_review_fix_count == 1
    handle_local_review.assert_called_once_with(state)
    handle_ci_monitoring.assert_called_once_with(state)


def test_local_review_fix_stops_at_iteration_limit(temp_git_repo):
    orchestrator, cfg = make_orchestrator(temp_git_repo)
    state = make_state()
    state.local_review_fix_count = cfg.max_review_fix_iterations

    invoke_local_review_fix = Mock()
    orchestrator.agent.invoke_local_review_fix = invoke_local_review_fix
    ticket_comment = Mock()
    orchestrator.ticketing.comment = ticket_comment

    result = orchestrator._handle_local_review_fix(state, "Please add a guard clause")

    assert result is False
    invoke_local_review_fix.assert_not_called()
    ticket_comment.assert_called_once()
