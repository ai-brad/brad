import tempfile
from unittest.mock import Mock, patch

import pytest

from brad import db
from brad.adapters.ci_cd.github_actions_adapter import GitHubActionsAdapter
from brad.gui.app import create_app
from brad.orchestrator import BradOrchestrator
from test_helpers import make_test_config


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    import os
    os.close(fd)
    db.init_db(path)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def test_issue_control_state_round_trip(temp_db):
    state = db.get_issue_control_state("TEST-123")
    assert state["state"] == "running"
    assert not db.is_brad_stopped("TEST-123")

    stopped = db.set_issue_control_state("TEST-123", "stopped", reason="operator pause", updated_by="ui")
    assert stopped["state"] == "stopped"
    assert stopped["reason"] == "operator pause"
    assert db.is_brad_stopped("TEST-123")

    running = db.set_issue_control_state("TEST-123", "running", updated_by="ui")
    assert running["state"] == "running"
    assert not db.is_brad_stopped("TEST-123")


def test_issue_stop_and_resume_routes_update_only_that_issue(temp_db):
    stop_id = db.create_execution("TEST-STOP", "Stop feature", cost_budget=150.0)
    other_id = db.create_execution("TEST-OTHER", "Other feature", cost_budget=150.0)

    app = create_app(temp_db)
    client = app.test_client()

    stop_resp = client.post(f"/api/execution/{stop_id}/stop", data={"reason": "operator stop"})
    assert stop_resp.status_code in (302, 303)

    stopped = db.get_issue_control_state("TEST-STOP")
    assert stopped["state"] == "stopped"
    assert stopped["reason"] == "operator stop"
    assert db.get_issue_control_state("TEST-OTHER")["state"] == "running"

    resume_resp = client.post(f"/api/execution/{stop_id}/resume")
    assert resume_resp.status_code in (302, 303)
    assert db.get_issue_control_state("TEST-STOP")["state"] == "running"

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert b"Stop Brad" not in dashboard.data

    detail = client.get(f"/execution/{stop_id}")
    assert detail.status_code == 200
    assert b"Stop this ticket" in detail.data
    assert b"Resume this ticket" in detail.data


def test_orchestrator_skips_work_for_a_stopped_issue(temp_git_repo, monkeypatch):
    cfg = make_test_config(temp_git_repo)
    orch = BradOrchestrator(cfg)
    orch.ticketing = Mock()
    orch.code_repo = Mock()
    orch.ci = Mock()
    orch.observability = Mock()
    orch.agent = Mock()

    db.set_issue_control_state("TEST-STOP", "stopped", reason="operator pause", updated_by="ui")
    issue = {"key": "TEST-STOP", "fields": {"summary": "Stop feature", "description": "details", "labels": [], "attachment": []}}

    orch._process_issue(issue)

    orch.ticketing.remove_label.assert_not_called()
    orch.code_repo.pr_exists_for_branch.assert_not_called()
    orch.ticketing.comment.assert_not_called()


def test_ci_wait_aborts_when_issue_is_stopped(temp_git_repo):
    cfg = make_test_config(temp_git_repo)
    adapter = GitHubActionsAdapter(cfg)
    db.set_issue_control_state("TEST-STOP", "stopped", reason="operator pause", updated_by="ui")

    with patch("brad.adapters.ci_cd.github_actions_adapter.requests.get") as mock_get:
        result = adapter.wait_for_pr(123, poll_interval=1, timeout=1, issue_key="TEST-STOP")

    assert result.stopped is True
    assert result.success is False
    mock_get.assert_not_called()
