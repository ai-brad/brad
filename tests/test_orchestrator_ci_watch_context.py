from unittest.mock import Mock

from brad import db
from brad.orchestrator import BradOrchestrator
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


def _stub_db(monkeypatch, executions=None, steps=None):
    monkeypatch.setattr(db, "create_execution", lambda *a, **kw: 99)
    monkeypatch.setattr(db, "finish_execution", lambda *a, **kw: None)
    monkeypatch.setattr(
        db, "get_executions_by_issue", lambda *_a, **_kw: executions or []
    )
    monkeypatch.setattr(db, "get_execution_steps", lambda *_a, **_kw: steps or [])


def test_watch_ci_after_push_synthesizes_state_with_jira_goal(
    temp_git_repo, monkeypatch
):
    orchestrator, _ = make_orchestrator(temp_git_repo)
    orchestrator.ticketing.fetch_issue = Mock(
        return_value={
            "fields": {
                "summary": "Wire batch endpoint",
                "description": "Make get_message_details_batch return one row per id.",
            }
        }
    )
    _stub_db(monkeypatch)

    captured = {}

    def fake_handle_ci_monitoring(state):
        captured["state"] = state

    orchestrator._handle_ci_monitoring = fake_handle_ci_monitoring

    orchestrator._watch_ci_after_push(pr_number=2355, branch_name="DEV-3205")

    state = captured["state"]
    assert state.issue_key == "DEV-3205"
    assert state.branch_name == "DEV-3205"
    assert state.pr_number == 2355
    assert state.execution_id == 99
    assert "Wire batch endpoint" in state.description
    assert "get_message_details_batch" in state.description


def test_watch_ci_after_push_survives_jira_failure(temp_git_repo, monkeypatch):
    orchestrator, _ = make_orchestrator(temp_git_repo)
    orchestrator.ticketing.fetch_issue = Mock(side_effect=RuntimeError("jira down"))
    _stub_db(monkeypatch)

    captured = {}
    orchestrator._handle_ci_monitoring = lambda state: captured.setdefault(
        "state", state
    )

    # Must not raise even though Jira blew up.
    orchestrator._watch_ci_after_push(pr_number=2355, branch_name="DEV-3205")

    assert captured["state"].description == ""


def test_watch_ci_after_push_includes_prior_activity_digest(temp_git_repo, monkeypatch):
    orchestrator, _ = make_orchestrator(temp_git_repo)
    orchestrator.ticketing.fetch_issue = Mock(return_value=None)  # Jira returns nothing
    _stub_db(
        monkeypatch,
        executions=[
            {"id": 7, "started_at": "2026-04-28T10:00:00Z", "status": "completed"},
        ],
        steps=[
            {
                "phase": "implementation",
                "status": "success",
                "result_summary": "Added batch handler",
            },
            {
                "phase": "ci_fix",
                "status": "fixed",
                "result_summary": "Bumped pytest assert tolerance",
            },
        ],
    )

    captured = {}
    orchestrator._handle_ci_monitoring = lambda state: captured.setdefault(
        "state", state
    )

    orchestrator._watch_ci_after_push(pr_number=2355, branch_name="DEV-3205")

    desc = captured["state"].description
    assert "Prior brad activity" in desc
    assert "implementation" in desc
    assert "Bumped pytest assert tolerance" in desc
